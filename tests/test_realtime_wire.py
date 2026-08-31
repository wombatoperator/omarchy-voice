"""One end-to-end pass over the realtime wire protocol, against a fake server.

This is the only test that exercises `RealtimeSession.run` itself — the connect,
the opening `session.update`, the function-call round trip, and a clean shutdown
through the control socket. It needs the `websockets` package; without it the
whole module skips, so the suite still runs green on a machine that only uses
a machine without python-websockets.

No audio is involved: the session starts muted (mode is not "always"), so
pw-record is never spawned.

Run with: python3 -m unittest discover -s tests
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:
    from websockets.asyncio.server import serve
except ImportError:  # pragma: no cover - depends on the machine
    serve = None

from omarchy_voice import feedback, realtime, session as session_mod
from omarchy_voice.config import Config


class FakeRealtimeServer:
    """The smallest server that looks like the Realtime API from the client's side."""

    def __init__(self):
        self.received: list[dict] = []
        self.session_update = asyncio.Event()
        self.tool_answered = asyncio.Event()
        self.port = 0
        self._server = None

    async def start(self):
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, ws):
        await ws.send(json.dumps({"type": "session.created",
                                  "session": {"id": "sess_test"}}))
        async for raw in ws:
            event = json.loads(raw)
            self.received.append(event)
            kind = event.get("type")
            if kind == "session.update":
                self.session_update.set()
                # A turn the model decided needs a tool call.
                await ws.send(json.dumps({
                    "type": "response.done",
                    "response": {"status": "completed", "output": [{
                        "type": "function_call",
                        "name": "hypr_dispatch",
                        "call_id": "call_wire",
                        "arguments": json.dumps(
                            {"lua": 'hl.dsp.focus({ workspace = "3" })'}),
                    }]},
                }))
            elif kind == "response.create":
                await ws.send(json.dumps({
                    "type": "response.output_audio_transcript.done",
                    "transcript": "Switched to workspace 3.",
                }))
                self.tool_answered.set()

    def of_type(self, kind):
        return [e for e in self.received if e.get("type") == kind]


@unittest.skipIf(serve is None, "websockets is not installed")
class WireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for module, names in ((feedback, ("LOG_FILE", "STATE_FILE", "STATE_DIR", "RUNTIME_DIR")),
                              (session_mod, ("SOCKET_PATH", "RUNTIME_DIR")),
                              (realtime, ("SAFETY_ID_FILE", "CONFIG_DIR"))):
            for name in names:
                value = root / ("session.log" if name == "LOG_FILE" else
                                "state.json" if name == "STATE_FILE" else
                                "control.sock" if name == "SOCKET_PATH" else
                                "safety-id" if name == "SAFETY_ID_FILE" else "")
                patcher = mock.patch.object(module, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)

        self.server = FakeRealtimeServer()
        await self.server.start()
        patcher = mock.patch.object(
            realtime, "REALTIME_URL", f"ws://127.0.0.1:{self.server.port}/v1/realtime")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.server.stop()

    async def test_one_full_round_trip(self):
        config = Config(dry_run=True, notify=False)
        session = realtime.RealtimeSession(config)
        runner = asyncio.create_task(session.run())

        await asyncio.wait_for(self.server.tool_answered.wait(), timeout=30)
        session._user_quit = True
        session._stop.set()
        self.assertEqual(await asyncio.wait_for(runner, timeout=10), 0)

        # 1. The session was configured before anything else was sent.
        update = self.server.of_type("session.update")[0]["session"]
        self.assertEqual(update["type"], "realtime")
        self.assertEqual(update["model"], config.realtime_model)
        self.assertEqual(update["output_modalities"], ["audio"])
        self.assertEqual(update["audio"]["input"]["format"],
                         {"type": "audio/pcm", "rate": config.realtime_sample_rate})
        self.assertEqual(update["audio"]["input"]["turn_detection"],
                         {"type": "semantic_vad"})
        self.assertEqual(update["audio"]["output"]["voice"], config.realtime_voice)
        self.assertIn("confirm_last", {t["name"] for t in update["tools"]})
        self.assertIn("You are the voice control layer", update["instructions"])

        # 2. The tool call was answered, and a spoken reply asked for.
        answer = self.server.of_type("conversation.item.create")[0]["item"]
        self.assertEqual(answer["call_id"], "call_wire")
        self.assertIn("dry-run", answer["output"])
        self.assertTrue(self.server.of_type("response.create"))

        # 3. Muted throughout: no audio was ever appended.
        self.assertEqual(self.server.of_type("input_audio_buffer.append"), [])

    async def test_control_socket_toggles_and_quits(self):
        config = Config(dry_run=True, notify=False)
        session = realtime.RealtimeSession(config)
        runner = asyncio.create_task(session.run())
        await asyncio.wait_for(self.server.session_update.wait(), timeout=30)

        self.assertTrue(session_mod.daemon_running())

        # A real toggle would spawn pw-record, so the capture step is stubbed —
        # what is under test is that a socket command reaches the event loop.
        async def fake_set_active(active):
            return f"toggled {active}"

        with mock.patch.object(session, "_set_active", fake_set_active):
            self.assertEqual(await asyncio.to_thread(session_mod.send_control, "toggle"),
                             "toggled True")
        self.assertEqual(await asyncio.to_thread(session_mod.send_control, "quit"),
                         "stopping")
        self.assertEqual(await asyncio.wait_for(runner, timeout=10), 0)
        self.assertFalse(session_mod.daemon_running())


if __name__ == "__main__":
    unittest.main()
