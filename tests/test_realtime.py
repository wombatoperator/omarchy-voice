"""The realtime engine's own safety-critical parts.

Two things are worth testing without a websocket or a microphone: that the
tool schemas convert to the shape the Realtime API wants, and that the
spoken-confirmation gate cannot be talked through. In realtime the user's
"yes, do it" goes to the model as audio and never reaches this process, so
the model *reports* the confirmation and this code has to be the thing that
judges it.

Run with: python3 -m unittest discover -s tests
"""

import asyncio
import json
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from omarchy_voice import feedback, realtime
from omarchy_voice.config import Config
from omarchy_voice.tools import TOOL_SCHEMAS


class FakeSocket:
    """Records what would have gone over the wire."""

    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def events(self, kind):
        return [e for e in self.sent if e.get("type") == kind]


class ToolConversionTests(unittest.TestCase):
    def setUp(self):
        self.tools = realtime.to_realtime_tools()
        self.by_name = {t["name"]: t for t in self.tools}

    def test_every_executor_tool_survives(self):
        for schema in TOOL_SCHEMAS:
            with self.subTest(tool=schema["name"]):
                converted = self.by_name[schema["name"]]
                self.assertEqual(converted["type"], "function")
                self.assertEqual(converted["parameters"], schema["input_schema"])
                self.assertEqual(converted["description"], schema["description"])

    def test_internal_schema_keys_are_dropped(self):
        for tool in self.tools:
            with self.subTest(tool=tool["name"]):
                self.assertNotIn("input_schema", tool)
                self.assertNotIn("strict", tool)
                self.assertEqual(set(tool), {"type", "name", "description", "parameters"})

    def test_gate_tools_are_realtime_only(self):
        self.assertIn("confirm_last", self.by_name)
        self.assertIn("cancel_last", self.by_name)
        self.assertNotIn("confirm_last", {s["name"] for s in TOOL_SCHEMAS})
        self.assertNotIn("cancel_last", {s["name"] for s in TOOL_SCHEMAS})

    def test_json_serialisable(self):
        json.dumps(self.tools)  # must not raise


class RealtimeSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for name, value in (("LOG_FILE", root / "session.log"),
                            ("STATE_FILE", root / "state.json"),
                            ("STATE_DIR", root),
                            ("RUNTIME_DIR", root)):
            patcher = mock.patch.object(feedback, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

        self.config = Config(dry_run=True, notify=False)
        self.session = realtime.RealtimeSession(self.config)
        self.socket = FakeSocket()
        self.session.ws = self.socket

    def hold_a_reboot(self):
        """Put a confirm-gated action into the pending slot, the real way."""
        result = self.session.executor.call("omarchy_cli", {"command": "reboot"})
        self.assertFalse(result.ok)
        self.assertIsNotNone(self.session.executor.pending)

    async def test_wrong_phrase_does_not_release_the_gate(self):
        self.hold_a_reboot()
        for phrase in ["they said yes", "the user confirmed", "ok", "do it", ""]:
            with self.subTest(phrase=phrase):
                output = await self.session._dispatch("confirm_last",
                                                      {"heard_phrase": phrase})
                self.assertTrue(output.startswith("ERROR:"), output)
                self.assertIsNotNone(self.session.executor.pending)

    async def test_a_real_confirmation_phrase_releases_it(self):
        self.hold_a_reboot()
        output = await self.session._dispatch("confirm_last", {"heard_phrase": "confirm"})
        self.assertFalse(output.startswith("ERROR:"), output)
        self.assertIsNone(self.session.executor.pending)
        self.assertIn("CONFIRM omarchy reboot", "\n".join(self.session.executor.transcript))

    async def test_confirmation_phrase_inside_a_sentence_counts(self):
        self.hold_a_reboot()
        output = await self.session._dispatch("confirm_last",
                                              {"heard_phrase": "yes do it please"})
        self.assertFalse(output.startswith("ERROR:"), output)
        self.assertIsNone(self.session.executor.pending)

    async def test_dont_confirm_does_not_release_the_gate(self):
        self.hold_a_reboot()
        output = await self.session._dispatch("confirm_last",
                                              {"heard_phrase": "don't confirm"})
        self.assertTrue(output.startswith("ERROR:"), output)
        self.assertIsNotNone(self.session.executor.pending)

    async def test_confirming_nothing_is_an_error(self):
        output = await self.session._dispatch("confirm_last", {"heard_phrase": "confirm"})
        self.assertTrue(output.startswith("ERROR:"), output)

    async def test_cancel_drops_the_held_action(self):
        self.hold_a_reboot()
        output = await self.session._dispatch("cancel_last", {})
        self.assertIn("Cancelled", output)
        self.assertIsNone(self.session.executor.pending)
        output = await self.session._dispatch("confirm_last", {"heard_phrase": "confirm"})
        self.assertTrue(output.startswith("ERROR:"), output)

    async def test_denied_actions_are_still_denied(self):
        output = await self.session._dispatch("run_shell", {"command": "sudo rm -rf /"})
        self.assertTrue(output.startswith("ERROR:"), output)
        self.assertIsNone(self.session.executor.pending)

    async def test_function_call_is_answered_and_a_reply_requested(self):
        await self.session._on_response_done({
            "type": "response.done",
            "response": {"status": "completed", "output": [
                {"type": "function_call", "name": "hypr_dispatch",
                 "call_id": "call_abc",
                 "arguments": json.dumps({"lua": 'hl.dsp.focus({ workspace = "3" })'})},
            ]},
        })
        outputs = self.socket.events("conversation.item.create")
        self.assertEqual(len(outputs), 1)
        item = outputs[0]["item"]
        self.assertEqual(item["type"], "function_call_output")
        self.assertEqual(item["call_id"], "call_abc")
        self.assertIn("dry-run", item["output"])
        self.assertIsInstance(item["output"], str)
        self.assertEqual(len(self.socket.events("response.create")), 1)

    async def test_same_batch_confirm_last_does_not_release_the_gate(self):
        await self.session._on_response_done({
            "type": "response.done",
            "response": {"status": "completed", "output": [
                {"type": "function_call", "name": "omarchy_cli",
                 "call_id": "call_hold",
                 "arguments": json.dumps({"command": "reboot"})},
                {"type": "function_call", "name": "confirm_last",
                 "call_id": "call_yes",
                 "arguments": json.dumps({"heard_phrase": "confirm"})},
            ]},
        })
        self.assertIsNotNone(self.session.executor.pending)
        outputs = [e["item"]["output"] for e in self.socket.events("conversation.item.create")]
        self.assertTrue(any("new user turn" in o for o in outputs), outputs)

    async def test_cancelled_response_does_not_dispatch_tools(self):
        await self.session._on_response_done({
            "type": "response.done",
            "response": {"status": "cancelled", "output": [
                {"type": "function_call", "name": "hypr_dispatch",
                 "call_id": "call_int",
                 "arguments": json.dumps({"lua": 'hl.dsp.focus({ workspace = "9" })'})},
            ]},
        })
        self.assertEqual(self.socket.events("conversation.item.create"), [])
        self.assertEqual(self.session.executor.transcript, [])

    async def test_unparseable_arguments_report_back_instead_of_crashing(self):
        await self.session._on_response_done({
            "type": "response.done",
            "response": {"status": "completed", "output": [
                {"type": "function_call", "name": "hypr_dispatch",
                 "call_id": "call_bad", "arguments": "{not json"},
            ]},
        })
        item = self.socket.events("conversation.item.create")[0]["item"]
        self.assertTrue(item["output"].startswith("ERROR:"), item["output"])

    async def test_a_plain_spoken_reply_sends_nothing_back(self):
        await self.session._on_response_done({
            "type": "response.done",
            "response": {"status": "completed",
                         "output": [{"type": "message", "role": "assistant"}]},
        })
        self.assertEqual(self.socket.sent, [])

    async def test_muting_stops_capture_rather_than_ignoring_it(self):
        await self.session._set_active(True)
        self.assertTrue(self.session._active_event.is_set())
        await self.session._set_active(False)
        self.assertFalse(self.session._active_event.is_set())
        self.assertFalse(self.session.active)

    async def test_manual_vad_commits_on_mute(self):
        self.session.config.realtime_turn_detection = "none"
        self.session._appended_audio = True
        await self.session._set_active(False)
        kinds = [e.get("type") for e in self.socket.sent]
        self.assertIn("input_audio_buffer.commit", kinds)
        self.assertIn("response.create", kinds)

    async def test_barge_in_cancels_and_drops_old_audio(self):
        self.session._audio_item_id = "item_1"
        self.session._audio_response_id = "resp_old"
        self.session._audio_bytes = 4800 * 4
        await self.session._on_event({"type": "input_audio_buffer.speech_started"})
        kinds = [e.get("type") for e in self.socket.sent]
        self.assertIn("response.cancel", kinds)
        self.assertIn("conversation.item.truncate", kinds)
        await self.session._on_event({
            "type": "response.output_audio.delta",
            "response_id": "resp_old",
            "delta": "AAAA",
        })
        # Ignored leftover delta must not have been written as a new speaker.
        self.assertIsNone(self.session.speaker._proc)

    async def test_barge_in_with_nothing_running_sends_no_cancel(self):
        """Speech between turns must not ask to cancel a response that is not there.

        A live session logged "response_cancel_not_active: no active response
        found" on every toggle-on, because barge-in fired response.cancel
        unconditionally.
        """
        self.session._audio_item_id = None
        self.session._audio_response_id = None
        self.session._response_running = False
        await self.session._on_event({"type": "input_audio_buffer.speech_started"})
        kinds = [e.get("type") for e in self.socket.sent]
        self.assertNotIn("response.cancel", kinds)
        self.assertNotIn("conversation.item.truncate", kinds)

    async def test_audio_bytes_reset_between_items(self):
        """Truncation is an offset into one item, so bytes cannot accumulate.

        Carrying a running total across items asked the server to cut past the
        end of what it held: "Audio content of 1850ms is already shorter than
        4850ms".
        """
        for item in ("item_1", "item_1", "item_2"):
            await self.session._on_event({
                "type": "response.output_audio.delta",
                "response_id": "resp_1", "item_id": item, "delta": "AAAA",
            })
        self.assertEqual(self.session._audio_item_id, "item_2")
        self.assertEqual(self.session._audio_bytes, 3)

    async def test_tool_loop_stops_at_max_turns(self):
        """The model must not be able to drive itself indefinitely.

        Each tool round ends by asking for another response, so a model that
        keeps retrying a failing call loops forever. A live session opened
        terminals every ~30 s, minutes after listening was switched off.
        """
        self.session.config.max_turns = 3
        self.session._tool_rounds = 0
        for _ in range(6):
            await self.session._on_event({
                "type": "response.done",
                "response": {"status": "completed", "output": [
                    {"type": "function_call", "name": "hypr_query",
                     "call_id": "c1", "arguments": '{"kind": "workspaces"}'},
                ]},
            })
        self.assertEqual(len(self.socket.events("response.create")), 2)

    async def test_a_new_user_turn_refills_the_budget(self):
        self.session.config.max_turns = 3
        self.session._tool_rounds = 3
        await self.session._on_event({"type": "input_audio_buffer.speech_started"})
        self.assertEqual(self.session._tool_rounds, 0)

    async def test_playback_never_blocks_the_caller(self):
        """Audio must not stall the loop that reads the websocket.

        pw-cat consumes at real time while the model sends far faster, so
        draining its pipe inline blocked event handling — tool calls included —
        for the length of the spoken reply. That was the "hangs after I talk to
        it" symptom.
        """
        from omarchy_voice.realtime import Speaker
        speaker = Speaker(24000)
        chunk = b"\x00\x01" * 2400          # 0.2 s of PCM16
        started = time.monotonic()
        for _ in range(25):                  # 5 s of audio
            await speaker.write(chunk)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(speaker._queue.qsize(), 25)
        await speaker.interrupt()
        self.assertEqual(speaker._queue.qsize(), 0)
        self.assertIsNone(speaker._proc)

    async def test_typed_turn_is_injected_as_a_user_message(self):
        self.assertEqual(await self.session._inject("switch to workspace four"), "sent")
        # A desktop snapshot is sent ahead of it, so pick the user turn out
        # rather than assuming it is the first item on the wire.
        items = [e["item"] for e in self.socket.events("conversation.item.create")]
        user = [i for i in items if i["role"] == "user"]
        self.assertEqual(len(user), 1)
        item = user[0]
        self.assertEqual(item["content"][0]["text"], "switch to workspace four")
        self.assertEqual(len(self.socket.events("response.create")), 1)
        self.assertTrue(self.session._user_turn_since_hold)

    async def test_local_confirm_releases_without_a_model_phrase(self):
        self.hold_a_reboot()
        output = await self.session._local_confirm()
        self.assertFalse(output.startswith("ERROR:"), output)
        self.assertIsNone(self.session.executor.pending)


class SessionUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_detection_can_be_switched_off(self):
        session = realtime.RealtimeSession(Config(realtime_turn_detection="none"))
        self.assertIsNone(session._turn_detection())
        session = realtime.RealtimeSession(Config(realtime_turn_detection="server_vad"))
        self.assertEqual(session._turn_detection(), {"type": "server_vad"})

class DeadResponseTests(unittest.IsolatedAsyncioTestCase):
    """A turn that produces nothing must not look like not being heard.

    The session log has stretches of `heard ...` with no reply and no action —
    the user asking to switch workspace four times running and getting silence
    each time. Those were `response.done` events with status `failed`, which
    the engine dropped without a log line, a notification, or a word.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for name, value in (("LOG_FILE", root / "session.log"),
                            ("STATE_FILE", root / "state.json"),
                            ("STATE_DIR", root),
                            ("RUNTIME_DIR", root)):
            patcher = mock.patch.object(feedback, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(dry_run=True, notify=False)
        self.session = realtime.RealtimeSession(self.config)
        self.socket = FakeSocket()
        self.session.ws = self.socket
        self.log = root / "session.log"

    def rate_limited(self, message="Rate limit reached. Please try again in 10ms."):
        return {"type": "response.done", "response": {
            "status": "failed",
            "status_details": {"type": "failed", "error": {
                "type": "tokens", "code": "rate_limit_exceeded", "message": message}}}}

    async def test_a_failed_response_is_logged(self):
        await self.session._on_response_done(self.rate_limited())
        self.assertIn("rate_limit_exceeded", self.log.read_text())

    async def test_a_rate_limited_turn_is_retried(self):
        await self.session._on_response_done(self.rate_limited())
        self.assertEqual(len(self.socket.events("response.create")), 1)

    async def test_retries_are_capped_then_the_user_is_told(self):
        for _ in range(realtime.RATE_LIMIT_RETRIES + 1):
            await self.session._on_response_done(self.rate_limited())
        self.assertEqual(len(self.socket.events("response.create")),
                         realtime.RATE_LIMIT_RETRIES)
        self.assertIn("rate limit", self.log.read_text().lower())

    async def test_a_new_user_turn_refills_the_retry_budget(self):
        for _ in range(realtime.RATE_LIMIT_RETRIES + 1):
            await self.session._on_response_done(self.rate_limited())
        await self.session._on_event({"type": "input_audio_buffer.speech_started"})
        self.assertEqual(self.session._rate_limit_retries, 0)

    async def test_a_non_rate_limit_failure_is_not_retried(self):
        await self.session._on_response_done({
            "type": "response.done",
            "response": {"status": "failed", "status_details": {
                "error": {"code": "server_error", "message": "boom"}}}})
        self.assertEqual(self.socket.events("response.create"), [])
        self.assertIn("server_error", self.log.read_text())

    async def test_a_completed_response_reports_nothing(self):
        await self.session._on_response_done(
            {"type": "response.done", "response": {"status": "completed", "output": []}})
        # Nothing logged at all: the file is only created on the first write.
        self.assertNotIn("error", self.log.read_text() if self.log.exists() else "")


class RetryAfterTests(unittest.TestCase):
    def test_milliseconds(self):
        self.assertAlmostEqual(realtime._retry_after("try again in 480ms"), 0.68, places=2)

    def test_seconds(self):
        self.assertAlmostEqual(realtime._retry_after("Please try again in 1.5s."), 1.7, places=2)

    def test_a_long_wait_is_clamped(self):
        self.assertEqual(realtime._retry_after("try again in 600s"), 8.0)

    def test_no_figure_falls_back(self):
        self.assertEqual(realtime._retry_after("rate limited"), realtime.RATE_LIMIT_PAUSE)


class StateRefreshTests(unittest.IsolatedAsyncioTestCase):
    """The desktop snapshot must not rewrite the cached instructions.

    Instructions are the cached prefix: persona, the capability manifest, the
    tool schemas. Rewriting them to carry a window list meant re-prefilling
    about 9k unchanged tokens on every single turn.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for name, value in (("LOG_FILE", root / "session.log"),
                            ("STATE_FILE", root / "state.json"),
                            ("STATE_DIR", root),
                            ("RUNTIME_DIR", root)):
            patcher = mock.patch.object(feedback, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.session = realtime.RealtimeSession(Config(dry_run=True, notify=False))
        self.socket = FakeSocket()
        self.session.ws = self.socket

    async def refresh(self):
        with mock.patch.object(realtime.capabilities, "live_state",
                               return_value="Workspaces in use: 1 (2 windows)"):
            self.session._state_refreshed = 0.0
            await self.session._refresh_state()

    async def test_the_snapshot_is_appended_not_written_into_instructions(self):
        await self.refresh()
        self.assertEqual(self.socket.events("session.update"), [])
        items = self.socket.events("conversation.item.create")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["item"]["role"], "system")

    async def test_the_snapshot_carries_the_live_state(self):
        await self.refresh()
        text = self.socket.events("conversation.item.create")[0]["item"]["content"][0]["text"]
        self.assertIn("Workspaces in use: 1 (2 windows)", text)

    async def test_a_typed_turn_refreshes_even_inside_the_interval(self):
        # `_instructions` stamps the clock at session start, so without the
        # force a turn in the first few seconds got no snapshot at all.
        self.session._state_refreshed = 1e18  # as if refreshed this instant
        with mock.patch.object(realtime.capabilities, "live_state", return_value="live"):
            await self.session._inject("what is open?")
        roles = [e["item"]["role"] for e in self.socket.events("conversation.item.create")]
        self.assertIn("system", roles)

    async def test_refreshes_are_rate_limited(self):
        await self.refresh()
        await self.session._refresh_state()  # straight away: too soon
        self.assertEqual(len(self.socket.events("conversation.item.create")), 1)

    async def test_the_previous_snapshot_is_deleted_not_stacked(self):
        await self.refresh()
        await self.refresh()
        creates = self.socket.events("conversation.item.create")
        deletes = self.socket.events("conversation.item.delete")
        self.assertEqual(len(creates), 2)
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0]["item_id"], creates[0]["item"]["id"])

    async def test_a_typed_turn_gets_a_snapshot_too(self):
        with mock.patch.object(realtime.capabilities, "live_state",
                               return_value="Workspaces in use: 1 (2 windows)"):
            await self.session._inject("what is open?")
        items = self.socket.events("conversation.item.create")
        # snapshot first, then the user's words — order matters, the model reads
        # the desktop as context for the sentence rather than after it.
        self.assertEqual(items[0]["item"]["role"], "system")
        self.assertIn("Workspaces in use", items[0]["item"]["content"][0]["text"])
        self.assertEqual(items[1]["item"]["role"], "user")

    async def test_the_first_snapshot_deletes_nothing(self):
        await self.refresh()
        self.assertEqual(self.socket.events("conversation.item.delete"), [])

    async def test_the_manifest_is_not_rebuilt_per_turn(self):
        with mock.patch.object(realtime.capabilities, "manifest") as manifest:
            await self.refresh()
        manifest.assert_not_called()

