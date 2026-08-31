"""Terminals through tmux, and telling you when something finishes.

A terminal used to be a picture: grim the window, run tesseract, hope. That
made output garbled, readable only while the window was visible, and invisible
entirely with the screen asleep. Input was worse — wtype into whatever had
focus, with no way to know it landed.

tmux answers all of it, and it is the one surface here where the assistant can
both read exactly and act reliably.

Run with: python3 -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from omarchy_voice.config import Config
from omarchy_voice.tools import (
    IDLE_COMMANDS, READ_ONLY_TOOLS, TERMINAL_OUTPUT_LIMIT, WATCH_MAX_SECONDS,
    Executor, Result,
)

PANES = "\n".join([
    "Work\t1\t1\t1\tbash\t~/code",
    "Work\t1\t1\t2\tpytest\trunning tests",
    "Spare\t0\t1\t1\tbash\t~/elsewhere",
])


class FakeTmux(Executor):
    """An executor with a scripted tmux and a desktop that has a terminal on it."""

    def __init__(self, panes=PANES, capture="build ok", terminal_visible=True):
        super().__init__(Config())
        self.panes_raw = panes
        self.capture = capture
        self.sent: list[list[str]] = []
        self.launched: list[list[str]] = []
        self._terminal_on_screen = lambda: terminal_visible

    def _shell(self, cmd, **kwargs):        # type: ignore[override]
        self.sent.append(cmd)
        if cmd[0] != "tmux":
            self.launched.append(cmd)
            return Result(True, "started")
        verb = cmd[1]
        if verb == "list-panes":
            return Result(True, self.panes_raw)
        if verb == "capture-pane":
            return Result(True, self.capture)
        return Result(True, "")


def with_tmux(**kwargs):
    ex = FakeTmux(**kwargs)
    with mock.patch("shutil.which", return_value="/usr/bin/tmux"):
        yield ex


class PaneListingTests(unittest.TestCase):
    def setUp(self):
        self.ex = FakeTmux()
        self.which = mock.patch("shutil.which", return_value="/usr/bin/tmux")
        self.which.start()
        self.addCleanup(self.which.stop)

    def test_panes_are_parsed_into_targets(self):
        panes = self.ex._tmux_panes()
        self.assertEqual([p["target"] for p in panes],
                         ["Work:1.1", "Work:1.2", "Spare:1.1"])

    def test_a_shell_means_idle_and_anything_else_means_busy(self):
        panes = {p["target"]: p for p in self.ex._tmux_panes()}
        self.assertTrue(panes["Work:1.1"]["idle"])
        self.assertFalse(panes["Work:1.2"]["idle"])
        self.assertIn("bash", IDLE_COMMANDS)

    def test_attachment_is_read_per_session(self):
        panes = {p["target"]: p for p in self.ex._tmux_panes()}
        self.assertTrue(panes["Work:1.1"]["attached"])
        self.assertFalse(panes["Spare:1.1"]["attached"])

    def test_an_empty_target_prefers_the_pane_with_something_running(self):
        pane, why = self.ex._resolve_pane("")
        self.assertEqual(pane["target"], "Work:1.2")

    def test_an_exact_target_wins(self):
        self.assertEqual(self.ex._resolve_pane("Work:1.1")[0]["target"], "Work:1.1")

    def test_an_ambiguous_target_is_refused_with_the_options(self):
        pane, why = self.ex._resolve_pane("Work")
        self.assertIsNone(pane)
        self.assertIn("Work:1.1", why)
        self.assertIn("Work:1.2", why)

    def test_no_tmux_at_all_says_how_to_start_one(self):
        ex = FakeTmux(panes="")
        pane, why = ex._resolve_pane("")
        self.assertIsNone(pane)
        self.assertIn("launch terminal tmux", why)


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.which = mock.patch("shutil.which", return_value="/usr/bin/tmux")
        self.which.start()
        self.addCleanup(self.which.stop)

    def test_reading_is_exact_text_not_a_screenshot(self):
        ex = FakeTmux(capture="error: cannot find module 'foo'")
        result = ex.call("read_terminal", {})
        self.assertIn("cannot find module", result.output)
        self.assertFalse(any("grim" in c[0] or "tesseract" in c[0] for c in ex.sent))

    def test_the_read_says_whether_the_pane_is_still_busy(self):
        ex = FakeTmux()
        self.assertIn("still running", ex.call("read_terminal", {}).output)
        self.assertIn("idle", ex.call("read_terminal", {"target": "Work:1.1"}).output)

    def test_a_pane_nobody_can_see_is_still_readable(self):
        """The whole point: workspace and DPMS do not apply to text."""
        ex = FakeTmux(terminal_visible=False)
        self.assertTrue(ex.call("read_terminal", {"target": "Spare:1.1"}).ok)

    def test_a_huge_scrollback_is_trimmed_from_the_top(self):
        ex = FakeTmux(capture="x" * (TERMINAL_OUTPUT_LIMIT * 2) + "THE-END")
        out = ex.call("read_terminal", {}).output
        self.assertLess(len(out), TERMINAL_OUTPUT_LIMIT + 400)
        self.assertIn("THE-END", out)          # the newest output survives
        self.assertIn("earlier output", out)

    def test_reading_and_listing_work_under_dry_run(self):
        self.assertIn("read_terminal", READ_ONLY_TOOLS)
        self.assertIn("list_terminals", READ_ONLY_TOOLS)


class RunningTests(unittest.TestCase):
    def setUp(self):
        self.which = mock.patch("shutil.which", return_value="/usr/bin/tmux")
        self.which.start()
        self.addCleanup(self.which.stop)

    def test_a_command_is_sent_with_a_literal_Enter(self):
        """send-keys takes the key by name, so none of the keysym problem that
        cost a whole session applies here."""
        ex = FakeTmux()
        with mock.patch("time.sleep"):
            ex.call("run_in_terminal", {"command": "ls", "target": "Work:1.1"})
        keys = next(c for c in ex.sent if c[:2] == ["tmux", "send-keys"])
        self.assertEqual(keys[-2:], ["ls", "Enter"])

    def test_the_command_is_passed_after_a_double_dash(self):
        ex = FakeTmux()
        with mock.patch("time.sleep"):
            ex.call("run_in_terminal", {"command": "--version", "target": "Work:1.1"})
        keys = next(c for c in ex.sent if c[:2] == ["tmux", "send-keys"])
        self.assertIn("--", keys)

    def test_a_pane_nobody_can_see_is_refused(self):
        """Chosen behaviour: an open microphone may not run commands where the
        user cannot watch them happen."""
        ex = FakeTmux(terminal_visible=False)
        result = ex.call("run_in_terminal", {"command": "ls", "target": "Work:1.1"})
        self.assertFalse(result.ok)
        self.assertIn("not on screen", result.output)
        self.assertEqual([c for c in ex.sent if c[:2] == ["tmux", "send-keys"]], [])

    def test_a_detached_session_is_refused_even_with_a_terminal_on_screen(self):
        ex = FakeTmux()
        result = ex.call("run_in_terminal", {"command": "ls", "target": "Spare:1.1"})
        self.assertFalse(result.ok)
        self.assertIn("not on screen", result.output)

    def test_typing_into_a_busy_pane_is_refused(self):
        """Keys sent to a pane running vim go to vim, not to a shell."""
        ex = FakeTmux()
        result = ex.call("run_in_terminal", {"command": "ls", "target": "Work:1.2"})
        self.assertFalse(result.ok)
        self.assertIn("busy", result.output)
        self.assertIn("pytest", result.output)

    def test_an_empty_command_is_refused(self):
        self.assertFalse(FakeTmux().call("run_in_terminal", {"command": "  "}).ok)

    def test_newlines_are_refused_rather_than_run_as_a_script(self):
        result = FakeTmux().call("run_in_terminal", {"command": "ls\nrm -rf /"})
        self.assertFalse(result.ok)

    def test_the_deny_list_still_applies(self):
        ex = FakeTmux()
        result = ex.call("run_in_terminal", {"command": "sudo rm -rf /"})
        self.assertFalse(result.ok)
        self.assertEqual([c for c in ex.sent if c[:2] == ["tmux", "send-keys"]], [])

    def test_the_gate_sees_the_actual_command(self):
        self.assertIn("rm -rf",
                      Executor.describe("run_in_terminal", {"command": "rm -rf /tmp/x"}))


class WatchingTests(unittest.TestCase):
    def setUp(self):
        self.which = mock.patch("shutil.which", return_value="/usr/bin/tmux")
        self.which.start()
        self.addCleanup(self.which.stop)

    def test_watching_a_busy_pane_returns_at_once(self):
        """It must not block: the microphone is open while it waits."""
        ex = FakeTmux()
        result = ex.call("watch_terminal", {"target": "Work:1.2"})
        self.assertTrue(result.ok)
        self.assertIn("Work:1.2", ex._watches)

    def test_watching_an_idle_pane_is_refused_with_what_it_last_showed(self):
        ex = FakeTmux(capture="all 42 tests passed")
        result = ex.call("watch_terminal", {"target": "Work:1.1"})
        self.assertFalse(result.ok)
        self.assertIn("already idle", result.output)
        self.assertIn("42 tests passed", result.output)

    def test_nothing_fires_while_the_command_is_still_running(self):
        ex = FakeTmux()
        ex.watch("Work:1.2", "the tests")
        self.assertEqual(ex.poll_watches(), [])

    def test_a_watch_does_not_fire_before_the_shell_has_even_forked(self):
        """`pane_current_command` still says "bash" for a moment after keys are
        sent. Reporting that as finished handed back a twenty-second command as
        done in under half a second."""
        ex = FakeTmux(panes=PANES.replace("pytest", "bash"))
        ex.watch("Work:1.2", "the tests")          # not yet observed busy
        self.assertEqual(ex.poll_watches(), [])

    def test_an_instant_command_is_reported_once_the_grace_passes(self):
        from omarchy_voice.tools import TERMINAL_START_GRACE
        ex = FakeTmux(panes=PANES.replace("pytest", "bash"))
        ex.watch("Work:1.2", "cd somewhere")
        ex._watches["Work:1.2"]["started"] -= TERMINAL_START_GRACE + 1
        [job] = ex.poll_watches()
        self.assertFalse(job["timed_out"])

    def test_watch_terminal_knows_the_pane_was_already_busy(self):
        ex = FakeTmux()
        ex.call("watch_terminal", {"target": "Work:1.2"})
        self.assertTrue(ex._watches["Work:1.2"]["seen_busy"])

    def test_a_pane_returning_to_the_shell_is_the_done_signal(self):
        ex = FakeTmux()
        ex.watch("Work:1.2", "the tests")
        self.assertEqual(ex.poll_watches(), [])    # busy: records that it started
        ex.panes_raw = PANES.replace("pytest", "bash")
        [job] = ex.poll_watches()
        self.assertEqual(job["label"], "the tests")
        self.assertFalse(job["vanished"])
        self.assertIn("build ok", job["tail"])

    def test_a_finished_job_is_announced_once(self):
        ex = FakeTmux()
        ex.watch("Work:1.2", "the tests", seen_busy=True)
        ex.panes_raw = PANES.replace("pytest", "bash")
        self.assertEqual(len(ex.poll_watches()), 1)
        self.assertEqual(ex.poll_watches(), [])

    def test_a_closed_pane_is_reported_rather_than_watched_forever(self):
        ex = FakeTmux()
        ex.watch("Work:1.2", "the tests", seen_busy=True)
        ex.panes_raw = "Work\t1\t1\t1\tbash\t~/code"
        [job] = ex.poll_watches()
        self.assertTrue(job["vanished"])

    def test_a_watch_on_something_that_never_ends_still_gives_up(self):
        """The pane stays busy forever. An early `continue` used to skip the age
        check entirely, so this watch would have been held until the daemon
        restarted."""
        ex = FakeTmux()
        ex.watch("Work:1.2", "the tests", seen_busy=True)
        ex._watches["Work:1.2"]["started"] -= WATCH_MAX_SECONDS + 1
        [job] = ex.poll_watches()
        self.assertTrue(job["timed_out"])
        self.assertEqual(ex._watches, {})

    def test_polling_nothing_costs_nothing(self):
        ex = FakeTmux()
        self.assertEqual(ex.poll_watches(), [])
        self.assertEqual(ex.sent, [])


class VisibilityTests(unittest.TestCase):
    """`session_attached` says a client exists, not that anyone can see it —
    the client may be in a window on a workspace nobody has looked at today."""

    def setUp(self):
        self.ex = Executor(Config())
        self.ex._visible_workspaces = lambda: {"2"}

    def windows(self, *clients):
        self.ex._query_json = lambda kind: list(clients)

    def test_a_terminal_on_a_visible_workspace_counts(self):
        self.windows({"class": "foot", "workspace": {"name": "2"}})
        self.assertTrue(self.ex._terminal_on_screen())

    def test_a_terminal_on_another_workspace_does_not(self):
        self.windows({"class": "foot", "workspace": {"name": "7"}})
        self.assertFalse(self.ex._terminal_on_screen())

    def test_a_browser_is_not_a_terminal(self):
        self.windows({"class": "chrome-x.com__-Default", "workspace": {"name": "2"}})
        self.assertFalse(self.ex._terminal_on_screen())

    def test_the_common_terminals_are_recognised(self):
        for klass in ("foot", "Alacritty", "kitty", "com.mitchellh.ghostty",
                      "org.wezfurlong.wezterm"):
            with self.subTest(klass=klass):
                self.windows({"class": klass, "workspace": {"name": "2"}})
                self.assertTrue(self.ex._terminal_on_screen())


if __name__ == "__main__":
    unittest.main()
