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

    # -- the budget is consecutive failures, not a lifetime allowance --------
    #
    # Scaled down rather than clock-faked: time.monotonic is the clock the
    # event loop schedules against, so patching it means asyncio.sleep never
    # comes back and the suite hangs. A "healthy" session here outlives a 20 ms
    # threshold; a flapping one returns at once. Same semantics, and the test
    # finishes.

    HEALTHY = 0.02

    async def serve_with(self, open_one, attempts=3):
        with mock.patch.object(self.session, "_open_one", side_effect=open_one), \
             mock.patch.object(realtime, "RECONNECT_HEALTHY_SECONDS", self.HEALTHY), \
             mock.patch.object(realtime, "RECONNECT_ATTEMPTS", attempts), \
             mock.patch.object(realtime, "RECONNECT_BASE_DELAY", 0.001), \
             mock.patch.object(realtime, "RECONNECT_MAX_DELAY", 0.001):
            await self.session._serve("wss://x", {})
        return (Path(self.tmp.name) / "session.log").read_text()

    async def test_session_expiry_does_not_spend_the_budget(self):
        """The server caps a realtime session at 60 minutes, so EVERY
        connection ends by dropping — the healthy ones included. Counting
        those against the cap killed this daemon after six good hours."""
        sessions = []

        async def serve_then_expire(url, headers):
            sessions.append(1)
            await asyncio.sleep(self.HEALTHY * 2)     # a full, useful session
            if len(sessions) > 8:
                self.session._user_quit = True
                return
            self.session._dropped = True              # "maximum duration ..."

        log = await self.serve_with(serve_then_expire, attempts=3)
        # Eight expiries against a budget of three, and still serving.
        self.assertEqual(len(sessions), 9)
        self.assertEqual(self.session._exit_code, 0)
        self.assertNotIn("gave up", log)

    async def test_a_genuinely_flapping_socket_still_gives_up(self):
        """The cap has to keep working, or a dead network retries forever."""
        async def drops_at_once(url, headers):
            self.session._dropped = True

        log = await self.serve_with(drops_at_once, attempts=3)
        self.assertEqual(self.session._exit_code, 1)
        self.assertIn("gave up", log)

    async def test_the_backoff_resets_after_a_healthy_session(self):
        """Otherwise a good long session is followed by a 30 s wait."""
        sessions = []

        async def serve_then_expire(url, headers):
            sessions.append(1)
            await asyncio.sleep(self.HEALTHY * 2)
            if len(sessions) > 3:
                self.session._user_quit = True
                return
            self.session._dropped = True

        log = await self.serve_with(serve_then_expire, attempts=6)
        # Every retry is the first retry: none of them ever reaches (2/6).
        self.assertIn("(1/6)", log)
        self.assertNotIn("(2/6)", log)

    async def test_a_recovered_reconnect_stops_saying_error(self):
        """Muted is the resting state, and nothing else writes the status, so
        without clearing it the bar and the orb keep 'reconnecting (1/6)' for
        the rest of the daemon's life — and the orb treats error as awake, so
        it holds an urgent-tinted overlay over a working desktop."""
        calls = []

        async def drop_once(url, headers):
            calls.append(1)
            if len(calls) == 1:
                self.session._dropped = True
            else:
                self.session._user_quit = True

        self.assertFalse(self.session.active)          # muted, the resting state
        with mock.patch.object(self.session, "_open_one", side_effect=drop_once), \
             mock.patch.object(realtime, "RECONNECT_BASE_DELAY", 0.01), \
             mock.patch.object(realtime, "RECONNECT_MAX_DELAY", 0.01):
            await self.session._serve("wss://x", {})
        state = json.loads((Path(self.tmp.name) / "state.json").read_text())
        self.assertEqual(state["status"], "idle")



if __name__ == "__main__":
    unittest.main()


