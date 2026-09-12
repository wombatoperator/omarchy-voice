"""Live protocol, action isolation, billing lifecycle, and confirmation tests.

No API calls, microphone capture, or desktop actions are performed.
"""
import asyncio
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from omarchy_voice import config, feedback, live, cli
from omarchy_voice.tools import Result


class Socket:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()

    async def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        if event["type"] == "session.start":
            self.incoming.put_nowait({"type": "session.started", "session": {"id": "live_test"}})
        elif event["type"] == "session.close":
            self.incoming.put_nowait({"type": "session.closed", "reason": "close_requested",
                                     "usage": {"seconds": 2}})

    async def recv(self):
        return json.dumps(await self.incoming.get())

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()

    def events(self, kind):
        return [x for x in self.sent if x["type"] == kind]


class Connection:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *args):
        pass


class LiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for module, fields in ((config, {"STATE_DIR": self.root}), (feedback, {
            "STATE_DIR": self.root, "RUNTIME_DIR": self.root,
            "LOG_FILE": self.root / "session.log", "STATE_FILE": self.root / "state.json",
            "LEVEL_FILE": self.root / "level"})):
            for name, value in fields.items():
                patcher = mock.patch.object(module, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        self.session = live.LiveSession(config.Config(engine="live", notify=False, dry_run=True, network_enabled=False))
        self.socket = Socket()
        self.session.ws = self.socket
        self.session._ready = True
        self.session._started_at = time.monotonic()

    async def backend(self, kind, *, delegation="d1", response_id="r1", **data):
        await self.session._on_event({"type": "response.event", "delegation_id": delegation,
                                      "event": {"type": kind, **data}})

    async def response(self, calls, *, delegation="d1", response_id="r1"):
        await self.backend("response.created", delegation=delegation, response={"id": response_id})
        for call in calls:
            await self.backend("response.output_item.done", delegation=delegation,
                               response_id=response_id, item=call)
        # Live intentionally strips output from terminal response snapshots.
        await self.backend("response.completed", delegation=delegation,
                           response={"id": response_id, "output": []})

    def call(self, identity="c1", name="hypr_query", arguments='{"what":"clients"}'):
        return {"type": "function_call", "call_id": identity, "name": name, "arguments": arguments}

    async def drain(self):
        worker = asyncio.create_task(self.session._tool_worker())
        try:
            await asyncio.wait_for(self.session._jobs.join(), 2)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_session_start_uses_live_protocol_and_separate_prompts(self):
        with mock.patch.object(live.capabilities, "manifest", return_value="TOOLS MANIFEST"), \
             mock.patch.object(live.capabilities, "live_state", return_value="DESKTOP"):
            start = await self.session._session_start()
        self.assertEqual(start["type"], "session.start")
        session = start["session"]
        self.assertNotIn("TOOLS MANIFEST", session["instructions"])
        self.assertFalse(session["store"])
        backend = session["delegation"]["responses"]
        self.assertIn("TOOLS MANIFEST", backend["instructions"])
        self.assertEqual(backend["service_tier"], "default")
        self.assertTrue(backend["parallel_tool_calls"])
        self.assertTrue(all(tool["strict"] is False for tool in backend["tools"]))
        self.assertNotIn("run_shell", [tool["name"] for tool in backend["tools"]])
        self.assertNotIn("turn_detection", session["audio"])

    async def test_complex_browser_tool_is_enabled_and_old_navigation_removed(self):
        with mock.patch.object(live.capabilities, "manifest", return_value="tools"), \
             mock.patch.object(live.capabilities, "live_state", return_value="desktop"), \
             mock.patch.object(live.capabilities, "installed_apps", return_value="apps"):
            start = await self.session._session_start()
            backend = start["session"]["delegation"]["responses"]
            names = {t["name"] for t in backend["tools"]}
            self.assertIn("browser_task", names)
            self.assertIn("open_page", names)
            self.assertNotIn("web_search", names)
            self.assertNotIn("click_text", names)
            self.assertIn("For ANY complex browser task", backend["instructions"])
            self.session.config.live_browser_enabled = False
            start = await self.session._session_start()
            names = {t["name"] for t in start["session"]["delegation"]["responses"]["tools"]}
            self.assertNotIn("browser_task", names)
            self.assertIn("web_search", names)

    async def test_browser_result_returns_through_the_live_tool_worker(self):
        from omarchy_voice.tools import Result
        with mock.patch.object(live.browser.BrowserWorker, "run", new=mock.AsyncMock(
                return_value=Result(True, "Verified article headline"))) as browser_run:
            await self.response([self.call(name="browser_task", arguments='{"task":"read this article"}')])
            await self.drain()
        browser_run.assert_awaited_once_with(task="read this article")
        output = self.socket.events("response.item.create")[-1]["item"]
        self.assertIn("Verified article headline", output["output"])
        self.assertTrue(self.socket.events("response.create"))

    async def test_priority_tier_is_explicit_and_sent_to_backend(self):
        self.session.config.live_service_tier = "priority"
        with mock.patch.object(live.capabilities, "manifest", return_value="tools"), \
             mock.patch.object(live.capabilities, "live_state", return_value="desktop"), \
             mock.patch.object(live.capabilities, "installed_apps", return_value="apps"):
            start = await self.session._session_start()
        self.assertEqual(start["session"]["delegation"]["responses"]["service_tier"], "priority")
        self.session.config.live_service_tier = "auto"
        self.assertIn("live.service_tier must be default or priority",
                      live.config_problems(self.session.config))

    async def test_collected_calls_execute_despite_empty_terminal_output(self):
        await self.response([self.call(), self.call("c2")])
        with mock.patch.object(self.session.executor, "call", return_value=Result(True, "done")) as execute:
            await self.drain()
        self.assertEqual(execute.call_count, 2)
        events = [x["type"] for x in self.socket.sent]
        self.assertEqual(events, ["response.item.create", "response.item.create", "response.create"])
        self.assertNotIn("delegation_id", self.socket.events("response.create")[0])

    async def test_independent_calls_really_overlap_inside_executor(self):
        import threading
        entered = threading.Barrier(2, timeout=1)
        def read(**args):
            entered.wait()  # Fails if Executor still holds its global lock.
            return Result(True, "done")
        await self.response([self.call(arguments='{"kind":"clients"}'),
                             self.call("c2", arguments='{"kind":"monitors"}')])
        with mock.patch.object(self.session.executor, "_tool_hypr_query", side_effect=read):
            await self.drain()
        outputs = self.socket.events("response.item.create")
        self.assertEqual([event["item"]["output"] for event in outputs], ["done", "done"])
        self.assertEqual(len(self.socket.events("response.create")), 1)

    async def test_independent_app_launches_and_targeted_closes_are_grouped(self):
        calls = [self.call("a", "launch_app", '{"app":"stocks"}'),
                 self.call("b", "launch_app", '{"app":"test_app"}'),
                 self.call("c", "hypr_dispatch", json.dumps({"lua": 'hl.dsp.window.close({window="address:0xaaa"})'})),
                 self.call("d", "hypr_dispatch", json.dumps({"lua": 'hl.dsp.window.close({window="address:0xbbb"})'}))]
        self.assertEqual([len(g) for g in self.session._call_groups(calls)], [2, 2])
        common = [self.call("e", "omarchy_cli", '{"command":"launch terminal"}'),
                  self.call("f", "omarchy_cli", '{"command":"launch browser"}')]
        self.assertEqual([len(g) for g in self.session._call_groups(common)], [2])
        self.assertIsNone(self.session.executor.parallel_key("omarchy_cli", {"command":"launch terminal; echo unsafe"}))

    async def test_focus_and_duplicate_targets_are_barriers(self):
        query = self.call("q", arguments='{"kind":"clients"}')
        focus = self.call("f", "hypr_dispatch", '{"lua":"hl.dsp.focus({workspace=\\"5\\"})"}')
        app = self.call("a", "launch_app", '{"app":"stocks"}')
        same_app = self.call("b", "launch_app", '{"app":"stocks:new"}')
        calls = [query, focus, app, same_app]
        self.assertEqual([len(g) for g in self.session._call_groups(calls)], [1, 1, 1, 1])
        self.assertIsNone(self.session.executor.parallel_key("hypr_dispatch", {"lua": "hl.dsp.window.close()"}))
        self.assertIsNone(self.session.executor.parallel_key("launch_app", {"app": "bash -c 'echo hi'"}))

    async def test_bounded_parallelism_and_dependency_order(self):
        self.session.config.live_max_parallel_tools = 2
        running, maximum, events = 0, 0, []
        async def execute(name, args, **kwargs):
            nonlocal running, maximum
            running += 1
            maximum = max(maximum, running)
            events.append(("start", args["id"]))
            await asyncio.sleep(0.01)
            events.append(("end", args["id"]))
            running -= 1
            return "done"
        calls = [self.call(str(i), "hypr_query", json.dumps({"kind":"clients", "id":i})) for i in range(5)]
        calls.append(self.call("focus", "hypr_dispatch", '{"id":"focus","lua":"hl.dsp.focus({workspace=\\"5\\"})"}'))
        await self.response(calls)
        with mock.patch.object(self.session, "_execute", side_effect=execute):
            await self.drain()
        self.assertEqual(maximum, 2)
        self.assertGreater(events.index(("start", "focus")), events.index(("end", 4)))

    async def test_cancellation_skips_later_batch_but_returns_started_results(self):
        self.session.config.live_max_parallel_tools = 2
        async def execute(name, args, **kwargs):
            await asyncio.sleep(0.01)
            self.session._revision += 1
            return "done"
        await self.response([self.call(str(i), arguments=json.dumps({"kind":str(i)})) for i in range(3)])
        with mock.patch.object(self.session, "_execute", side_effect=execute) as action:
            await self.drain()
        self.assertEqual(action.await_count, 2)
        self.assertIn("superseded", self.socket.events("response.item.create")[-1]["item"]["output"])
        self.assertEqual(self.socket.events("response.create"), [])

    async def test_confirmation_prevents_later_batch_actions(self):
        self.session.config.confirm_patterns.append("launch stocks")
        calls = [self.call("a", "launch_app", '{"app":"stocks"}'),
                 self.call("b", "launch_app", '{"app":"test_app"}')]
        self.assertEqual([len(g) for g in self.session._call_groups(calls)], [1, 1])
        await self.response(calls)
        await self.drain()
        self.assertIsNotNone(self.session.executor.pending)
        self.assertIn("not executed", self.socket.events("response.item.create")[-1]["item"]["output"])

    async def test_failed_workspace_change_does_not_launch_apps_in_wrong_workspace(self):
        await self.response([self.call("f", "hypr_dispatch", '{"lua":"hl.dsp.focus({workspace=\\"5\\"})"}'),
                             self.call("a", "launch_app", '{"app":"stocks"}')])
        with mock.patch.object(self.session.executor, "call", return_value=Result(False, "workspace failed")) as action:
            await self.drain()
        self.assertEqual(action.call_count, 1)
        self.assertIn("dependency group failed", self.socket.events("response.item.create")[-1]["item"]["output"])
        self.assertEqual(len(self.socket.events("response.create")), 1)

    async def test_stale_worker_does_not_write_new_session_call_cache(self):
        async def execute(*args, **kwargs):
            self.session._epoch += 1
            self.session._seen_calls = {}
            return "done"
        await self.response([self.call()])
        with mock.patch.object(self.session, "_execute", side_effect=execute):
            await self.drain()
        self.assertEqual(self.session._seen_calls, {})

    async def test_saved_news_and_complete_app_names_reach_backend(self):
        self.session.config.news_sources = ["https://apnews.com/", "https://www.reuters.com/", "https://www.nytimes.com/"]
        with mock.patch.object(live.capabilities, "manifest", return_value="manifest"), \
             mock.patch.object(live.capabilities, "live_state", return_value="desktop"), \
             mock.patch.object(live.capabilities, "installed_apps", return_value="stocks (stocks)") as apps:
            start = await self.session._session_start()
        instructions = start["session"]["delegation"]["responses"]["instructions"]
        self.assertIn("stocks (stocks)", instructions)
        self.assertIn("https://apnews.com/", instructions)
        self.assertIn("ONE compose_windows", instructions)
        apps.assert_called_once_with(200)

    async def test_perf_logs_separate_backend_and_tools(self):
        await self.response([self.call()])
        with mock.patch.object(self.session.executor, "call", return_value=Result(True, "done")):
            await self.drain()
        events = [json.loads(line.split("perf    ")[1]) for line in (self.root / "session.log").read_text().splitlines() if "perf    " in line]
        names = {event["event"] for event in events}
        self.assertTrue({"backend_started", "backend_finished", "batch_started", "tool_finished"} <= names)
        self.assertTrue(all(event.get("duration_ms", 0) >= 0 for event in events))

    async def test_audio_reader_continues_during_slow_tool(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return "done"
        await self.response([self.call()])
        worker = asyncio.create_task(self.session._tool_worker())
        try:
            with mock.patch.object(self.session, "_execute", side_effect=slow), \
                 mock.patch.object(self.session.speaker, "write", new_callable=mock.AsyncMock) as play:
                await asyncio.wait_for(entered.wait(), 1)
                await asyncio.wait_for(self.session._on_event({"type": "session.output_audio.delta",
                    "delta": base64.b64encode(b"\x00\x20" * 20).decode()}), 0.2)
                play.assert_awaited_once()
                release.set()
                await asyncio.wait_for(self.session._jobs.join(), 1)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_stop_request_rejects_late_audio_before_housekeeping_closes(self):
        self.session._close_requested.set()
        self.assertFalse(self.session._closing)
        with mock.patch.object(self.session.speaker, "write", new_callable=mock.AsyncMock) as play:
            await self.session._on_event({"type": "session.output_audio.delta",
                "delta": base64.b64encode(b"\x00\x20" * 2400).decode()})
        play.assert_not_awaited()

    async def test_duplicate_function_events_do_not_repeat_actions(self):
        await self.response([self.call(), self.call()])
        await self.backend("response.completed", response={"id": "r1", "output": []})
        with mock.patch.object(self.session.executor, "call", return_value=Result(True, "done")) as execute:
            await self.drain()
        execute.assert_called_once()

    async def test_new_delegation_invalidates_queued_old_actions(self):
        await self.response([self.call()])
        await self.session._on_event({"type": "session.delegation.created", "delegation": {
            "id": "d2", "target": "responses"}})
        with mock.patch.object(self.session.executor, "call") as execute:
            await self.drain()
        execute.assert_not_called()
        self.assertIn("superseded", self.socket.events("response.item.create")[0]["item"]["output"])
        self.assertEqual(self.socket.events("response.create"), [])

    async def test_late_old_response_does_not_adopt_new_delegation_revision(self):
        for identity in ("d1", "d2"):
            await self.session._on_event({"type": "session.delegation.created", "delegation": {
                "id": identity, "target": "responses"}})
        await self.response([self.call()], delegation="d1")
        with mock.patch.object(self.session.executor, "call") as execute:
            await self.drain()
        execute.assert_not_called()
        self.assertEqual(self.socket.events("response.create"), [])

    async def test_mute_stops_queued_actions_and_requests_close(self):
        await self.response([self.call()])
        await self.session._set_active(False)
        with mock.patch.object(self.session.executor, "call") as execute:
            await self.drain()
        self.assertTrue(self.session._close_requested.is_set())
        execute.assert_not_called()

    async def test_inflight_result_does_not_continue_a_replacement_session(self):
        async def action(*args, **kwargs):
            self.session._epoch += 1
            return "done"
        await self.response([self.call(), self.call("c2")])
        with mock.patch.object(self.session, "_execute", side_effect=action) as execute:
            await self.drain()
        execute.assert_awaited_once()
        self.assertEqual(self.socket.sent, [])

    async def test_invalid_arguments_return_an_error_without_execution(self):
        await self.response([self.call(arguments="[]")])
        with mock.patch.object(self.session.executor, "call") as execute:
            await self.drain()
        execute.assert_not_called()
        self.assertIn("ERROR", self.socket.events("response.item.create")[0]["item"]["output"])

    async def test_budget_caps_tool_rounds(self):
        self.session.config.max_turns = 1
        await self.response([self.call()])
        with mock.patch.object(self.session.executor, "call", return_value=Result(True, "done")):
            await self.drain()
        # One bounded final response can report the last tool's result.
        self.assertEqual(len(self.socket.events("response.create")), 1)
        await self.response([self.call("over-limit")], response_id="summary")
        with mock.patch.object(self.session.executor, "call") as execute:
            await self.drain()
        execute.assert_not_called()
        self.assertEqual(len(self.socket.events("response.create")), 1)
        self.assertTrue(self.socket.events("session.commentary.append"))

    async def test_speech_fragments_do_not_replenish_task_budget(self):
        self.session.config.max_turns = 1
        await self.response([self.call()])
        with mock.patch.object(self.session.executor, "call", return_value=Result(True, "done")):
            await self.drain()
        await self.session._on_event({"type": "session.input_transcript.delta", "delta": "thanks"})
        await self.response([self.call("c2")], response_id="r2")
        with mock.patch.object(self.session.executor, "call") as execute:
            await self.drain()
        execute.assert_not_called()
        self.assertEqual(self.session._task_rounds["d1"], 1)
        self.assertIn("not executed", self.socket.events("response.item.create")[-1]["item"]["output"])

    async def test_new_delegation_has_its_own_budget(self):
        self.session.config.max_turns = 1
        for number in range(2):
            await self.session._on_event({"type": "session.delegation.created", "delegation": {
                "id": f"d{number}", "target": "responses"}})
            await self.response([self.call(f"c{number}")], delegation=f"d{number}", response_id=f"r{number}")
            with mock.patch.object(self.session.executor, "call", return_value=Result(True, "done")) as execute:
                await self.drain()
            execute.assert_called_once()

    async def test_new_speech_blocks_queued_actions_then_forwards_after_results(self):
        await self.response([self.call("old", "launch_app", '{"app":"stocks"}')])
        await self.session._on_event({"type": "session.input_transcript.delta", "delta": "Close "})
        await self.session._on_event({"type": "session.input_transcript.delta", "delta": "Premier League too"})
        # Tell the voice layer who owns the correction before a slow tool drains,
        # once per utterance rather than once per transcript fragment.
        self.assertEqual(len(self.socket.events("session.thinking.append")), 1)
        self.assertFalse(self.socket.events("response.item.create"))
        await self.session._forward_steering()
        self.assertFalse(self.socket.events("response.create"))
        with mock.patch.object(self.session.executor, "call") as execute:
            await self.drain()
        execute.assert_not_called()
        self.session._last_input_at -= 1.3
        await self.session._forward_steering()
        items = self.socket.events("response.item.create")
        self.assertIn("not executed", items[0]["item"]["output"])
        self.assertEqual(items[1]["item"]["content"][0]["text"], "Close Premier League too")
        self.assertEqual(len(self.socket.events("response.create")), 1)
        await self.session._forward_steering()
        self.assertEqual(len(self.socket.events("response.create")), 1)

    async def test_steering_waits_for_running_tool_and_preserves_its_result(self):
        async def execute(*args, **kwargs):
            await self.session._on_event({"type": "session.input_transcript.delta", "delta": "close stocks"})
            return "started successfully"
        await self.response([self.call("a"), self.call("b", "launch_app", '{"app":"stocks"}')])
        with mock.patch.object(self.session, "_execute", side_effect=execute) as action:
            await self.drain()
        self.assertEqual(action.await_count, 1)
        outputs = self.socket.events("response.item.create")
        self.assertEqual(outputs[0]["item"]["output"], "started successfully")
        self.assertIn("not executed", outputs[1]["item"]["output"])
        self.assertFalse(self.socket.events("response.create"))

    async def test_original_utterance_continuation_does_not_pause_its_delegation(self):
        await self.session._on_event({"type": "session.input_transcript.delta", "delta": "open stocks"})
        await self.response([self.call()])
        await self.session._on_event({"type": "session.input_transcript.delta", "delta": " please"})
        self.assertEqual(self.session._steering_text, "")

    async def test_cancel_clears_unforwarded_speech(self):
        self.session._steering_text = "open stocks"
        await self.session._local_cancel()
        await self.session._forward_steering()
        self.assertFalse(self.socket.events("response.create"))

    async def test_delayed_delivery_does_not_split_continuous_speech(self):
        await self.session._on_event({"type": "session.input_transcript.delta", "delta": "Open", "start_ms": 100, "end_ms": 500})
        await self.response([self.call()])
        self.session._last_input_at -= 3
        await self.session._on_event({"type": "session.input_transcript.delta", "delta": " stocks", "start_ms": 500, "end_ms": 900})
        self.assertEqual(self.session._steering_text, "")

    async def test_two_pending_updates_are_both_retained(self):
        await self.response([self.call()])
        await self.session._on_event({"type": "session.input_transcript.delta", "delta": "Close stocks."})
        self.session._last_input_at -= 2
        await self.session._on_event({"type": "session.input_transcript.delta", "delta": "Keep Steam open."})
        self.assertEqual(self.session._steering_text, "Close stocks.\nKeep Steam open.")

    async def test_continuation_after_forward_is_not_lost(self):
        await self.response([self.call()])
        await self.session._on_event({"type": "session.input_transcript.delta", "delta": "Close stocks"})
        await self.drain()
        self.session._last_input_at -= 1.3
        await self.session._forward_steering()
        self.session._last_input_at = time.monotonic()  # Continued speech, not a new utterance.
        await self.session._on_event({"type": "session.input_transcript.delta", "delta": " and Premier League"})
        self.assertEqual(self.session._steering_text, "Close stocks and Premier League")

    async def test_replayed_completed_call_keeps_result_during_steering(self):
        self.session._seen_calls["c1"] = "already completed"
        self.session._steering_text = "new instruction"
        await self.response([self.call()])
        await self.drain()
        self.assertEqual(self.socket.events("response.item.create")[0]["item"]["output"], "already completed")

    async def test_completed_forwarded_answer_is_relayed_once(self):
        self.session._input_forwarded = True
        await self.response([])
        notices = self.socket.events("session.thinking.append")
        self.assertEqual(len(notices), 1)
        self.assertIn("backend is now idle", notices[0]["content"])
        await self.backend("response.completed", response={"id": "r1"})
        self.assertEqual(len(self.socket.events("session.thinking.append")), 1)

    async def test_stale_answer_cannot_complete_the_forwarded_update(self):
        self.session._input_forwarded = True
        await self.backend("response.created", response={"id": "old"})
        self.session._revision += 1
        await self.backend("response.completed", response={"id": "old"})
        self.assertFalse(self.socket.events("session.instructions.append"))

    async def test_step_limit_then_new_spoken_task_recovers_without_voice_delegation(self):
        self.session.config.max_turns = 1
        await self.response([self.call()])
        with mock.patch.object(self.session.executor, "call", return_value=Result(True, "done")):
            await self.drain()
        await self.response([], response_id="limit-summary")
        await self.session._on_event({"type": "session.input_transcript.delta",
                                      "delta": "Close the news windows and keep Codex."})
        self.session._last_input_at -= 1.3
        await self.session._forward_steering()
        self.assertEqual(self.session._task_rounds, {})
        await self.response([self.call("new-work")], response_id="recovered")
        with mock.patch.object(self.session.executor, "call", return_value=Result(True, "new result")) as execute:
            await self.drain()
        execute.assert_called_once()
        self.assertEqual(self.session._task_rounds["d1"], 1)

    async def test_forwarded_fragments_share_budget_but_next_utterance_gets_new_budget(self):
        self.session._steering_text = "close stocks"
        await self.session._forward_steering()
        await self.response([], response_id="r1")
        self.session._task_rounds["d1"] = 8
        self.session._steering_text = "close stocks and news"
        await self.session._forward_steering()
        self.assertEqual(self.session._task_rounds["d1"], 8)
        await self.response([], response_id="r2")
        self.session._utterance += 1
        self.session._steering_text = "now check memory"
        await self.session._forward_steering()
        self.assertEqual(self.session._task_rounds, {})

    async def test_new_idle_speech_restores_routing_after_completed_correction(self):
        self.session._input_forwarded = True
        await self.response([])
        await self.session._on_event({"type": "session.input_transcript.delta", "delta": "Open my news"})
        notice = self.socket.events("session.instructions.append")[-1]["content"]
        self.assertIn("Delegate this new request", notice)
        self.assertFalse(self.session._steering_text)

    async def test_idle_routing_watchdog_reports_once_and_does_not_run_tools(self):
        await self.session._on_event({"type": "session.input_transcript.delta", "delta": "Open my news"})
        self.session._last_input_at -= 7
        await self.session._routing_watchdog()
        await self.session._routing_watchdog()
        self.assertEqual(len(self.socket.events("session.instructions.append")), 2)
        self.assertFalse(self.socket.events("response.create"))
        trace = [json.loads(x) for x in (self.root / "live-trace.jsonl").read_text().splitlines()]
        self.assertEqual(sum(x["event"] == "input_without_delegation" for x in trace), 1)

    async def test_after_handoff_all_later_speech_is_explicitly_forwarded(self):
        self.session._steering_text = "Only tell me the time"
        await self.session._forward_steering()
        await self.response([])
        self.session._recover_next_input = False
        await self.session._on_event({"type": "session.input_transcript.delta", "delta": "Now check computer health"})
        self.assertEqual(self.session._steering_text, "Now check computer health")
        self.session._last_input_at -= 1.3
        await self.session._forward_steering()
        self.assertEqual(self.socket.events("response.item.create")[-1]["item"]["content"][0]["text"],
                         "Now check computer health")

    async def test_unsolicited_delegation_cannot_supersede_application_owned_request(self):
        self.session._application_owned = True
        await self.backend("response.created", response={"id": "active"})
        revision = self.session._revision
        await self.session._on_event({"type": "session.delegation.created", "delegation": {
            "id": "duplicate", "target": "responses"}})
        self.assertEqual(self.session._revision, revision)
        await self.response([self.call()], delegation="duplicate", response_id="duplicate-response")
        with mock.patch.object(self.session.executor, "call") as execute:
            await self.drain()
        execute.assert_not_called()
        self.assertFalse(self.socket.events("response.create"))

    async def test_manual_response_delegation_is_accepted_after_handoff(self):
        self.session._application_owned = True
        self.session._backend_requested = True
        await self.session._on_event({"type": "session.delegation.created", "delegation": {
            "id": "manual", "target": "responses"}})
        await self.response([self.call()], delegation="manual")
        with mock.patch.object(self.session.executor, "call", return_value=Result(True, "done")) as execute:
            await self.drain()
        execute.assert_called_once()

    async def test_tool_trace_retains_result_beyond_recovery_state_limit(self):
        output = "verified page content " * 200
        with mock.patch.object(self.session.executor, "call", return_value=Result(True, output)):
            await self.session._execute("hypr_query", {"kind": "clients"}, call_id="trace-call")
        trace = [json.loads(x) for x in (self.root / "live-trace.jsonl").read_text().splitlines()]
        result = next(x for x in trace if x["event"] == "tool_result")
        self.assertEqual(result["output"], output)
        self.assertEqual(result["call_id"], "trace-call")
        self.assertEqual((self.root / "live-trace.jsonl").stat().st_mode & 0o777, 0o600)

    async def test_audio_latency_is_not_rearmed_by_each_transcript_fragment(self):
        with mock.patch.object(self.session.speaker, "write", new=mock.AsyncMock()):
            await self.session._on_event({"type": "session.input_transcript.delta", "delta": "hello"})
            await self.session._on_event({"type": "session.output_audio.delta",
                "delta": base64.b64encode(b"\x00\x20" * 2400).decode()})
            await self.session._on_event({"type": "session.input_transcript.delta", "delta": " there"})
        self.assertFalse(self.session._awaiting_first_audio)

    async def test_model_cannot_invent_confirmation(self):
        self.session.executor.pending = ("omarchy_cli", {"args": "reboot"})
        with mock.patch.object(self.session.executor, "run_pending") as execute:
            result = await self.session._execute("confirm_last", {"heard_phrase": "confirm"})
        self.assertIn("ERROR", result)
        execute.assert_not_called()

    async def test_confirmation_requires_new_user_audio_not_assistant_text(self):
        self.session.executor.pending = ("omarchy_cli", {"args": "reboot"})
        self.session._hold_after_ms = 100
        for event in [
            {"type": "session.output_transcript.delta", "delta": "confirm", "start_ms": 200, "end_ms": 300},
            {"type": "session.input_transcript.delta", "delta": "confirm", "start_ms": 0, "end_ms": 50},
        ]:
            await self.session._on_event(event)
        self.assertEqual(self.session._confirmation_text, "")
        await self.session._on_event({"type": "session.input_transcript.delta", "delta": "confirm",
                                     "start_ms": 200, "end_ms": 300})
        with mock.patch.object(self.session.executor, "run_pending", return_value=Result(True, "done")) as execute:
            result = await self.session._execute("confirm_last", {})
        execute.assert_called_once()
        self.assertIn("done", result)

    async def test_negated_confirmation_is_rejected(self):
        self.session.executor.pending = ("omarchy_cli", {"args": "reboot"})
        self.session._confirmation_text = "do not confirm"
        with mock.patch.object(self.session.executor, "run_pending") as execute:
            result = await self.session._execute("confirm_last", {})
        execute.assert_not_called()
        self.assertIn("ERROR", result)

    async def test_usage_snapshots_are_not_added(self):
        for seconds in (10, 20, 30):
            await self.session._on_event({"type": "session.usage.updated", "usage": {"seconds": seconds}})
        await self.session._on_event({"type": "session.closed", "usage": {"seconds": 31}})
        self.assertEqual(self.session._usage_seconds, 31)
        self.assertTrue(self.session._usage_final)

    async def test_protocol_error_pauses_instead_of_reopening_paid_sessions(self):
        self.session.active = True
        self.session._wanted.set()
        await self.session._on_event({"type": "error", "error": {"message": "bad configuration"}})
        self.assertFalse(self.session.active)
        self.assertFalse(self.session._wanted.is_set())
        self.assertTrue(self.session._close_requested.is_set())

    async def test_incomplete_handoff_keeps_voice_and_durable_tasks_available(self):
        self.session.active = True
        self.session._wanted.set()
        await self.backend("response.created", response={"id": "r1"})
        partial = self.call(arguments='{"what":')
        partial["status"] = "incomplete"
        await self.backend("response.output_item.done", item=partial)
        await self.session._on_event({"type": "error", "error": {"message": "Responses handoff incomplete."}})
        self.assertTrue(self.session.active)
        self.assertTrue(self.session._wanted.is_set())
        self.assertFalse(self.session._close_requested.is_set())
        self.assertFalse(self.session._backend_busy())
        self.assertTrue(self.session._recover_next_input)
        self.assertTrue(self.session._jobs.empty())
        self.assertFalse(self.socket.events("response.create"))

    async def test_completed_response_with_incomplete_call_does_not_execute(self):
        partial = self.call()
        partial["status"] = "incomplete"
        await self.response([partial])
        self.assertTrue(self.session._jobs.empty())
        self.assertTrue(self.session._recover_next_input)

    async def test_task_tools_and_full_goal_routing_in_live_prompt(self):
        with mock.patch.object(live.capabilities, "manifest", return_value="tools"), \
                mock.patch.object(live.capabilities, "live_state", return_value="desktop"), \
                mock.patch.object(live.capabilities, "installed_apps", return_value="apps"):
            start = await self.session._session_start()
        backend = start["session"]["delegation"]["responses"]
        self.assertIn("task_submit", {x["name"] for x in backend["tools"]})
        self.assertIn("WHOLE goal", backend["instructions"])

    async def test_task_notice_waits_for_delivery_then_survives_voice_reconnect(self):
        manager = mock.Mock()
        manager.store.notices.return_value = [{"id": 7, "text": "Task completed"}]
        with mock.patch.object(self.session.executor, "task_manager", return_value=manager), \
                mock.patch.object(self.session.feedback, "notify", return_value=False):
            self.session.active = False
            await self.session._poll_task_notices()
            manager.store.acknowledge.assert_not_called()
            self.session.active = True
            await self.session._poll_task_notices()
            manager.store.acknowledge.assert_called_once_with(7)
        self.assertTrue(self.socket.events("session.commentary.append"))

    async def test_task_store_failure_does_not_break_other_watchers(self):
        with mock.patch.object(self.session.executor, "task_manager", side_effect=OSError("unavailable")):
            await self.session._poll_task_notices()

    async def test_failed_backend_does_not_execute_collected_calls(self):
        await self.backend("response.created", response={"id": "r1"})
        await self.backend("response.output_item.done", item=self.call())
        await self.backend("response.failed", response={"id": "r1"})
        self.assertTrue(self.session._jobs.empty())
        self.assertTrue(self.socket.events("session.commentary.append"))

    async def test_private_state_preserves_uncertain_actions(self):
        self.session._operations.append({"name": "launch_app", "status": "running"})
        self.session._remember("user", "Open a browser")
        await self.session._persist_state()
        self.assertEqual(self.session._state_path.stat().st_mode & 0o777, 0o600)
        restored = live.LiveSession(self.session.config)
        self.assertIn("unknown", restored._operations[0]["status"])
        self.assertEqual(restored._history[0]["text"], "Open a browser")

    async def test_cannot_execute_without_journal(self):
        with mock.patch.object(self.session, "_save_state", side_effect=OSError("disk full")), \
             mock.patch.object(self.session.executor, "call") as execute:
            with self.assertRaises(OSError):
                await self.session._execute("hypr_query", {"what": "clients"})
        execute.assert_not_called()

    async def test_transcript_fragments_coalesce_before_disk_write(self):
        with mock.patch.object(self.session, '_save_state') as save:
            for text in ('Open ', 'a ', 'browser'):
                self.session._remember('user', text)
            save.assert_not_called()
            await self.session._persist_state()
            self.assertEqual(save.call_count, 1)
            self.assertEqual(json.loads(save.call_args.args[0])['history'][-1]['text'], 'Open a browser')

    async def test_slow_journal_does_not_block_event_loop(self):
        import threading
        started, release = threading.Event(), threading.Event()
        def slow(*args):
            started.set()
            release.wait(2)
        with mock.patch.object(self.session, '_save_state', side_effect=slow):
            task = asyncio.create_task(self.session._persist_state())
            try:
                await asyncio.wait_for(asyncio.to_thread(started.wait), 1)
                await asyncio.sleep(.01)
                self.assertFalse(task.done())
            finally:
                release.set()
                await task

    async def test_cancelled_journal_finishes_before_next_writer(self):
        import threading
        started, release = threading.Event(), threading.Event()
        writes = []
        def slow(payload):
            if not writes:
                started.set()
                release.wait(2)
            writes.append(json.loads(payload)['history'][-1]['text'])
        with mock.patch.object(self.session, '_save_state', side_effect=slow):
            self.session._remember('user', 'first')
            first = asyncio.create_task(self.session._persist_state())
            await asyncio.wait_for(asyncio.to_thread(started.wait), 1)
            first.cancel()
            self.session._remember('user', ' second')
            second = asyncio.create_task(self.session._persist_state())
            await asyncio.sleep(.01)
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)
            self.assertEqual(writes, ['first', 'first second'])

    async def test_handshake_and_graceful_close(self):
        self.session.ws = None
        self.session._wanted.set()
        async def audio():
            self.assertTrue(self.session._ready)
            self.session._close_requested.set()
        with mock.patch.object(live.capabilities, "manifest", return_value="manifest"), \
             mock.patch.object(live.capabilities, "live_state", return_value="desktop"), \
             mock.patch.object(live.realtime, "_open_socket", return_value=Connection(self.socket)) as connect, \
             mock.patch.object(self.session, "_audio_loop", side_effect=audio):
            await asyncio.wait_for(self.session._connection({}), 2)
        connect.assert_called_once_with(live.LIVE_URL, {})
        self.assertEqual([x["type"] for x in self.socket.sent], ["session.start", "session.close"])
        self.assertTrue(self.session._usage_final)
        self.assertIsNone(self.session.ws)

    async def test_boot_does_not_open_a_paid_session(self):
        self.session.ws = None
        async def stop_later():
            await asyncio.sleep(0.02)
            self.session._stop.set()
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), \
             mock.patch.object(live, "ControlServer"), \
             mock.patch.object(live.realtime, "_safety_identifier", return_value="test"), \
             mock.patch.object(self.session, "_connection") as connect:
            await asyncio.gather(self.session.run(), stop_later())
        connect.assert_not_called()

    async def test_time_limit_closes_without_automatic_restart(self):
        self.session.active = True
        self.session._wanted.set()
        self.session.config.live_max_session_seconds = 0.01
        self.session._started_at = time.monotonic() - 1
        self.session._closed.set()  # Cleared below; emulate receiving final event.
        async def finish():
            await asyncio.sleep(0.01)
            self.session._closed.set()
        self.session._closed.clear()
        await asyncio.wait_for(asyncio.gather(self.session._housekeeping(), finish()), 1)
        self.assertFalse(self.session._wanted.is_set())
        self.assertFalse(self.session.active)
        self.assertTrue(self.socket.events("session.close"))

    async def test_typed_commands_use_backend_messages(self):
        await self.session._inject("workspace 3")
        task = asyncio.create_task(self.session._housekeeping())
        try:
            await asyncio.sleep(0.02)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        message = self.socket.events("response.item.create")[0]["item"]
        self.assertEqual(message["role"], "user")
        self.assertFalse(self.session.active)
        self.assertEqual(len(self.socket.events("response.create")), 1)

    async def run_audio(self, frames, *, voice=0.0, active=True):
        session = self.session
        session.active = active
        remaining = list(frames)
        async def read(_):
            if remaining:
                return remaining.pop(0)
            session.active = False
            session._close_requested.set()
            return b""
        proc = mock.Mock()
        proc.stdout.read = mock.AsyncMock(side_effect=read)
        sent = []
        async def send(event):
            sent.append(event)
            if not active:
                session._close_requested.set()
        with mock.patch.object(live.asyncio, "create_subprocess_exec", new_callable=mock.AsyncMock, return_value=proc) as capture, \
             mock.patch.object(session, "_kill_mic", new_callable=mock.AsyncMock), \
             mock.patch.object(session.speaker, "level_now", return_value=voice), \
             mock.patch.object(session, "_send", side_effect=send):
            await asyncio.wait_for(session._audio_loop(), 1)
        return sent, capture

    async def test_echo_gate_sends_equal_duration_silence(self):
        events, _ = await self.run_audio([b"\x00\x20" * 2400], voice=0.6)
        self.assertEqual(events[0]["type"], "session.input_audio.append")
        self.assertEqual(base64.b64decode(events[0]["audio"]), bytes(4800))

    async def test_pcm_carries_an_odd_byte_into_next_frame(self):
        self.session.config.barge_in = True
        events, _ = await self.run_audio([b"\x01\x02\x03", b"\x04\x05\x06"])
        self.assertEqual([base64.b64decode(x["audio"]) for x in events], [b"\x01\x02", b"\x03\x04\x05\x06"])

    async def test_full_duplex_keeps_input_when_output_is_audible(self):
        self.session.config.barge_in = True
        events, _ = await self.run_audio([b"\x00\x20" * 2400], voice=0.6)
        self.assertEqual(base64.b64decode(events[0]["audio"]), b"\x00\x20" * 2400)

    async def test_typed_audio_is_synthetic_and_never_starts_recorder(self):
        events, capture = await self.run_audio([], active=False)
        capture.assert_not_called()
        self.assertEqual(base64.b64decode(events[0]["audio"]), bytes(4800))

    async def test_silent_output_does_not_hold_typed_session_open(self):
        self.session.config.live_typed_idle_seconds = 0.01
        self.session._last_activity = time.monotonic() - 1
        self.session._wanted.set()
        task = asyncio.create_task(self.session._housekeeping())
        try:
            await asyncio.sleep(0.02)
            self.assertTrue(self.session._close_requested.is_set())
            self.assertFalse(self.session._wanted.is_set())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class LiveConfigTests(unittest.TestCase):
    def test_live_table_is_namespaced(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.toml"
            path.write_text('[openai]\nengine="live"\n[live]\nbackend_model="test"\nvoice="marin"\n')
            loaded = config.load(path)
        self.assertEqual(loaded.engine, "live")
        self.assertEqual(loaded.live_backend_model, "test")
        self.assertEqual(loaded.unknown_keys, [])

    def test_realtime_remains_available(self):
        self.assertEqual(config.Config().engine, "realtime")
        args = cli.build_parser().parse_args(["run", "--engine", "live"])
        with mock.patch.object(live, "run", return_value=0) as run:
            cli.cmd_run(args, config.Config(engine="live"))
        run.assert_called_once()

    def test_invalid_audio_and_unbounded_session_are_rejected(self):
        self.assertEqual(len(live.config_problems(config.Config(live_sample_rate=48000,
                                                               live_max_session_seconds=float("inf")))), 2)


if __name__ == "__main__":
    unittest.main()
