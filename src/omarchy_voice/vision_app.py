"""Minimal Omarchy camera companion: FFmpeg capture, FFplay preview, Unix IPC.

All frames stay in RAM. Only an explicit inspect makes a model request. Capture
and preview never wait for inference; closing the preview releases the camera.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import struct
import sys
import time

from .vision import fingerprint, image_filters, secure_runtime, validate_inspection, validate_request, validate_settings
from . import config
from .trace import Trace


async def terminate(process):
    if process is not None and process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 1)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


class InvalidJPEGFrame(RuntimeError):
    """A fully consumed multipart record whose JPEG is incomplete."""


async def read_frame(reader):
    header = await reader.readuntil(b"\r\n\r\n")
    match = re.search(rb"Content-length:\s*(\d+)", header, re.I)
    if not match or not 0 <= int(match[1]) <= 10_000_000:
        raise RuntimeError("Invalid camera frame length")
    raw = await reader.readexactly(int(match[1]))
    if not raw.startswith(b"\xff\xd8") or not raw.endswith(b"\xff\xd9"):
        raise InvalidJPEGFrame("Camera did not deliver a complete JPEG")
    return raw


async def locked():
    if not shutil.which("omarchy-shell"):
        return False
    process = await asyncio.create_subprocess_exec("omarchy-shell", "lock", "isLocked",
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 2)
        # On Omarchy an unavailable lock check is not authorization to capture.
        if process.returncode or output.strip().lower() not in {b"true", b"false"}:
            raise RuntimeError("Cannot verify Omarchy lock state")
        return output.strip().lower() == b"true"
    finally:
        await terminate(process)


class Camera:
    def __init__(self, s, report=None):
        self.settings = s
        self.report = report or (lambda *args, **kwargs: None)
        self.stderr = {"capture": "", "preview": ""}
        self.capture = self.preview = None
        self.frame = None
        self.inspection_frame = None
        self.frame_at = self.frame_wall = 0.0
        self.sequence = 0
        self.ready = asyncio.Event()
        self.tasks = []
        self.failure = ""
        self.dropped_frames = 0

    def diagnostics(self):
        return {"capture_exit": self.capture.returncode if self.capture else None,
                "preview_exit": self.preview.returncode if self.preview else None,
                "capture_stderr": self.stderr["capture"], "preview_stderr": self.stderr["preview"],
                "frame_sequence": self.sequence,
                "dropped_frames": self.dropped_frames,
                "frame_age_ms": round((time.monotonic() - self.frame_at) * 1000) if self.frame else None}

    async def _stderr(self, name, stream):
        # Drain continuously, retain only a small tail, never log frames.
        while chunk := await stream.read(1024):
            self.stderr[name] = (self.stderr[name] + chunk.decode(errors="replace"))[-2048:]

    @property
    def active(self):
        return (self.capture is not None and self.capture.returncode is None
                and self.preview is not None and self.preview.returncode is None and not self.failure)

    async def start(self):
        if self.active:
            return
        await self.stop()
        self.stderr = {"capture": "", "preview": ""}
        self.dropped_frames = 0
        began = time.monotonic()
        for name in ("ffmpeg", "ffplay"):
            if not shutil.which(name):
                raise RuntimeError(f"{name} is required; install FFmpeg")
        s = self.settings
        if not os.path.exists(s["device"]):
            raise RuntimeError(f"Camera device {s['device']} is missing; reconnect it or enable USB streaming")
        capture = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "v4l2",
                   "-input_format", s["input_format"], "-video_size", f'{s["width"]}x{s["height"]}',
                   "-framerate", str(s["fps"]), "-i", s["device"], "-an"]
        capture += ["-c:v", "copy"] if s["input_format"] == "mjpeg" else ["-c:v", "mjpeg", "-q:v", "3", "-threads", "1"]
        capture += ["-f", "mpjpeg", "pipe:1"]
        try:
            self.capture = await asyncio.create_subprocess_exec(*capture, stdout=asyncio.subprocess.PIPE,
                                                                stderr=asyncio.subprocess.PIPE)
            self.preview = await asyncio.create_subprocess_exec(
                "ffplay", "-hide_banner", "-loglevel", "error", "-f", "mjpeg", "-framerate", str(s["preview_fps"]),
                "-probesize", "32", "-analyzeduration", "0", "-flags", "low_delay", "-framedrop",
                "-window_title", "OMA Vision — Camera on · Q / Esc to close", "-x", "640", "-y", "360",
                "-filter_threads", "1",
                *(["-vf", ",".join(image_filters(s, preview=True))] if image_filters(s, preview=True) else []),
                "-autoexit", "-i", "pipe:0",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            self.tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._preview()),
                          asyncio.create_task(self._stderr("capture", self.capture.stderr)),
                          asyncio.create_task(self._stderr("preview", self.preview.stderr))]
            await asyncio.wait_for(self.ready.wait(), 8)
            if not self.active or not self.frame:
                raise RuntimeError(self.failure or "Camera or preview failed to open")
            self.report("vision_capture_started", startup_ms=round((time.monotonic() - began) * 1000, 1),
                        capture_pid=self.capture.pid, preview_pid=self.preview.pid)
        except BaseException:
            await self.stop()
            raise

    async def _read(self):
        started = time.monotonic()
        consecutive_bad = 0
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(read_frame(self.capture.stdout), 3)
                except InvalidJPEGFrame:
                    # The declared multipart body was fully consumed, so the
                    # next header remains aligned. Never retry framing/IO errors.
                    consecutive_bad += 1
                    self.dropped_frames += 1
                    if consecutive_bad == 1:
                        self.report("vision_frame_dropped", consecutive_bad=consecutive_bad, **self.diagnostics())
                    # FFmpeg can emit a burst of empty records at once. Bound
                    # recovery by elapsed freshness, not by the nominal FPS.
                    if time.monotonic() - max(started, self.frame_at) >= 1 or consecutive_bad >= 10_000:
                        raise RuntimeError("Camera JPEG stream did not recover within the frame budget") from None
                    continue
                if consecutive_bad:
                    self.report("vision_frames_recovered", skipped=consecutive_bad, **self.diagnostics())
                consecutive_bad = 0
                if time.monotonic() - started < .25:
                    continue  # Let exposure settle, without retaining warm-up frames.
                self.frame, self.frame_at, self.frame_wall = raw, time.monotonic(), time.time()
                self.sequence += 1
                self.ready.set()
        except (OSError, RuntimeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError) as exc:
            detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            self.failure = "Camera feed stopped: " + detail
            self.report("vision_capture_error", error=self.failure, **self.diagnostics())
            self.ready.set()

    async def _preview(self):
        try:
            while True:
                frame = self.inspection_frame or self.frame
                if frame:
                    self.preview.stdin.write(frame)
                    await asyncio.wait_for(self.preview.stdin.drain(), 1)
                await asyncio.sleep(1 / self.settings["preview_fps"])
        except (OSError, RuntimeError, asyncio.TimeoutError) as exc:
            self.failure = "Preview stopped: " + type(exc).__name__
            self.report("vision_preview_error", error=self.failure, **self.diagnostics())

    async def fresh(self):
        # Wait for a frame captured AFTER this request, not a cached camera image.
        sequence = self.sequence
        deadline = time.monotonic() + 2
        while self.sequence == sequence:
            if not self.active:
                raise RuntimeError(self.failure or "Camera is off")
            if time.monotonic() > deadline:
                raise RuntimeError("No fresh camera frame")
            await asyncio.sleep(.01)
        return self.frame, self.frame_wall, self.sequence

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        await asyncio.gather(terminate(self.capture), terminate(self.preview))
        self.capture = self.preview = None
        self.frame = None
        self.inspection_frame = None
        self.ready.clear()
        self.failure = ""


class Companion:
    def __init__(self, s, report=None):
        self.settings = s
        self.report = report or (lambda *args, **kwargs: None)
        self.camera = Camera(s, self.report)
        self.inference = self.transform = None
        self.busy = False
        self.lifecycle = asyncio.Lock()
        self.epoch = 0
        self.previous = ""
        self.owner = ""
        self.owner_pid = 0
        self.started = self.last_activity = time.monotonic()
        self.requests = 0
        self.reason = "off"
        self.done = asyncio.Event()

    def status(self):
        return {"ok": True, "active": self.camera.active, "busy": self.busy, "status": self.reason,
                "model": self.settings["model"], "protocol": self.settings["protocol"],
                "settings_id": fingerprint(self.settings), "requests": self.requests,
                "crop_percent": self.settings["crop_percent"], "sharpen": self.settings["sharpen"],
                "frame_age_ms": round((time.monotonic() - self.camera.frame_at) * 1000) if self.camera.frame else None}

    async def start(self, owner, owner_pid):
        async with self.lifecycle:
            generation = self.epoch
            self.owner, self.owner_pid = owner, owner_pid
            if await locked():
                raise RuntimeError("Camera is unavailable while the session is locked")
            if not self.camera.active:
                await self.camera.start()
                self.started = time.monotonic()
                self.requests = 0
                self.previous = ""
            if generation != self.epoch:
                await self.camera.stop()
                raise RuntimeError("Camera start cancelled")
            self.last_activity = time.monotonic()
            self.reason = "camera on"
        return self.status()

    async def stop(self, reason="off"):
        # Increment before awaits: in-flight results can never revive a stopped session.
        self.epoch += 1
        async with self.lifecycle:
            self.reason = reason
            if self.camera.capture or self.busy:
                self.report("vision_stopped", reason=reason, busy=self.busy, requests=self.requests,
                            **self.camera.diagnostics())
            await asyncio.gather(terminate(self.inference), terminate(self.transform))
            await self.camera.stop()
            self.previous = ""
            self.reason = reason
            self.last_activity = time.monotonic()
        return self.status()

    async def image(self, raw, region, *, inspection=None):
        return await self.transform_image(raw, image_filters(self.settings, region, inspection=inspection))

    async def transform_image(self, raw, filters):
        if not filters:
            return raw
        epoch = self.epoch
        self.transform = await asyncio.create_subprocess_exec("ffmpeg", "-hide_banner", "-loglevel", "error",
            "-filter_threads", "1",
            "-f", "mjpeg", "-i", "pipe:0", "-vf", ",".join(filters), "-frames:v", "1", "-threads", "1",
            "-c:v", "mjpeg", "-q:v", "3", "-f", "image2pipe", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            if epoch != self.epoch:
                raise RuntimeError("Camera stopped during image processing")
            output, _ = await asyncio.wait_for(self.transform.communicate(raw), 5)
            if self.transform.returncode or not output.startswith(b"\xff\xd8"):
                raise RuntimeError("Camera crop/resize failed")
            return output
        finally:
            await terminate(self.transform)
            self.transform = None

    async def detail_preview(self, image):
        # Pad around the detail so FFplay's existing framing shows the whole
        # snapshot in the SAME window. Live preview needs no extra encoder.
        s = self.settings
        scale = min(640 / s["width"], 640 / s["height"])
        width, height = (max(2, int(s[key] * scale) // 2 * 2) for key in ("width", "height"))
        keep = (100 - s["crop_percent"]) / 100
        view_w, view_h = int(width * keep) // 2 * 2, int(height * keep) // 2 * 2
        return await self.transform_image(image, [
            f"scale={view_w}:{view_h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
            f"pad={view_w}:{view_h}:(ow-iw)/2:(oh-ih)/2",
            "drawbox=x=0:y=0:w=iw:h=ih:color=cyan:t=3",
            "drawtext=text='DETAIL SNAPSHOT':fontsize=14:fontcolor=white:box=1:boxcolor=black@0.8:x=6:y=6",
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"])

    def check_inspection(self, epoch):
        if epoch != self.epoch or not self.camera.active:
            raise RuntimeError("Camera inspection was cancelled; discarded old result: " +
                               (self.camera.failure or self.reason))

    async def model(self, image, question, epoch, **options):
        self.check_inspection(epoch)
        if self.requests >= self.settings["max_requests"]:
            raise RuntimeError("Camera session request limit reached")
        self.requests += 1  # Every attempted provider call consumes the session budget.
        try:
            self.inference = await asyncio.create_subprocess_exec(sys.executable, "-m", "omarchy_voice.vision_provider",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            self.check_inspection(epoch)
            message = {"settings": self.settings, "image": base64.b64encode(image).decode(), "question": question,
                       "previous": self.previous, **options}
            data, _ = await self.inference.communicate(json.dumps(message).encode())
            self.check_inspection(epoch)
            result = json.loads(data)
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "Camera inspection failed"))
            return result
        finally:
            await terminate(self.inference)
            self.inference = None

    async def inspect(self, question, region, owner, owner_pid):
        if self.busy:
            raise RuntimeError("A camera inspection is already running; requests are not queued")
        self.busy = True
        began = time.monotonic()
        stage = "start"
        self.report("vision_inspect_started", model=self.settings["model"], protocol=self.settings["protocol"],
                    cropped=bool(region) or bool(self.settings["crop_percent"]),
                    crop_percent=self.settings["crop_percent"], sharpen=self.settings["sharpen"], epoch=self.epoch)
        try:
            await self.start(owner, owner_pid)
            ready_ms = round((time.monotonic() - began) * 1000, 1)
            epoch = self.epoch
            if self.requests >= self.settings["max_requests"]:
                raise RuntimeError("Camera session request limit reached; stop and start a new session")
            stage = "fresh_frame"
            source, captured_at, sequence = await self.camera.fresh()
            stage = "transform"
            raw = await self.image(source, region)
            self.check_inspection(epoch)
            self.reason = "inspecting"
            stage = "provider"
            prepared_ms = round((time.monotonic() - began) * 1000, 1)
            selection, detail, requests = None, None, []
            allow = self.settings["auto_inspect"] and self.requests + 2 <= self.settings["max_requests"]
            async with asyncio.timeout(self.settings["timeout_seconds"]):
                for step in range(2):
                    stage = "provider" if step == 0 else "detail_provider"
                    call_started_ms = (time.monotonic() - began) * 1000
                    options = {"allow_inspection": allow and step == 0}
                    if detail is not None:
                        options.update(detail_image=base64.b64encode(detail).decode(), inspection=selection)
                    result = await self.model(raw, question, epoch, **options)
                    requests.append({key: result.get(key) for key in ("model_ms", "first_text_ms", "usage")})
                    self.report("vision_model_finished", step=step + 1, frame_sequence=sequence, **requests[-1])
                    if "inspection" not in result:
                        if not isinstance(result.get("observation"), str) or not result["observation"].strip():
                            raise RuntimeError("Vision returned no completed observation")
                        break
                    if not options["allow_inspection"]:
                        raise RuntimeError("Vision inspection step limit reached; no further tools executed")
                    selection = result["inspection"]
                    validate_inspection(selection)
                    self.report("vision_inspection_selected", frame_sequence=sequence, **selection)
                    stage = "detail_transform"
                    detail = await self.image(source, region, inspection=selection)
                    self.check_inspection(epoch)
                    stage = "detail_preview"
                    try:
                        preview = await self.detail_preview(detail)
                    except (RuntimeError, OSError):
                        # A missing optional drawtext filter must not turn a
                        # valid inspection into another paid retry.
                        self.report("vision_detail_preview_unavailable", frame_sequence=sequence)
                    else:
                        self.check_inspection(epoch)
                        self.camera.inspection_frame = preview
            self.check_inspection(epoch)
            usage = {}
            for field, chat_field in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
                values = [(r["usage"] or {}).get(field, (r["usage"] or {}).get(chat_field)) for r in requests]
                usage[field] = sum(values) if all(type(v) is int for v in values) else None
            result.update(model_calls=len(requests), usage=usage, request_metrics=requests,
                          model_ms=round(sum(r["model_ms"] or 0 for r in requests), 1),
                          first_text_ms=round(call_started_ms + result["first_text_ms"], 1)
                              if result.get("first_text_ms") is not None else None)
            self.previous = f'Captured at {captured_at}: {result["observation"]}'
            self.last_activity = time.monotonic()
            self.reason = "camera on"
            self.report("vision_inspect_finished", ready_ms=ready_ms, prepared_ms=prepared_ms,
                        total_ms=round((time.monotonic() - began) * 1000, 1),
                        model_ms=result.get("model_ms"), first_text_ms=result.get("first_text_ms"),
                        usage=result.get("usage"), model_calls=len(requests), image_bytes=len(raw),
                        detail_image_bytes=len(detail) if detail else 0, frame_sequence=sequence)
            return {**result, "model": self.settings["model"], "captured_at": captured_at, "frame_sequence": sequence,
                    "crop_percent": self.settings["crop_percent"], "sharpen": self.settings["sharpen"],
                    "region": region, "inspection": selection, "image_bytes": len(raw),
                    "detail_image_bytes": len(detail) if detail else 0,
                    "total_ms": round((time.monotonic() - began) * 1000, 1),
                    "observation_age_ms": round((time.time() - captured_at) * 1000),
                    "freshness": "Snapshot at captured_at; not a claim of continuous awareness"}
        except asyncio.TimeoutError:
            self.report("vision_inspect_error", stage=stage, error="timeout", status=self.reason,
                        total_ms=round((time.monotonic() - began) * 1000, 1), **self.camera.diagnostics())
            raise RuntimeError("Camera inspection timed out; no automatic retry") from None
        except Exception as exc:
            # Provider errors can contain private content. Log type/stage and
            # local capture diagnostics, not the question or model observation.
            self.report("vision_inspect_error", stage=stage, error_type=type(exc).__name__, status=self.reason,
                        capture_failure=self.camera.failure,
                        total_ms=round((time.monotonic() - began) * 1000, 1), **self.camera.diagnostics())
            raise
        finally:
            self.camera.inspection_frame = None
            self.busy = False
            if self.camera.active:
                self.reason = "camera on"

    async def handle(self, reader, writer):
        try:
            peer = writer.get_extra_info("socket")
            _, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                raise RuntimeError("Camera IPC is restricted to this user")
            message = json.loads(await asyncio.wait_for(reader.readline(), 3))
            if set(message) - {"action", "question", "region", "owner", "owner_pid", "owner_only"}:
                raise ValueError("Unknown camera request fields")
            action, question, region = message.get("action"), message.get("question", ""), message.get("region")
            validate_request(action, question, region)
            owner, pid = message.get("owner", "manual"), message.get("owner_pid", 0)
            if not isinstance(owner, str) or len(owner) > 128 or type(pid) is not int or pid < 0:
                raise ValueError("Invalid camera owner")
            if action in {"inspect", "start"} and not self.settings["enabled"]:
                raise RuntimeError("Camera vision disabled")
            if action == "inspect":
                result = await self.inspect(question, region, owner, pid)
            elif action == "start":
                result = await self.start(owner, pid)
            elif action in {"stop", "quit"}:
                if message.get("owner_only") and message["owner_only"] != self.owner:
                    result = self.status()
                else:
                    result = await self.stop()
                    if action == "quit":
                        self.done.set()
            else:
                result = self.status()
        except Exception as exc:
            result = {"ok": False, "error": str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__ + ": camera operation failed"}
        try:
            writer.write(json.dumps(result).encode() + b"\n")
            await writer.drain()
        except (OSError, RuntimeError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def monitor(self):
        tick = 0
        while not self.done.is_set():
            await asyncio.sleep(.25)
            tick += 1
            if self.lifecycle.locked():
                continue
            if self.camera.capture:
                reason = ""
                if not self.camera.active or time.monotonic() - self.camera.frame_at > 3:
                    reason = (self.camera.failure or
                              ("preview exited" if self.camera.preview and self.camera.preview.returncode is not None else
                               "capture exited" if self.camera.capture.returncode is not None else "camera frame stalled"))
                elif time.monotonic() - self.started >= self.settings["max_session_seconds"]:
                    reason = "camera session time limit reached"
                elif not self.busy and time.monotonic() - self.last_activity >= self.settings["idle_seconds"]:
                    reason = "camera idle timeout"
                elif self.owner_pid:
                    try:
                        os.kill(self.owner_pid, 0)
                    except ProcessLookupError:
                        reason = "voice process exited"
                if not reason and tick % 8 == 0:
                    try:
                        if await locked():
                            reason = "session locked"
                    except (RuntimeError, OSError, asyncio.TimeoutError):
                        reason = "lock state unavailable"
                if reason:
                    await self.stop(reason)
            elif not self.busy and time.monotonic() - self.last_activity > 15:
                self.done.set()  # No idle daemon required.


async def serve(s):
    validate_settings(s)
    directory = secure_runtime()
    fd = os.open(directory / "app.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / "control.sock"
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        trace = Trace(config.STATE_DIR / "vision-trace.jsonl", max_bytes=1024 * 1024, backups=2)
        def report(event, **values):
            # Diagnostics must never break camera service (e.g. disk full).
            with contextlib.suppress(OSError):
                trace.write(event, companion_pid=os.getpid(), **values)
        app = Companion(s, report)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, app.done.set)
        server = await asyncio.start_unix_server(app.handle, path=str(path), limit=8192)
        os.chmod(path, 0o600)
        monitor = asyncio.create_task(app.monitor())
        try:
            async with server:
                await app.done.wait()
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            await app.stop()
            path.unlink(missing_ok=True)
    finally:
        os.close(fd)


def main():
    os.umask(0o077)
    try:
        s = json.loads(sys.stdin.buffer.read(32_000))
        asyncio.run(serve(s))
    except Exception as exc:
        print(f"Vision companion stopped: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
