"""Free vision tests: provider wire contracts, fresh frames, cancellation and IPC."""
import asyncio
import base64
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omarchy_voice import config, vision, vision_app, vision_provider
from omarchy_voice.tools import Executor, tools_for


def wire(events):
    return io.BytesIO(b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events))

DETAIL = {"region": [250, 250, 500, 500], "rotation": 90, "enhancement": "contrast_sharpen"}


class ConfigurationTests(unittest.TestCase):
    def test_namespacing_and_endpoint_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[vision]\nmodel="local-vlm"\nprotocol="chat_completions"\nbase_url="http://localhost:8000/v1"\nreasoning_effort=""\ndetail=""\n')
            cfg = config.load(path)
            s = vision.settings(cfg)
            self.assertFalse(cfg.unknown_keys)
            self.assertEqual(s["model"], "local-vlm")
            self.assertEqual(s["api_key_env"], "")
            self.assertEqual(vision.settings(config.Config())["api_key_env"], "OPENAI_API_KEY")
            cfg.vision_api_key_env = "LOCAL_VLM_TOKEN"
            self.assertEqual(vision.settings(cfg)["api_key_env"], "LOCAL_VLM_TOKEN")

    def test_invalid_config_and_crop_fail_before_launch(self):
        for kwargs in ({"vision_timeout_seconds": float("nan")}, {"vision_max_requests": 0},
                       {"vision_auto_inspect": "yes"},
                       {"vision_base_url": "http://external.example/v1"},
                       {"vision_base_url": "https://" + "user:pass@" + "example.com/v1"},
                       {"vision_crop_percent": 100}, {"vision_crop_percent": True},
                       {"vision_sharpen": float("nan")}, {"vision_sharpen": -1},
                       {"vision_input_format": "mjpeg -i bad"}):
            with self.assertRaises(ValueError):
                vision.settings(config.Config(**kwargs))
        for region in ([0, 0, 1001, 500], [0, 0, True, 1], [0, 0, -1, 1], [1, 2]):
            with self.assertRaises(ValueError):
                vision.validate_request("inspect", region=region)

    def test_tool_disabled_and_dry_run_never_launch(self):
        self.assertNotIn("camera_view", {s["name"] for s in tools_for(config.Config(vision_enabled=False))})
        with mock.patch.object(vision, "rpc") as rpc, mock.patch.object(vision.VisionClient, "ensure") as launch:
            result = Executor(config.Config(dry_run=True)).call("camera_view", {"action": "inspect", "question": "What is this?"})
            self.assertTrue(result.ok)
            rpc.assert_not_called()
            launch.assert_not_called()

    def test_muting_only_stops_owned_camera(self):
        client = vision.VisionClient(config.Config(), session=True)
        with mock.patch.object(vision, "rpc") as rpc:
            client.stop_owned()
            rpc.assert_not_called()
            client.used = True
            client.stop_owned()
            self.assertEqual(rpc.call_args.args[0], {"action": "stop", "owner_only": client.owner})

    def test_runtime_rejects_symlink_and_public_directory(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, "RUNTIME_DIR", Path(tmp) / "run"):
            root = config.RUNTIME_DIR
            root.mkdir(mode=0o755)
            with self.assertRaises(RuntimeError):
                vision.secure_runtime()
            root.chmod(0o700)
            (root / "vision").symlink_to(Path(tmp), target_is_directory=True)
            with self.assertRaises(RuntimeError):
                vision.secure_runtime()