class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    """A dropped websocket used to end the run, exit 0, and leave systemd
    thinking the service was healthy while the microphone key did nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for name, value in (("LOG_FILE", root / "session.log"),
                            ("STATE_FILE", root / "state.json"),
                            ("STATE_DIR", root), ("RUNTIME_DIR", root)):
            patcher = mock.patch.object(feedback, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.session = realtime.RealtimeSession(Config(dry_run=True, notify=False))
        self.session.ws = FakeSocket()

    async def test_a_send_failure_marks_the_connection_dropped(self):
        class Dead:
            async def send(self, raw):
                raise ConnectionError("keepalive ping timeout")
        self.session.ws = Dead()
        await self.session._send({"type": "ping"})
        self.assertTrue(self.session._dropped)
        self.assertEqual(self.session._exit_code, 1)
        self.assertTrue(self.session._stop.is_set())

    async def test_a_user_quit_is_not_a_drop(self):
        # _control hands work to the loop, so it needs one.
        self.session.loop = asyncio.get_running_loop()
        self.session._control("quit")
        self.assertFalse(self.session._dropped)
        self.assertTrue(self.session._user_quit)

    async def test_serve_reconnects_after_a_drop_then_stops_on_quit(self):
        attempts = []

        async def fake_session(url, headers):
            attempts.append(1)
            if len(attempts) < 3:
                self.session._dropped = True     # socket died
            else:
                self.session._user_quit = True   # user asked to stop
        with mock.patch.object(self.session, "_open_one", side_effect=fake_session), \
             mock.patch.object(realtime, "RECONNECT_BASE_DELAY", 0.01), \
             mock.patch.object(realtime, "RECONNECT_MAX_DELAY", 0.01):
            await self.session._serve("wss://x", {})
        self.assertEqual(len(attempts), 3)

    async def test_serve_gives_up_and_fails_after_the_cap(self):
        async def always_drops(url, headers):
            self.session._dropped = True
        with mock.patch.object(self.session, "_open_one", side_effect=always_drops), \
             mock.patch.object(realtime, "RECONNECT_ATTEMPTS", 2), \
             mock.patch.object(realtime, "RECONNECT_BASE_DELAY", 0.01), \
             mock.patch.object(realtime, "RECONNECT_MAX_DELAY", 0.01):
            await self.session._serve("wss://x", {})
        # Non-zero so Restart=on-failure gets its turn.
        self.assertEqual(self.session._exit_code, 1)
        self.assertIn("gave up", self.tmp.name and
                      (Path(self.tmp.name) / "session.log").read_text())

    async def test_listening_survives_a_reconnect(self):
        self.session.active = True
        self.session._active_event.set()
        calls = []

        async def drop_once(url, headers):
            calls.append(1)
            if len(calls) == 1:
                self.session._dropped = True
            else:
                self.session._user_quit = True
        with mock.patch.object(self.session, "_open_one", side_effect=drop_once), \
             mock.patch.object(realtime, "RECONNECT_BASE_DELAY", 0.01), \
             mock.patch.object(realtime, "RECONNECT_MAX_DELAY", 0.01):
            await self.session._serve("wss://x", {})
        # It went back to listening rather than coming up muted.
        self.assertTrue(self.session.active)



if __name__ == "__main__":
    unittest.main()