class AnnounceTests(unittest.IsolatedAsyncioTestCase):
    """Saying something without being asked.

    Everything else this daemon does is a reply. A build that ends on
    workspace two is the one thing it starts on its own, and the rules about
    when it may do that are the interesting part.
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
        self.session._last_interruption = 0.0
        self.notified = []
        patcher = mock.patch.object(self.session.feedback, "notify",
                                    side_effect=lambda *a, **k: self.notified.append(a))
        patcher.start()
        self.addCleanup(patcher.stop)

    def job(self, **over):
        base = {"target": "Work:1.1", "label": "the test run", "seconds": 42.0,
                "vanished": False, "timed_out": False, "tail": "ALL TESTS PASSED"}
        return {**base, **over}

    async def test_a_muted_session_notifies_instead_of_speaking(self):
        """Talking into a room that is not listening is just noise."""
        self.session.active = False
        await self.session._announce(self.job())
        self.assertEqual(self.socket.sent, [])
        self.assertTrue(self.notified)

    async def test_a_listening_session_is_asked_to_speak(self):
        self.session.active = True
        await self.session._announce(self.job())
        self.assertTrue(self.socket.events("conversation.item.create"))
        self.assertTrue(self.socket.events("response.create"))

    async def test_the_output_goes_with_it_so_the_verdict_is_not_guessed(self):
        self.session.active = True
        await self.session._announce(self.job(tail="FAILED: 3 tests"))
        item = self.socket.events("conversation.item.create")[0]["item"]
        text = item["content"][0]["text"]
        self.assertIn("FAILED: 3 tests", text)
        self.assertIn("the test run", text)

    async def test_the_model_is_told_the_user_did_not_just_speak(self):
        """Without this it answers as though it had been asked something."""
        self.session.active = True
        await self.session._announce(self.job())
        text = self.socket.events("conversation.item.create")[0]["item"]["content"][0]["text"]
        self.assertIn("did not just speak", text)

    async def test_a_vanished_pane_is_described_as_closed_not_finished(self):
        self.session.active = True
        await self.session._announce(self.job(vanished=True))
        text = self.socket.events("conversation.item.create")[0]["item"]["content"][0]["text"]
        self.assertIn("closed", text)

    async def test_it_waits_rather_than_talking_over_a_reply_in_flight(self):
        self.session.active = True
        self.session._response_running = True

        async def release():
            await asyncio.sleep(0.05)
            self.session._response_running = False

        await asyncio.gather(self.session._announce(self.job()), release())
        self.assertTrue(self.socket.events("response.create"))

    async def test_announcements_do_not_pile_up_on_each_other(self):
        self.session.active = True
        self.session._last_interruption = time.time()
        with mock.patch.object(realtime, "WATCH_MIN_GAP_SECONDS", 0.05):
            await self.session._announce(self.job())
        self.assertTrue(self.socket.events("response.create"))

    async def test_a_stopping_session_says_nothing(self):
        self.session.active = True
        self.session._stop.set()
        self.session._response_running = True
        await self.session._announce(self.job())
        self.assertEqual(self.socket.events("response.create"), [])


class EchoGateTests(unittest.TestCase):
    """Her voice must not come back in as the user's.

    From a real session on speakers, with the mic and the line out on the same
    audio interface:

        reply  'OH-mah, OH-mah, OH-mah.'
        error  response cancelled: turn_detected
        heard  '\uc5b4\ub9c8'                <- her own name, back through the mic
        reply  'Yes, I'm here.'

    and, later, her own sentence returned as two user turns which she then
    apologised for not catching. A fragment that transcribes as an instruction
    is not merely noise: one arrived as 'Бела.' and pressed CTRL+R.
    """

    def setUp(self):
        self.speaker = realtime.Speaker(rate=24000)

    def test_a_fresh_speaker_is_not_playing(self):
        self.assertFalse(self.speaker.is_playing())

    def test_writing_audio_books_its_real_duration(self):
        """PCM16 mono: one second of 24 kHz is 48000 bytes."""
        with mock.patch.object(realtime.asyncio, "create_task"):
            asyncio.run(self.speaker.write(b"\0" * 48000))
        self.assertTrue(self.speaker.is_playing())
        self.assertAlmostEqual(self.speaker._plays_until - time.monotonic(), 1.0, delta=0.2)

    def test_chunks_queue_up_rather_than_overwriting_each_other(self):
        """The model sends a reply far faster than it is spoken, so the gate
        has to track the whole backlog, not the newest chunk."""
        with mock.patch.object(realtime.asyncio, "create_task"):
            for _ in range(3):
                asyncio.run(self.speaker.write(b"\0" * 24000))   # 0.5s each
        self.assertAlmostEqual(self.speaker._plays_until - time.monotonic(), 1.5, delta=0.3)

    def test_the_tail_keeps_the_gate_shut_a_little_longer(self):
        """A room rings after playback stops; the last syllable comes back late."""
        self.speaker._plays_until = time.monotonic() - 0.1
        self.assertFalse(self.speaker.is_playing())
        self.assertTrue(self.speaker.is_playing(realtime.ECHO_TAIL_SECONDS))

    def test_a_barge_in_reopens_the_microphone_at_once(self):
        """Dropping queued audio means nothing more is coming out, so the gate
        must not stay shut for audio that will never be played."""
        with mock.patch.object(realtime.asyncio, "create_task"):
            asyncio.run(self.speaker.write(b"\0" * 480000))       # 10 seconds
        self.assertTrue(self.speaker.is_playing())
        self.speaker._drop_queued()
        self.assertFalse(self.speaker.is_playing())

    def test_half_duplex_is_the_default(self):
        self.assertFalse(Config().barge_in)

    def test_barge_in_can_be_turned_back_on_for_headphones(self):
        self.assertTrue(Config(barge_in=True).barge_in)


class MicrophoneGateTests(unittest.IsolatedAsyncioTestCase):
    """The gate as the microphone actually sees it, not as Speaker reports it.

    EchoGateTests above covers Speaker's bookkeeping, and every one of those
    tests passed for a whole release while `_mic_loop` appended every frame
    unconditionally: `is_playing` was never called, `_held_frames` was never
    incremented, `ECHO_TAIL_SECONDS` was never read. The feature was tested at
    the accessor and absent from the audio path, which is the one place it has
    to exist. So these tests drive the loop and assert on what reached the
    wire.
    """

    # 100 ms of 24 kHz PCM16, loud enough to clear the meter's noise gate:
    # frame_level reads it as 0.6, "normal speech". A quieter frame would read
    # 0.0 whether or not the gate held it, which would make the orb assertion
    # below pass for the wrong reason.
    FRAME = b"\x00\x20" * 2400

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
        self.log = root / "session.log"

    async def run_mic(self, frames, config=None, speaking=0.0):
        """Run _mic_loop over `frames`, with her voice booked `speaking` seconds.

        `frames` may hold the string "quiet", which stops the playback booking
        at that point — the way a barge-in or the end of a reply reopens the
        gate part way through a capture.
        """
        session = realtime.RealtimeSession(config or Config(dry_run=True, notify=False))
        socket = FakeSocket()
        session.ws = socket
        session._active_event.set()
        if speaking:
            session.speaker._plays_until = time.monotonic() + speaking

        # (yours, hers) as published to the orb.
        levels = []
        session.feedback.level = lambda mic, voice=0.0: levels.append((mic, voice))

        queue = list(frames)

        class Stdout:
            async def read(self, _n):
                while queue:
                    item = queue.pop(0)
                    if item == "quiet":
                        session.speaker._plays_until = 0.0
                        continue
                    return item
                # EOF is how a capture ends; stop the session so the outer
                # loop does not spawn a second recorder.
                session._stop.set()
                return b""

        class Proc:
            returncode = None
            stdout = Stdout()

            def terminate(self):
                self.returncode = 0

            async def wait(self):
                self.returncode = 0
                return 0

        with mock.patch.object(realtime.asyncio, "create_subprocess_exec",
                               new=mock.AsyncMock(return_value=Proc())):
            await asyncio.wait_for(session._mic_loop(), timeout=5)
        appended = socket.events("input_audio_buffer.append")
        return session, appended, levels

    async def test_her_own_voice_never_reaches_the_wire(self):
        """The bug, stated as a test: four frames captured while she is
        speaking, and the server must be sent none of them."""
        session, appended, _ = await self.run_mic([self.FRAME] * 4, speaking=5.0)
        self.assertEqual(appended, [])
        self.assertEqual(session._held_frames, 4)

    async def test_a_quiet_room_is_sent_normally(self):
        """The gate must not be a mute button: with nothing playing, every
        frame goes."""
        _, appended, _ = await self.run_mic([self.FRAME] * 4, speaking=0.0)
        self.assertEqual(len(appended), 4)

    async def test_the_microphone_reopens_when_she_stops(self):
        """Two frames held while she talks, two sent once the room is quiet."""
        session, appended, _ = await self.run_mic(
            [self.FRAME, self.FRAME, "quiet", self.FRAME, self.FRAME], speaking=5.0)
        self.assertEqual(len(appended), 2)
        self.assertEqual(session._held_frames, 0)      # reset when it reopened
        self.assertIn("mic     held 2 frames while speaking", self.log.read_text())

    async def test_the_tail_outlasts_the_audio(self):
        """A room rings after playback ends. A frame arriving inside the tail
        is still her, so it is still held."""
        session, appended, _ = await self.run_mic(
            [self.FRAME], speaking=-realtime.ECHO_TAIL_SECONDS / 2)
        self.assertFalse(session.speaker.is_playing())   # playback itself is over
        self.assertEqual(appended, [])                   # and yet: still held

    async def test_the_orb_does_not_twitch_on_her_own_voice(self):
        """A held frame is not being heard. Showing its loudness on the meter
        reads as 'it is listening to you' while the gate is shut."""
        _, _, levels = await self.run_mic([self.FRAME] * 3, speaking=5.0)
        self.assertEqual([mic for mic, _ in levels if mic > 0.0], [])

    async def test_her_voice_reaches_the_orb_while_the_mic_is_held(self):
        """The gate silences the input meter for the length of every reply. If
        that were the only channel the orb would go dead still whenever she
        talks, so her own loudness has to arrive on the second one."""
        session = realtime.RealtimeSession(Config(dry_run=True, notify=False))
        socket = FakeSocket()
        session.ws = socket
        session._active_event.set()
        levels = []
        session.feedback.level = lambda mic, voice=0.0: levels.append((mic, voice))
        # A second of her, loud, already booked for playback.
        with mock.patch.object(realtime.asyncio, "create_task"):
            await session.speaker.write(b"\x00\x20" * 24000)

        queue = [self.FRAME] * 3

        class Stdout:
            async def read(self, _n):
                if queue:
                    return queue.pop(0)
                session._stop.set()
                return b""

        class Proc:
            returncode = None
            stdout = Stdout()

            def terminate(self):
                self.returncode = 0

            async def wait(self):
                self.returncode = 0
                return 0

        with mock.patch.object(realtime.asyncio, "create_subprocess_exec",
                               new=mock.AsyncMock(return_value=Proc())):
            await asyncio.wait_for(session._mic_loop(), timeout=5)

        self.assertEqual(socket.events("input_audio_buffer.append"), [])   # held
        self.assertEqual([mic for mic, _ in levels if mic > 0.0], [])      # meter flat
        self.assertTrue([voice for _, voice in levels if voice > 0.0],
                        "her own voice never reached the orb")

    async def test_barge_in_hands_the_interruption_back(self):
        """Headphones or PipeWire echo cancellation: there is no leak to gate,
        and holding the mic would only stop the user cutting in."""
        config = Config(dry_run=True, notify=False, barge_in=True)
        session, appended, _ = await self.run_mic(
            [self.FRAME] * 4, config=config, speaking=5.0)
        self.assertEqual(len(appended), 4)
        self.assertEqual(session._held_frames, 0)

    async def test_held_audio_is_not_counted_as_something_to_commit(self):
        """`turn_detection = none` commits whatever is in the buffer on
        toggle-off. Her own voice is not a turn: if a held frame still set the
        flag, letting go of the key would commit a buffer holding nothing but
        her reply, and she would answer herself."""
        session, appended, _ = await self.run_mic([self.FRAME] * 2, speaking=5.0)
        self.assertEqual(appended, [])
        self.assertFalse(session._appended_audio)


class SpeechMeterTests(unittest.TestCase):
    """What the orb is told about her own voice."""

    def setUp(self):
        self.speaker = realtime.Speaker(rate=24000)

    def write(self, pcm):
        with mock.patch.object(realtime.asyncio, "create_task"):
            asyncio.run(self.speaker.write(pcm))

    QUIET = (0x0040).to_bytes(2, "little")      # below the meter's noise gate
    LOUD = (0x2000).to_bytes(2, "little")       # reads as normal speech

    def test_a_silent_speaker_reads_zero(self):
        self.assertEqual(self.speaker.level_now(), 0.0)

    def test_loudness_is_booked_on_the_playback_clock_not_arrival(self):
        """Half a second of quiet then half a second of loud arrives as one
        chunk off the socket. The meter must report quiet now and loud later,
        or it runs a second ahead of the speakers."""
        self.write(self.QUIET * 12000 + self.LOUD * 12000)
        self.assertEqual(self.speaker.level_now(), 0.0)
        loud = [level for _, _, level in self.speaker._envelope if level > 0.0]
        self.assertTrue(loud, "the loud half was never booked")
        self.assertAlmostEqual(loud[0], 0.6, delta=0.05)

    def test_a_chunk_is_sliced_rather_than_flattened(self):
        """One level for a whole delta would make the meter a staircase."""
        self.write(self.LOUD * 24000)                      # one second
        self.assertEqual(len(self.speaker._envelope),
                         int(1.0 / realtime.METER_SLICE))

    def test_a_barge_in_stops_her_mid_word(self):
        """Dropping queued audio must clear the envelope too, or the orb goes
        on breathing with a reply that was cut off."""
        self.write(self.LOUD * 240000)                     # ten seconds
        self.assertGreater(self.speaker.level_now(), 0.0)
        self.speaker._drop_queued()
        self.assertEqual(self.speaker.level_now(), 0.0)


class LevelFileTests(unittest.TestCase):
    """The orb reads this file 16 times a second; its format is an interface."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "level"
        for name, value in (("LEVEL_FILE", self.path),
                            ("STATE_FILE", Path(self.tmp.name) / "state.json"),
                            ("STATE_DIR", Path(self.tmp.name)),
                            ("RUNTIME_DIR", Path(self.tmp.name))):
            patcher = mock.patch.object(feedback, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.feedback = feedback.Feedback(Config(notify=False))

    def publish(self, mic, voice=0.0):
        self.feedback._level_at = 0.0          # defeat the 20 Hz cap
        self.feedback.level(mic, voice)
        return self.path.read_text()

    def test_both_voices_are_published(self):
        self.assertEqual(self.publish(0.5, 0.25), "0.500 0.250")

    def test_the_microphone_stays_the_first_field(self):
        """parseFloat stops at the space, so a reader written against the old
        single-float format still gets the microphone level and simply never
        learns there is a second channel."""
        self.assertEqual(float(self.publish(0.75, 0.5).split()[0]), 0.75)

    def test_levels_are_clamped_to_the_range_the_orb_expects(self):
        self.assertEqual(self.publish(9.0, -3.0), "1.000 0.000")


class EchoRiskTests(unittest.TestCase):
    """doctor should say this out loud, because working it out from a session
    log took an evening."""

    SCARLETT_MIC = ("alsa_input.usb-Focusrite_Scarlett_Solo_USB_Y73FW"
                    "440536E29-00.HiFi__Mic1__source")
    SCARLETT_OUT = ("alsa_output.usb-Focusrite_Scarlett_Solo_USB_Y73FW"
                    "440536E29-00.HiFi__Line__sink")

    def risk(self, config, sink):
        with mock.patch.object(realtime, "default_sink", return_value=sink):
            return realtime.echo_risk(config)

    def test_half_duplex_needs_no_warning(self):
        """Nothing to warn about: the microphone is shut while she speaks."""
        self.assertEqual(
            self.risk(Config(device=self.SCARLETT_MIC), self.SCARLETT_OUT), "")

    def test_one_device_for_both_is_called_out(self):
        risk = self.risk(Config(barge_in=True, device=self.SCARLETT_MIC),
                         self.SCARLETT_OUT)
        self.assertIn("same device", risk)
        self.assertIn("barge_in = false", risk)

    def test_a_headset_is_fine(self):
        risk = self.risk(
            Config(barge_in=True, device="alsa_input.usb-Some_Headset-00.mono-chat"),
            "alsa_output.usb-Some_Headset-00.analog-chat")
        self.assertEqual(risk, "")

    def test_an_echo_cancelled_source_is_fine(self):
        risk = self.risk(Config(barge_in=True, device="echo-cancel-source"),
                         self.SCARLETT_OUT)
        self.assertEqual(risk, "")

    def test_separate_devices_still_get_a_gentle_note(self):
        risk = self.risk(Config(barge_in=True, device=self.SCARLETT_MIC),
                         "alsa_output.pci-0000_01_00.1.hdmi-stereo")
        self.assertIn("answering herself", risk)

    def test_nothing_is_claimed_when_the_devices_cannot_be_read(self):
        self.assertEqual(self.risk(Config(barge_in=True, device=""), ""), "")