class ProviderTests(unittest.TestCase):
    def test_tool_is_optional_and_final_request_contains_both_views_without_tools(self):
        for protocol in ("responses", "chat_completions"):
            with self.subTest(protocol=protocol):
                s = vision.settings(config.Config(vision_protocol=protocol))
                _, initial = vision_provider.payload(s, "overview", "Identify this", allow_inspection=True)
                self.assertEqual(len(initial["tools"]), 1)
                self.assertEqual(initial["tool_choice"], "auto")
                self.assertFalse(initial["parallel_tool_calls"])
                _, final = vision_provider.payload(s, "overview", "Identify this", detail_image="detail", inspection=DETAIL)
                self.assertNotIn("tools", final)
                content = final["input" if protocol == "responses" else "messages"][0]["content"]
                self.assertEqual(len(content), 3)
                self.assertIn("overview", json.dumps(content[1]))
                self.assertIn("detail", json.dumps(content[2]))

    def test_only_one_complete_valid_inspection_can_execute(self):
        call = {"type": "function_call", "name": "inspect_region", "arguments": json.dumps(DETAIL)}
        cases = [[call], [call, call], [{**call, "name": "shell"}],
                 [{**call, "arguments": '{"region":'}], [{**call, "status": "in_progress"}],
                 [{**call, "arguments": json.dumps({**DETAIL, "command": "anything"})}],
                 [{**call, "arguments": json.dumps({**DETAIL, "rotation": True})}],
                 [{**call, "arguments": json.dumps({**DETAIL, "region": [900, 0, 500, 500]})}],
                 [{**call, "arguments": json.dumps({**DETAIL, "enhancement": "arbitrary filter"})}]]
        for calls in cases:
            events = [{"type": "response.completed", "response": {"status": "completed", "output": calls}}]
            with self.subTest(calls=calls):
                if calls == [call]:
                    result = vision_provider.consume(wire(events), "responses", time.monotonic(), allow_inspection=True)
                    self.assertEqual(result["inspection"], DETAIL)
                else:
                    with self.assertRaises(RuntimeError):
                        vision_provider.consume(wire(events), "responses", time.monotonic(), allow_inspection=True)
                with self.assertRaises(RuntimeError):
                    vision_provider.consume(wire(events), "responses", time.monotonic())

    def test_chat_tool_arguments_are_assembled_but_never_used_if_truncated(self):
        arguments = json.dumps(DETAIL)
        events = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "type": "function", "function": {
            "name": "inspect_region", "arguments": arguments[:20]}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": arguments[20:]}}]},
                          "finish_reason": "tool_calls"}]}]
        result = vision_provider.consume(wire(events), "chat_completions", time.monotonic(), allow_inspection=True)
        self.assertEqual(result["inspection"], DETAIL)
        for ending in ("length", "stop", None):
            events[-1]["choices"][0]["finish_reason"] = ending
            with self.subTest(ending=ending), self.assertRaises(RuntimeError):
                vision_provider.consume(wire(events), "chat_completions", time.monotonic(), allow_inspection=True)

    def test_responses_and_local_chat_payloads(self):
        s = vision.settings(config.Config())
        image = base64.b64encode(b"JPEG fixture").decode()
        endpoint, payload = vision_provider.payload(s, image, "Read the label", "previous")
        self.assertEqual(endpoint, "/responses")
        self.assertFalse(payload["store"])
        self.assertEqual(payload["max_output_tokens"], 768)
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["input"][0]["content"][1]["detail"], "original")
        s.update(protocol="chat_completions", reasoning_effort="", detail="", api_key_env="")
        endpoint, payload = vision_provider.payload(s, image, "Read the label")
        self.assertEqual(endpoint, "/chat/completions")
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("detail", payload["messages"][0]["content"][1]["image_url"])

    def test_streams_and_incomplete_answers(self):
        events = [{"type": "response.output_text.delta", "delta": "Pi 5"},
                  {"type": "response.completed", "response": {"status": "completed", "id": "fixture", "usage": {"output_tokens": 4}}}]
        result = vision_provider.consume(wire(events), "responses", time.monotonic())
        self.assertEqual(result["observation"], "Pi 5")
        self.assertIsNotNone(result["first_text_ms"])
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            vision_provider.consume(wire([{"type": "response.incomplete"}]), "responses", time.monotonic())
        chat = [{"choices": [{"delta": {"content": "label"}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
        self.assertEqual(vision_provider.consume(wire(chat), "chat_completions", time.monotonic())["observation"], "label")
        chat[-1]["choices"][0]["finish_reason"] = "length"
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            vision_provider.consume(wire(chat), "chat_completions", time.monotonic())

    def test_custom_provider_does_not_receive_openai_secret(self):
        s = vision.settings(config.Config(vision_base_url="http://localhost:8000/v1", vision_protocol="chat_completions"))
        fake = wire([{"choices": [{"delta": {"content": "fixture"}, "finish_reason": "stop"}]}])
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "do-not-forward"}), \
             mock.patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = fake
            vision_provider.analyse(s, "", "question")
            request = opener.return_value.open.call_args.args[0]
            self.assertNotIn("Authorization", request.headers)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    @contextlib.contextmanager
    def inspection_fixture(self, app, replies):
        replies, messages = iter(replies), []
        async def communicate(data):
            messages.append(json.loads(data))
            return json.dumps(next(replies)).encode(), None
        process = mock.Mock(returncode=0, communicate=mock.AsyncMock(side_effect=communicate))
        with mock.patch.object(app, "start"), \
             mock.patch.object(app.camera, "fresh", return_value=(b"original", 123.0, 7)) as fresh, \
             mock.patch.object(app, "image", side_effect=[b"overview", b"detail"]) as image, \
             mock.patch.object(app, "detail_preview", return_value=b"preview"), \
             mock.patch.object(vision_app.Camera, "active", new_callable=mock.PropertyMock, return_value=True), \
             mock.patch.object(vision_app.asyncio, "create_subprocess_exec", return_value=process):
            yield messages, fresh, image

    async def test_automatic_detail_uses_same_source_and_counts_both_requests(self):
        app = vision_app.Companion(vision.settings(config.Config()), mock.Mock())
        usage = {"input_tokens": 11, "output_tokens": 7}
        replies = [{"ok": True, "inspection": DETAIL, "usage": usage, "model_ms": 12},
                   {"ok": True, "observation": "Identified from the label", "usage": usage,
                    "model_ms": 15, "first_text_ms": 2}]
        with self.inspection_fixture(app, replies) as (messages, fresh, image):
            result = await app.inspect("Which component?", [0, 0, 500, 1000], "voice", 0)
            fresh.assert_awaited_once()
            image.assert_has_awaits([mock.call(b"original", [0, 0, 500, 1000]),
                                    mock.call(b"original", [0, 0, 500, 1000], inspection=DETAIL)])
        self.assertEqual(len(messages), 2)
        self.assertTrue(messages[0]["allow_inspection"])
        self.assertFalse(messages[1]["allow_inspection"])
        self.assertEqual(messages[0]["image"], messages[1]["image"])
        self.assertEqual(base64.b64decode(messages[1]["detail_image"]), b"detail")
        self.assertEqual(result["captured_at"], 123.0)
        self.assertEqual(result["frame_sequence"], 7)
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(app.requests, 2)
        self.assertEqual(result["usage"], {"input_tokens": 22, "output_tokens": 14})
        self.assertEqual(result["model_ms"], 27)
        self.assertIsNone(app.camera.inspection_frame)
        self.assertFalse(app.busy)
        self.assertNotIn("Identified from the label", str(app.report.call_args_list))

    async def test_direct_answer_and_disabled_or_exhausted_detail_budget_use_one_call(self):
        for enabled, remaining, allow in ((True, 20, True), (False, 20, False), (True, 1, False)):
            app = vision_app.Companion(vision.settings(config.Config(vision_auto_inspect=enabled)))
            app.requests = 20 - remaining
            with self.subTest(enabled=enabled, remaining=remaining), \
                 self.inspection_fixture(app, [{"ok": True, "observation": "Direct answer"}]) as (messages, _, image):
                result = await app.inspect("What is this?", None, "voice", 0)
                self.assertEqual(len(messages), 1)
                self.assertEqual(messages[0]["allow_inspection"], allow)
                self.assertEqual(image.await_count, 1)
                self.assertEqual(result["model_calls"], 1)
                self.assertIsNone(result["inspection"])

    async def test_unavailable_or_repeated_tool_call_does_not_execute(self):
        for enabled, replies, expected in ((False, [{"ok": True, "inspection": DETAIL}], 1),
                (True, [{"ok": True, "inspection": DETAIL}] * 2, 2)):
            app = vision_app.Companion(vision.settings(config.Config(vision_auto_inspect=enabled)))
            with self.subTest(enabled=enabled), self.inspection_fixture(app, replies) as (messages, _, image):
                with self.assertRaisesRegex(RuntimeError, "step limit"):
                    await app.inspect("Identify", None, "voice", 0)
                self.assertEqual(len(messages), expected)
                self.assertEqual(image.await_count, expected)
            self.assertFalse(app.busy)
            self.assertIsNone(app.camera.inspection_frame)

    async def test_stop_after_crop_prevents_second_upload_and_snapshot_display(self):
        app = vision_app.Companion(vision.settings(config.Config()))
        async def crop(raw, region, **options):
            if options:
                app.epoch += 1
            return b"image"
        with self.inspection_fixture(app, [{"ok": True, "inspection": DETAIL}]) as (messages, _, image):
            image.side_effect = crop
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                await app.inspect("Identify", None, "voice", 0)
        self.assertEqual(len(messages), 1)
        self.assertEqual(app.previous, "")
        self.assertIsNone(app.camera.inspection_frame)

    async def test_detail_failure_is_not_retried_and_restores_preview(self):
        app = vision_app.Companion(vision.settings(config.Config()))
        with self.inspection_fixture(app, [{"ok": True, "inspection": DETAIL},
                {"ok": False, "error": "Provider unavailable"}]) as (messages, _, image):
            with self.assertRaisesRegex(RuntimeError, "Provider unavailable"):
                await app.inspect("Identify", None, "voice", 0)
        self.assertEqual(len(messages), 2)
        self.assertIsNone(app.camera.inspection_frame)
        self.assertFalse(app.busy)

    async def test_both_model_steps_share_one_timeout(self):
        app = vision_app.Companion(vision.settings(config.Config()))
        app.settings["timeout_seconds"] = .2
        model = app.model
        async def delayed(*args, **kwargs):
            await asyncio.sleep(.12)
            return await model(*args, **kwargs)
        with self.inspection_fixture(app, [{"ok": True, "inspection": DETAIL}]) as (messages, _, _), \
             mock.patch.object(app, "model", side_effect=delayed):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                await app.inspect("Identify", None, "voice", 0)
        self.assertEqual(len(messages), 1)
        self.assertIsNone(app.camera.inspection_frame)
        self.assertFalse(app.busy)

    async def test_real_provider_process_roundtrip_against_local_synthetic_server(self):
        messages = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                messages.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                result = ({"status": "completed", "output": [{"type": "function_call", "name": "inspect_region",
                    "arguments": json.dumps(DETAIL)}]} if len(messages) == 1 else
                    {"status": "completed", "output": []})
                events = ([] if len(messages) == 1 else [{"type": "response.output_text.delta", "delta": "Fixture answer"}])
                events.append({"type": "response.completed", "response": result})
                data = wire(events).getvalue()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        app = vision_app.Companion(vision.settings(config.Config(
            vision_base_url=f'http://127.0.0.1:{server.server_port}/v1')))
        try:
            with mock.patch.object(app, 'start'), \
                 mock.patch.object(app.camera, 'fresh', return_value=(b'original', 123.0, 7)), \
                 mock.patch.object(app, 'image', side_effect=[b'overview', b'detail']), \
                 mock.patch.object(app, 'detail_preview', side_effect=RuntimeError('No drawtext filter')), \
                 mock.patch.object(vision_app.Camera, 'active', new_callable=mock.PropertyMock, return_value=True), \
                 mock.patch.dict(os.environ, {'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}):
                result = await app.inspect('Identify the fixture', None, 'test', 0)
            self.assertEqual(result['observation'], 'Fixture answer')
            self.assertEqual(result['model_calls'], 2)
            self.assertEqual(len(messages), 2)
            self.assertIn('tools', messages[0])
            self.assertNotIn('tools', messages[1])
            self.assertEqual(len(messages[1]['input'][0]['content']), 3)
            self.assertIsNone(app.camera.inspection_frame)
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is optional in unit-test environments')
    async def test_detail_crop_rotation_and_preview_preserve_selected_pixels(self):
        pixels = b''.join(b'\xff\x00\x00' if x < 160 else
                          b'\x00\xff\x00' if y < 120 else b'\x00\x00\xff'
                          for y in range(240) for x in range(320))
        def ffmpeg(data, *args):
            return subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-filter_threads', '1',
                '-i', 'pipe:0', *args, '-threads', '1', 'pipe:1'], input=data, capture_output=True,
                check=True, timeout=5).stdout
        raw = await asyncio.to_thread(ffmpeg, b'P6\n320 240\n255\n' + pixels,
                                      '-frames:v', '1', '-c:v', 'mjpeg', '-f', 'image2pipe')
        app = vision_app.Companion(vision.settings(config.Config(vision_width=320, vision_height=240)))
        selection = {"region": [500, 0, 500, 1000], "rotation": 90, "enhancement": "contrast_sharpen"}
        detail = await app.image(raw, None, inspection=selection)
        rgb = await asyncio.to_thread(ffmpeg, detail, '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo')
        self.assertEqual(len(rgb), 168 * 112 * 3)
        left, right = (56 * 168 + 25) * 3, (56 * 168 + 140) * 3
        self.assertGreater(rgb[left + 2], 200)  # Bottom blue becomes left after clockwise rotation.
        self.assertGreater(rgb[right + 1], 200)
        self.assertLess(rgb[left], 30)  # Original left/red half must be excluded.
        # Smaller overview uploads must not force detail crops to use its lost pixels.
        app.settings["image_width"] = 160
        detail = await app.image(raw, None, inspection=selection)
        rgb = await asyncio.to_thread(ffmpeg, detail, '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo')
        self.assertEqual(len(rgb), 160 * 106 * 3)
        filters = subprocess.run(['ffmpeg', '-hide_banner', '-filters'], capture_output=True, text=True, check=True).stdout
        if 'drawtext' not in filters:
            return  # Optional preview label; geometry/rotation checks above still run.
        preview = await app.detail_preview(detail)
        shown = await asyncio.to_thread(ffmpeg, preview, '-vf', ','.join(vision.image_filters(app.settings, preview=True)),
                                        '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo')
        self.assertEqual(len(shown), 448 * 336 * 3)
        self.assertGreater(shown[(168 * 448 + 70) * 3 + 2], 180)
        self.assertGreater(shown[(168 * 448 + 370) * 3 + 1], 180)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is optional in unit-test environments')
    async def test_default_crop_removes_edges_and_region_uses_zoomed_coordinates(self):
        # Synthetic red border, blue middle: the requested crop must remove the
        # border in both the visible preview and the model's image.
        pixels = b''.join(b'\xff\x00\x00' if x < 32 or x >= 288 or y < 24 or y >= 216
                          else b'\x00\x00\xff' for y in range(240) for x in range(320))
        def ffmpeg(data, *args):
            return subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-filter_threads', '1',
                '-i', 'pipe:0', *args, '-threads', '1', 'pipe:1'], input=data, capture_output=True,
                check=True, timeout=5).stdout
        raw = await asyncio.to_thread(ffmpeg, b'P6\n320 240\n255\n' + pixels,
                                      '-frames:v', '1', '-c:v', 'mjpeg', '-f', 'image2pipe')
        s = vision.settings(config.Config(vision_width=320, vision_height=240))
        app = vision_app.Companion(s)
        output = await app.image(raw, None)
        rgb = await asyncio.to_thread(ffmpeg, output, '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo')
        self.assertEqual(len(rgb), 224 * 168 * 3)
        self.assertLess(max(rgb[0::3]), 30)
        self.assertGreater(min(rgb[2::3]), 200)
        preview = await asyncio.to_thread(ffmpeg, raw, '-vf', ','.join(vision.image_filters(s, preview=True)),
                                         '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo')
        self.assertEqual(len(preview), len(rgb))
        self.assertLess(max(preview[0::3]), 30)
        region = await app.image(raw, [0, 0, 500, 500])
        small = await asyncio.to_thread(ffmpeg, region, '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo')
        self.assertEqual(len(small), 112 * 84 * 3)
        app.settings.update(crop_percent=0, sharpen=0)
        self.assertIs(await app.image(raw, None), raw)

    async def test_isolated_bad_jpeg_is_dropped_but_repeated_corruption_stops(self):
        camera = vision_app.Camera(vision.settings(config.Config()), mock.Mock())
        camera.capture = mock.Mock(returncode=None)
        clock = [0.0]
        fake_time = mock.Mock(wraps=time)
        fake_time.monotonic.side_effect = lambda: clock[0]
        async def frames(reader):
            await asyncio.sleep(0)
            clock[0] += .1
            value = next(values)
            if isinstance(value, Exception): raise value
            return value
        bad = vision_app.InvalidJPEGFrame('incomplete JPEG')
        values = iter([bad, b'good', bad, b'latest-good'] + [bad] * 20)
        with mock.patch.object(vision_app, 'time', fake_time), \
             mock.patch.object(vision_app, 'read_frame', side_effect=frames):
            await camera._read()
        self.assertEqual(camera.frame, b'latest-good')
        self.assertEqual(camera.sequence, 1)
        self.assertGreaterEqual(camera.dropped_frames, 12)
        self.assertIn('frame budget', camera.failure)

    async def test_empty_record_preserves_framing_for_next_valid_image(self):
        frame = b'\xff\xd8main\xff\xd9'
        reader = asyncio.StreamReader()
        reader.feed_data(b'--ffmpeg\r\nContent-length: 0\r\n\r\n\r\n--ffmpeg\r\nContent-length: ' +
                         str(len(frame)).encode() + b'\r\n\r\n' + frame)
        with self.assertRaises(vision_app.InvalidJPEGFrame):
            await vision_app.read_frame(reader)
        self.assertEqual(await vision_app.read_frame(reader), frame)

    async def test_bad_frame_logs_specific_failure_without_frame_content(self):
        report = mock.Mock()
        camera = vision_app.Camera(vision.settings(config.Config()), report)
        camera.capture = mock.Mock(returncode=None)
        with mock.patch.object(vision_app, 'read_frame', side_effect=RuntimeError('Camera did not deliver a complete JPEG')):
            await camera._read()
        self.assertIn('complete JPEG', camera.failure)
        self.assertEqual(report.call_args.args[0], 'vision_capture_error')
        self.assertNotIn('frame', report.call_args.kwargs)

    async def test_stderr_retention_is_bounded(self):
        camera = vision_app.Camera(vision.settings(config.Config()))
        stream = asyncio.StreamReader()
        stream.feed_data(b'x' * 10000 + b'USB disconnected')
        stream.feed_eof()
        await camera._stderr('capture', stream)
        self.assertEqual(len(camera.stderr['capture']), 2048)
        self.assertTrue(camera.stderr['capture'].endswith('USB disconnected'))

    async def test_inspection_error_logs_stage_without_question(self):
        report = mock.Mock()
        app = vision_app.Companion(vision.settings(config.Config()), report)
        with mock.patch.object(app, 'start', side_effect=RuntimeError('missing device')):
            with self.assertRaises(RuntimeError):
                await app.inspect('private question', None, 'test', 0)
        event = report.call_args
        self.assertEqual(event.args[0], 'vision_inspect_error')
        self.assertEqual(event.kwargs['stage'], 'start')
        self.assertNotIn('private question', str(report.call_args_list))

    async def test_live_voice_and_backend_both_know_camera_routing(self):
        from omarchy_voice import live
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, "STATE_DIR", Path(tmp)), \
             mock.patch.object(live.capabilities, "manifest", return_value="fixture"), \
             mock.patch.object(live.capabilities, "live_state", return_value="fixture"), \
             mock.patch.object(live.capabilities, "installed_apps", return_value="fixture"):
            session = live.LiveSession(config.Config(dry_run=True, notify=False))
            session._perf = mock.Mock()
            payload = (await session._session_start())["session"]
            self.assertIn("look at this", payload["instructions"])
            backend = payload["delegation"]["responses"]
            self.assertIn("camera_view", {tool["name"] for tool in backend["tools"]})
            session.config.vision_enabled = False
            payload = (await session._session_start())["session"]
            self.assertNotIn(vision.VOICE_ROUTING, payload["instructions"])
            self.assertNotIn("camera_view", {tool["name"] for tool in payload["delegation"]["responses"]["tools"]})

    async def test_multipart_frames_use_lengths_not_embedded_jpeg_markers(self):
        frame = b"\xff\xd8thumbnail\xff\xd9still-main-frame\xff\xd9"
        reader = asyncio.StreamReader()
        reader.feed_data(b"--fixture\r\nContent-type: image/jpeg\r\nContent-length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame)
        self.assertEqual(await vision_app.read_frame(reader), frame)

    async def test_inspect_waits_for_new_frame(self):
        camera = vision_app.Camera(vision.settings(config.Config()))
        camera.capture = mock.Mock(returncode=None)
        camera.preview = mock.Mock(returncode=None)
        camera.frame = b"old"
        task = asyncio.create_task(camera.fresh())
        await asyncio.sleep(.025)
        self.assertFalse(task.done())
        camera.frame = b"new"
        camera.frame_wall = time.time()
        camera.sequence += 1
        raw, _, _ = await task
        self.assertEqual(raw, b"new")

    async def test_locked_session_never_starts_capture(self):
        app = vision_app.Companion(vision.settings(config.Config()))
        with mock.patch.object(vision_app, "locked", return_value=True), \
             mock.patch.object(app.camera, "start") as start:
            with self.assertRaisesRegex(RuntimeError, "locked"):
                await app.start("test", 0)
            start.assert_not_called()

    async def test_lock_check_failure_stops_active_capture(self):
        for error in (RuntimeError("Invalid lock response"), TimeoutError(), FileNotFoundError()):
            with self.subTest(error=type(error).__name__):
                app = vision_app.Companion(vision.settings(config.Config()))
                app.camera.capture = mock.Mock(returncode=None)
                app.camera.preview = mock.Mock(returncode=None)
                app.camera.frame_at = time.monotonic()
                app.previous = "Previous observation"
                with mock.patch.object(vision_app, "locked", side_effect=error), \
                     mock.patch.object(vision_app.asyncio, "sleep", new_callable=mock.AsyncMock), \
                     mock.patch.object(app.camera, "stop", side_effect=app.done.set) as stop:
                    await app.monitor()
                    stop.assert_awaited_once()
                self.assertEqual(app.reason, "lock state unavailable")
                self.assertEqual(app.previous, "")

    async def test_stop_during_camera_start_cannot_revive_capture(self):
        app = vision_app.Companion(vision.settings(config.Config()))
        entered, release = asyncio.Event(), asyncio.Event()
        async def start():
            entered.set()
            await release.wait()
        with mock.patch.object(vision_app, "locked", return_value=False), \
             mock.patch.object(app.camera, "start", side_effect=start), \
             mock.patch.object(app.camera, "stop") as stop:
            opening = asyncio.create_task(app.start("voice", 0))
            await entered.wait()
            stopping = asyncio.create_task(app.stop())
            await asyncio.sleep(0)
            release.set()
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                await opening
            await stopping
            self.assertTrue(stop.called)
            self.assertIsNone(app.camera.frame)

    async def test_busy_inspection_does_not_queue_or_open_another_request(self):
        app = vision_app.Companion(vision.settings(config.Config()))
        app.busy = True
        with mock.patch.object(app, "start") as start:
            with self.assertRaisesRegex(RuntimeError, "not queued"):
                await app.inspect("another", None, "test", 0)
            start.assert_not_called()

    async def test_stale_inference_is_discarded_and_worker_terminated(self):
        app = vision_app.Companion(vision.settings(config.Config()))
        process = mock.Mock(returncode=None)
        async def communicate(data):
            app.epoch += 1  # Simulate stop during provider request.
            return json.dumps({"ok": True, "observation": "stale"}).encode(), None
        process.communicate = mock.AsyncMock(side_effect=communicate)
        with mock.patch.object(app, "start"), \
             mock.patch.object(app, "image", return_value=b"jpeg"), \
             mock.patch.object(app.camera, "fresh", return_value=(b"jpeg", time.time(), 1)), \
             mock.patch.object(vision_app.Camera, "active", new_callable=mock.PropertyMock, return_value=True), \
             mock.patch.object(vision_app.asyncio, "create_subprocess_exec", return_value=process), \
             mock.patch.object(vision_app, "terminate") as terminate:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                await app.inspect("question", None, "test", 0)
            self.assertEqual(app.previous, "")
            self.assertFalse(app.busy)
            terminate.assert_awaited_with(process)

    async def test_private_socket_roundtrip_without_camera_or_api(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, "RUNTIME_DIR", Path(tmp) / "run"):
            directory = vision.secure_runtime()
            app = vision_app.Companion(vision.settings(config.Config()))
            server = await asyncio.start_unix_server(app.handle, path=str(directory / "control.sock"), limit=8192)
            try:
                result = await asyncio.to_thread(vision.rpc, {"action": "status"})
                self.assertFalse(result["active"])
                with self.assertRaisesRegex(RuntimeError, "Unknown"):
                    await asyncio.to_thread(vision.rpc, {"action": "status", "base_url": "https://other.example"})
            finally:
                server.close()
                await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
