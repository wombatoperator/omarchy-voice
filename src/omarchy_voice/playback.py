"""Paced Live PCM playback with a bounded jitter buffer and playback diagnostics."""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque

from .realtime import frame_level, _terminate


class LiveSpeaker:
    FRAME_SECONDS = .02
    DEVICE_LATENCY = .04
    MAX_BUFFER_SECONDS = 10

    def __init__(self, rate, buffer_ms=120, report=None, target=""):
        self.rate = rate
        self.target = target
        self.buffer_seconds = self._initial_buffer_seconds = buffer_ms / 1000
        self.report = report or (lambda event, **values: None)
        self._pcm = bytearray()
        self._pcm_ready = asyncio.Event()
        self._proc = self._pump = self._stderr_task = None
        self._error = None
        self._envelope = deque()
        self._plays_until = 0.0
        self._primed = False
        self._first_at = None
        self._last_arrival = None
        self._starved_at = None
        self._audible_until = 0.0
        self._last_frame_audible = False
        self._packet_history = deque(maxlen=20)
        self._stream_started_at = None
        self._stats = dict(received_samples=0, played_samples=0, underruns=0, quiet_refill_ms=0,
                           max_arrival_gap_ms=0.0, max_loop_lag_ms=0.0, max_buffer_ms=0.0)
        self._last_report = time.monotonic()

    def check_error(self):
        if self._error:
            raise RuntimeError(self._error)

    async def start(self):
        self.check_error()
        if self._pump is None or self._pump.done():
            self._stats = dict(received_samples=0, played_samples=0, underruns=0, quiet_refill_ms=0,
                               max_arrival_gap_ms=0.0, max_loop_lag_ms=0.0, max_buffer_ms=0.0)
            self.buffer_seconds = self._initial_buffer_seconds
            self._last_report = time.monotonic()
            self._pump = asyncio.create_task(self._start_lazy())

    async def _read_stderr(self):
        while line := await self._proc.stderr.readline():
            self.report("playback_device", message=line.decode(errors="replace")[:1000])

    async def write(self, pcm):
        self.check_error()
        if len(pcm) % 2:
            raise ValueError("Playback requires complete PCM16 samples")
        if not pcm:
            return
        if self._pump is None:
            await self.start()
        now = time.monotonic()
        if self._stream_started_at is None:
            self._stream_started_at = now
        gap_ms = (now - self._last_arrival) * 1000 if self._last_arrival is not None else 0
        self._packet_history.append({"gap_ms": round(gap_ms, 1),
                                     "pcm_ms": round(len(pcm) * 500 / self.rate, 1),
                                     "queued_ms": round(len(self._pcm) * 500 / self.rate, 1)})
        if self._last_arrival is not None:
            self._stats["max_arrival_gap_ms"] = max(
                self._stats["max_arrival_gap_ms"], (now - self._last_arrival) * 1000)
        if self._starved_at is not None:
            if now - self._starved_at < 1 and frame_level(pcm) > 0:
                self._stats["underruns"] += 1
                self.buffer_seconds = min(.24, self.buffer_seconds + .04)
                self.report("playback_underrun", gap_ms=round((now - self._starved_at) * 1000, 1),
                            buffer_target_ms=round(self.buffer_seconds * 1000),
                            recent_packets=list(self._packet_history))
            self._starved_at = None
        self._last_arrival = now
        if self._first_at is None:
            self._first_at = now
        if len(self._pcm) + len(pcm) > self.rate * 2 * self.MAX_BUFFER_SECONDS:
            raise RuntimeError("Playback queue exceeded 10 seconds; stopping instead of dropping speech")
        self._pcm.extend(pcm)
        self._pcm_ready.set()
        self._stats["received_samples"] += len(pcm) // 2
        self._stats["max_buffer_ms"] = max(self._stats["max_buffer_ms"], len(self._pcm) * 500 / self.rate)

    async def _start_lazy(self):
        try:
            started = time.monotonic()
            self._proc = await asyncio.create_subprocess_exec(
                "pw-cat", "--playback", "--raw", "--rate", str(self.rate),
                "--channels", "1", "--format", "s16", "--latency", "40ms",
                *(["--target", self.target] if self.target else []), "-",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
            self.report("playback_device_started", process_start_ms=round((time.monotonic() - started) * 1000, 1))
            self._stderr_task = asyncio.create_task(self._read_stderr())
            await self._pump_loop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = f"Audio playback failed: {type(exc).__name__}: {exc}"
            self.report("playback_error", message=self._error)

    def _next_frame(self, now):
        size = int(self.rate * self.FRAME_SECONDS) * 2
        threshold = int(self.rate * self.buffer_seconds) * 2
        if not self._primed and self._pcm:
            # A short final utterance must also drain if no further packets arrive.
            self._primed = (len(self._pcm) >= threshold or
                            now - self._first_at >= self.buffer_seconds)
        if self._primed and self._pcm:
            # Live sends silence between replies too. Consuming that silence
            # at the device clock can drain the jitter reserve before speech
            # begins. Refill while quiet, without cutting or slowing words.
            if (not self._last_frame_audible and len(self._pcm) < threshold + size
                    and not frame_level(self._pcm[:size])
                    and self._last_arrival is not None and now - self._last_arrival < self.buffer_seconds):
                self._stats["quiet_refill_ms"] += round(self.FRAME_SECONDS * 1000)
                return bytes(size)
            pcm = bytes(self._pcm[:size])
            del self._pcm[:size]
            self._stats["played_samples"] += len(pcm) // 2
            self._last_frame_audible = bool(frame_level(pcm))
            return pcm.ljust(size, b'\0')
        if self._primed:
            self._primed = False
            self._first_at = None
            if now < self._audible_until + .1:
                # Count only if speech resumes; ending a stream is not an underrun.
                self._starved_at = now
        return bytes(size)

    async def _wait_frame(self):
        # PipeWire has 40 ms of device buffering. A packet just across a tick
        # boundary should use a small part of that cushion, not insert silence
        # and force another 160 ms of priming (observed with a 0.6 ms delay).
        if self._primed and len(self._pcm) < int(self.rate * self.FRAME_SECONDS) * 2:
            self._pcm_ready.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._pcm_ready.wait(), .01)

    async def _pump_loop(self):
        deadline = time.monotonic()
        try:
            while True:
                now = time.monotonic()
                lag = max(0, now - deadline)
                self._stats["max_loop_lag_ms"] = max(self._stats["max_loop_lag_ms"], lag * 1000)
                # Never dump seconds of catch-up audio after a stalled event loop.
                if lag > self.FRAME_SECONDS * 2:
                    deadline = now
                await self._wait_frame()
                pcm = self._next_frame(time.monotonic())
                level = frame_level(pcm)
                self._proc.stdin.write(pcm)
                await self._proc.stdin.drain()
                at = time.monotonic() + self.DEVICE_LATENCY
                self._envelope.append((at, at + self.FRAME_SECONDS, level))
                if level:
                    if at > self._audible_until + .3:
                        self.report("playback_speech_started", queued_ms=round(len(self._pcm) * 500 / self.rate, 1),
                                    estimated_device_latency_ms=round(self.DEVICE_LATENCY * 1000))
                    self._plays_until = self._audible_until = at + self.FRAME_SECONDS
                while self._envelope and self._envelope[0][1] < at - 1:
                    self._envelope.popleft()
                if now - self._last_report >= 5:
                    self._report()
                deadline += self.FRAME_SECONDS
                await asyncio.sleep(max(0, deadline - time.monotonic()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = f"Audio playback failed: {type(exc).__name__}: {exc}"
            self.report("playback_error", message=self._error)

    def _report(self):
        self.report("playback_stats", **{k: round(v, 2) for k, v in self._stats.items()},
                    queued_ms=round(len(self._pcm) * 500 / self.rate, 1),
                    stream_elapsed_ms=round((time.monotonic() - self._stream_started_at) * 1000, 1)
                        if self._stream_started_at is not None else 0,
                    source_duration_ms=round(self._stats["received_samples"] * 1000 / self.rate, 1),
                    buffer_target_ms=round(self.buffer_seconds * 1000))
        self._last_report = time.monotonic()

    def level_now(self):
        now = time.monotonic()
        while self._envelope and self._envelope[0][1] <= now:
            self._envelope.popleft()
        return self._envelope[0][2] if self._envelope and self._envelope[0][0] <= now else 0.0

    def is_playing(self, tail=0):
        return time.monotonic() < self._plays_until + tail

    async def interrupt(self):
        await self.close()

    async def close(self):
        pump, self._pump = self._pump, None
        if pump and not pump.done():
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
        proc, self._proc = self._proc, None
        if proc and proc.returncode is None:
            await _terminate(proc)
        if self._stderr_task:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None
        self._report()
        self._pcm.clear()
        self._envelope.clear()
        self._plays_until = self._audible_until = 0.0
        self._primed = False
        self._last_frame_audible = False
        self._packet_history.clear()
        self._stream_started_at = None
        self._first_at = self._last_arrival = self._starved_at = None
        self._error = None
