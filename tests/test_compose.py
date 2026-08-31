"""Composition, and the mistakes from the session log that are now caught here.

Run with: python3 -m unittest discover -s tests
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from omarchy_voice.config import Config
from omarchy_voice.tools import (
    CLICK_UNAVAILABLE, Executor, Result, _layout_plan, _misused_change_id,
    _pane_command, _pane_hint, _window_matches, normalise_omarchy,
)


class LayoutPlanTests(unittest.TestCase):
    def test_columns_walk_rightwards(self):
        self.assertEqual(_layout_plan("columns", 4),
                         [("r", 0), ("r", 1), ("r", 2)])

    def test_main_and_side_stacks_down_the_side(self):
        # Pane 0 keeps the left; 1 opens beside it, the rest below the one before.
        self.assertEqual(_layout_plan("main-and-side", 4),
                         [("r", 0), ("d", 1), ("d", 2)])

    def test_grid_fills_two_by_two(self):
        self.assertEqual(_layout_plan("grid", 4),
                         [("r", 0), ("d", 0), ("d", 1)])

    def test_a_plan_has_one_step_per_pane_after_the_first(self):
        for layout in ("columns", "main-and-side", "grid"):
            for count in range(1, 7):
                with self.subTest(layout=layout, count=count):
                    self.assertEqual(len(_layout_plan(layout, count)), max(0, count - 1))

    def test_every_step_anchors_on_a_pane_already_open(self):
        for layout in ("columns", "main-and-side", "grid"):
            for count in range(2, 7):
                for index, (_, anchor) in enumerate(_layout_plan(layout, count)):
                    with self.subTest(layout=layout, count=count, index=index):
                        # Pane index+1 is being placed, so panes 0..index exist.
                        self.assertLessEqual(anchor, index)


class PaneCommandTests(unittest.TestCase):
    def test_web_pane_needs_an_http_url(self):
        self.assertIsNone(_pane_command("web", "file:///etc/passwd", ""))
        self.assertIsNone(_pane_command("web", "news.ycombinator.com", ""))
        self.assertEqual(_pane_command("web", "https://apnews.com", "AP"),
                         ["omarchy", "launch", "webapp", "https://apnews.com"])

    def test_panes_never_launch_or_focus(self):
        # Composing means new windows. launch-or-focus would steal a window
        # from another workspace instead of opening one here.
        for kind, target in (("web", "https://apnews.com"), ("terminal", ""),
                             ("tui", "btop")):
            with self.subTest(kind=kind):
                self.assertNotIn("focus", _pane_command(kind, target, ""))

    def test_app_pane_rejects_a_command_line(self):
        self.assertIsNone(_pane_command("app", "rm -rf /", ""))
        self.assertIsNone(_pane_command("app", "chromium --incognito", ""))

    def test_tui_app_id_is_sanitised(self):
        argv = _pane_command("tui", "btop", "my name; rm -rf /")
        self.assertEqual(argv[3], "--app-id=mynamerm-rf")


class WindowMatchTests(unittest.TestCase):
    """A composition once claimed a Chrome "Profile error occurred" dialog as
    its first pane, shifting every later pane by one."""

    DIALOG = {"class": "", "initialClass": "", "focusHistoryID": 0,
              "address": "0xdialog", "title": "Profile error occurred",
              "initialTitle": "Profile error occurred"}
    AP = {"class": "chrome-apnews.com__-Default", "initialClass": "chrome-apnews.com__-Default",
          "address": "0xap", "focusHistoryID": 1,
          "title": "Associated Press News", "initialTitle": "apnews.com"}

    def test_hints_come_off_the_target(self):
        self.assertEqual(_pane_hint("web", "https://www.bbc.com/news", ""), "bbc.com")
        self.assertEqual(_pane_hint("web", "https://apnews.com", ""), "apnews.com")
        self.assertEqual(_pane_hint("app", "spotify.desktop", ""), "spotify")
        self.assertEqual(_pane_hint("terminal", "", ""), "")

    def test_a_dialog_does_not_match_the_site(self):
        self.assertFalse(_window_matches(self.DIALOG, "apnews.com"))

    def test_the_real_window_matches_on_its_initial_title(self):
        # The page retitles itself once loaded, so initialTitle is what matches.
        self.assertTrue(_window_matches(
            {"class": "", "initialTitle": "www.bbc.com_/news", "title": "BBC News"}, "bbc.com"))

    def test_an_unclassed_window_is_never_claimed(self):
        executor = Executor(Config())
        with mock.patch.object(executor, "_query_json", return_value=[self.DIALOG]):
            self.assertIsNone(executor._await_new_window(set(), 0.4, "apnews.com"))

    def test_the_matching_window_wins_over_a_dialog(self):
        executor = Executor(Config())
        with mock.patch.object(executor, "_query_json",
                               return_value=[self.DIALOG, self.AP]):
            self.assertEqual(
                executor._await_new_window(set(), 1.0, "apnews.com"), "0xap")

    def test_an_unhinted_pane_still_takes_a_classed_window(self):
        executor = Executor(Config())
        with mock.patch.object(executor, "_query_json", return_value=[self.DIALOG, self.AP]):
            self.assertEqual(executor._await_new_window(set(), 1.0, ""), "0xap")


class ComposeValidationTests(unittest.TestCase):
    def setUp(self):
        self.executor = Executor(Config())

    def validate(self, **kwargs):
        return self.executor._validate_compose_windows(**kwargs)

    def test_empty_panes_rejected(self):
        self.assertIsNotNone(self.validate(panes=[]))

    def test_too_many_panes_rejected(self):
        panes = [{"kind": "terminal", "target": ""}] * 9
        self.assertIn("unreadable", self.validate(panes=panes))

    def test_unknown_layout_rejected(self):
        self.assertIsNotNone(self.validate(
            panes=[{"kind": "terminal", "target": ""}], layout="cascade"))

    def test_bad_pane_names_which_one(self):
        error = self.validate(panes=[{"kind": "terminal", "target": ""},
                                     {"kind": "web", "target": "ftp://x"}])
        self.assertIn("pane 2", error)

    def test_a_valid_composition_passes(self):
        self.assertIsNone(self.validate(
            panes=[{"kind": "web", "target": "https://apnews.com"},
                   {"kind": "terminal", "target": ""}],
            layout="columns", workspace="next"))


class ComposePolicyTests(unittest.TestCase):
    """Composition must not become a way around the deny list."""

    def test_the_outer_gate_sees_the_whole_composition(self):
        # `describe` spells out every pane, so a deny pattern matching any of
        # them stops the call before the executor runs a thing.
        executor = Executor(Config(deny_patterns=[r"\bapnews\b"]))
        with mock.patch.object(Executor, "_shell") as shell:
            result = executor.call("compose_windows", {
                "panes": [{"kind": "web", "target": "https://apnews.com"}],
                "workspace": "current"})
        self.assertFalse(result.ok)
        self.assertIn("refused", result.output)
        shell.assert_not_called()

    def test_each_pane_is_checked_against_the_command_it_will_run(self):
        # The pane's label is all the outer gate sees; the inner check is what
        # catches a deny pattern written against the command line itself.
        executor = Executor(Config(deny_patterns=[r"\blaunch webapp\b"]))
        with mock.patch.object(executor, "_dispatch_lua", return_value=Result(True, "ok")), \
             mock.patch.object(executor, "_query_json", return_value=[]), \
             mock.patch.object(executor, "_shell") as shell:
            result = executor.call("compose_windows", {
                "panes": [{"kind": "web", "target": "https://apnews.com", "name": "AP News"},
                          {"kind": "web", "target": "https://reuters.com", "name": "Reuters"}],
                "workspace": "current"})
        self.assertFalse(result.ok)
        self.assertIn("policy", result.output)
        shell.assert_not_called()


class SinglePaneTests(unittest.TestCase):
    """A layout tool asked to lay out one window means the request was a
    question, not a workspace. The model kept coming here for "who won the
    race" because this was the habitual route to anything on the web."""

    def setUp(self):
        self.executor = Executor(Config())

    def test_one_pane_is_refused_and_names_the_right_tool(self):
        result = self.executor.call("compose_windows", {
            "panes": [{"kind": "web", "target": "https://f1.com", "name": "F1"}]})
        self.assertFalse(result.ok)
        self.assertIn("web_search", result.output)
        self.assertIn("open_page", result.output)

    def test_the_refusal_carries_the_url_so_open_page_is_one_step_away(self):
        result = self.executor.call("compose_windows", {
            "panes": [{"kind": "web", "target": "https://f1.com", "name": "F1"}]})
        self.assertIn("https://f1.com", result.output)

    def test_two_panes_are_still_a_composition(self):
        with mock.patch.object(self.executor, "_query_json", return_value=[]), \
             mock.patch.object(self.executor, "_dispatch_lua", return_value=Result(True, "ok")), \
             mock.patch.object(self.executor, "_shell", return_value=Result(True, "started")), \
             mock.patch.object(self.executor, "_await_new_window", return_value=None):
            result = self.executor.call("compose_windows", {
                "panes": [{"kind": "web", "target": "https://a.test", "name": "A"},
                          {"kind": "web", "target": "https://b.test", "name": "B"}],
                "workspace": "current"})
        self.assertTrue(result.ok)


class ComposeRunTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()

    def test_a_pane_whose_window_never_appears_is_reported_not_claimed(self):
        executor = Executor(self.config)
        with mock.patch.object(executor, "_query_json", return_value=[]), \
             mock.patch.object(executor, "_dispatch_lua", return_value=Result(True, "ok")), \
             mock.patch.object(executor, "_shell", return_value=Result(True, "started")), \
             mock.patch.object(executor, "_await_new_window", return_value=None):
            result = executor.call("compose_windows", {
                "panes": [{"kind": "terminal", "target": "", "name": "shell"},
                          {"kind": "terminal", "target": "", "name": "logs"}],
                "workspace": "current"})
        self.assertTrue(result.ok)
        self.assertIn("shell", result.output)
        self.assertIn("did not appear", result.output)

    def test_next_workspace_skips_the_ones_in_use(self):
        executor = Executor(self.config)
        with mock.patch.object(executor, "_query_json", return_value=[
                {"id": 1, "windows": 2}, {"id": 2, "windows": 1}]):
            self.assertEqual(executor._target_workspace("next"), ("3", ""))

    def test_current_workspace_means_do_not_switch(self):
        self.assertEqual(Executor(self.config)._target_workspace("current"), (None, ""))

    def test_a_nonsense_workspace_is_refused(self):
        target, error = Executor(self.config)._target_workspace("over there")
        self.assertIsNone(target)
        self.assertTrue(error)


class CommandLookupTests(unittest.TestCase):
    """The CLI list moved out of the prompt and behind a tool: 128 routes at
    ~2,270 tokens were resent every turn against a per-minute budget."""

    INDEX = [
        ("omarchy theme set <theme-name>", "Switch to a different theme"),
        ("omarchy theme list", "List available themes"),
        ("omarchy toggle nightlight [--status]", "Toggle nightlight screen filter"),
        ("omarchy audio output volume <raise|lower>", "Change the output volume"),
    ]

    def search(self, query, **kw):
        with mock.patch("omarchy_voice.capabilities.command_index",
                        return_value=self.INDEX):
            from omarchy_voice import capabilities
            return capabilities.search_commands(query, **kw)

    def test_a_phrase_with_an_unmatched_word_still_finds_the_route(self):
        # "dark" appears in no route; requiring every word found nothing at all.
        hits = self.search("dark theme")
        self.assertTrue(hits)
        self.assertIn("theme", hits[0])

    def test_a_route_hit_outranks_a_summary_hit(self):
        hits = self.search("volume")
        self.assertIn("audio output volume", hits[0])

    def test_nothing_matches_returns_nothing(self):
        self.assertEqual(self.search("xyzzy"), [])

    def test_an_empty_query_returns_nothing(self):
        self.assertEqual(self.search("   "), [])

    def test_results_are_capped(self):
        self.assertLessEqual(len(self.search("omarchy", limit=2)), 2)

    def test_the_tool_tells_the_model_what_to_do_with_a_hit(self):
        executor = Executor(Config())
        with mock.patch("omarchy_voice.capabilities.search_commands",
                        return_value=["  omarchy theme list"]):
            result = executor.call("omarchy_help", {"query": "theme"})
        self.assertTrue(result.ok)
        self.assertIn("omarchy_cli", result.output)

    def test_a_miss_suggests_a_plainer_word(self):
        executor = Executor(Config())
        with mock.patch("omarchy_voice.capabilities.search_commands", return_value=[]):
            result = executor.call("omarchy_help", {"query": "xyzzy"})
        self.assertFalse(result.ok)
        self.assertIn("plainer", result.output)

    def test_lookup_is_read_only(self):
        from omarchy_voice.tools import READ_ONLY_TOOLS
        self.assertIn("omarchy_help", READ_ONLY_TOOLS)


class ReadScreenTests(unittest.TestCase):
    """OCR is the answer to "what does it say". The assistant used to tell the
    user it could not read the screen at all."""

    def setUp(self):
        self.executor = Executor(Config())

    def test_a_window_on_a_hidden_workspace_is_refused_with_the_fix(self):
        with mock.patch.object(self.executor, "_query_json", side_effect=lambda k: {
            "clients": [{"address": "0xa", "workspace": {"name": "7"},
                         "at": [0, 0], "size": [100, 100]}],
            "monitors": [{"activeWorkspace": {"name": "1"}}],
        }[k]):
            result = self.executor.call("read_screen", {"target": "address:0xa"})
        self.assertFalse(result.ok)
        self.assertIn("workspace 7", result.output)
        self.assertIn("hl.dsp.focus", result.output)

    def test_a_visible_window_is_ocred_at_its_geometry(self):
        with mock.patch.object(self.executor, "_query_json", side_effect=lambda k: {
            "clients": [{"address": "0xa", "workspace": {"name": "1"},
                         "at": [12, 38], "size": [800, 600]}],
            "monitors": [{"activeWorkspace": {"name": "1"}}],
        }[k]), mock.patch.object(self.executor, "_ocr_region",
                                 return_value=Result(True, "hello")) as ocr:
            result = self.executor.call("read_screen", {"target": "address:0xa"})
        self.assertTrue(result.ok)
        ocr.assert_called_once_with("12,38 800x600")

    def test_screen_reads_the_focused_monitor(self):
        with mock.patch.object(self.executor, "_query_json", return_value=[
            {"focused": False, "x": 0, "y": 0, "width": 100, "height": 100},
            {"focused": True, "x": 2560, "y": 0, "width": 1920, "height": 1080},
        ]), mock.patch.object(self.executor, "_ocr_region",
                              return_value=Result(True, "text")) as ocr:
            self.executor.call("read_screen", {})
        ocr.assert_called_once_with("2560,0 1920x1080")

    def test_an_unknown_address_says_how_to_get_a_real_one(self):
        with mock.patch.object(self.executor, "_query_json", return_value=[]):
            result = self.executor.call("read_screen", {"target": "address:0xgone"})
        self.assertFalse(result.ok)
        self.assertIn("hypr_query", result.output)

    def test_a_screenful_of_text_is_capped(self):
        # _screen_unavailable is stubbed because it asks the real hyprctl through
        # Popen, which subprocess.run does not cover: with the monitor in DPMS
        # off this test used to fail on the sleeping-display refusal instead of
        # on anything to do with the cap.
        from omarchy_voice.tools import OCR_LIMIT
        with mock.patch.object(self.executor, "_screen_unavailable", return_value=None), \
             mock.patch("subprocess.run") as run:
            run.side_effect = [
                mock.Mock(returncode=0, stdout=b"PNG", stderr=b""),
                mock.Mock(returncode=0, stdout=b"x" * (OCR_LIMIT * 2), stderr=b""),
            ]
            result = self.executor._ocr_region("0,0 100x100")
        self.assertTrue(result.ok)
        self.assertLess(len(result.output), OCR_LIMIT + 200)
        self.assertIn("not read", result.output)

    def test_read_screen_survives_dry_run(self):
        # Read-only, like hypr_query: --dry-run must still be able to look.
        from omarchy_voice.tools import READ_ONLY_TOOLS
        self.assertIn("read_screen", READ_ONLY_TOOLS)


class ClickByTextTests(unittest.TestCase):
    """Clicking by coordinates is useless to someone talking. The request that
    prompted this was "double-click into this US and Iran trade strikes"."""

    # A headline OCR'd into words, with one word misread ("frade").
    WORDS = [
        {"text": "Breaking", "x": 10, "y": 10, "w": 60, "h": 12, "conf": 90},
        {"text": "US", "x": 100, "y": 50, "w": 20, "h": 12, "conf": 95},
        {"text": "and", "x": 130, "y": 50, "w": 25, "h": 12, "conf": 95},
        {"text": "Iran", "x": 160, "y": 50, "w": 30, "h": 12, "conf": 95},
        {"text": "frade", "x": 195, "y": 50, "w": 35, "h": 12, "conf": 60},
        {"text": "strikes", "x": 235, "y": 50, "w": 45, "h": 12, "conf": 92},
        {"text": "Continue", "x": 400, "y": 300, "w": 70, "h": 14, "conf": 98},
    ]

    def setUp(self):
        self.executor = Executor(Config())

    def test_half_the_words_is_not_a_match(self):
        # "Files changed" used to match the prose "changed files and file tree"
        # elsewhere on the page and click it, confidently, in the wrong place.
        words = [
            {"text": "changed", "x": 10, "y": 400, "w": 50, "h": 12, "conf": 90},
            {"text": "documents", "x": 70, "y": 400, "w": 60, "h": 12, "conf": 90},
        ]
        self.assertIsNone(self.executor._find_phrase(words, "Files changed"))

    def test_a_short_query_needs_every_word(self):
        from omarchy_voice.tools import _required_hits
        self.assertEqual(_required_hits(1), 1)
        self.assertEqual(_required_hits(2), 2)
        self.assertEqual(_required_hits(3), 3)

    def test_a_long_query_tolerates_one_ocr_miss(self):
        from omarchy_voice.tools import _required_hits
        self.assertEqual(_required_hits(5), 4)

    def test_ocr_uses_automatic_page_segmentation(self):
        # psm 6 assumes one uniform block and read straight past the tab bar of
        # a GitHub pull request; psm 3 segments the page and finds it.
        from omarchy_voice.tools import OCR_PAGE_MODE
        self.assertEqual(OCR_PAGE_MODE, 3)

    def test_a_phrase_is_found_despite_a_misread_word(self):
        point = self.executor._find_phrase(self.WORDS, "US and Iran trade strikes")
        self.assertIsNotNone(point)
        x, y = point
        # Centre should land inside the headline's span, not on "Breaking".
        self.assertTrue(100 <= x <= 280, x)
        self.assertTrue(50 <= y <= 62, y)

    def test_a_single_word_button_is_found(self):
        x, y = self.executor._find_phrase(self.WORDS, "Continue")
        self.assertTrue(400 <= x <= 470)
        self.assertTrue(300 <= y <= 314)

    def test_text_that_is_not_there_is_not_invented(self):
        self.assertIsNone(self.executor._find_phrase(self.WORDS, "Delete everything"))

    def test_no_words_no_match(self):
        self.assertIsNone(self.executor._find_phrase([], "Continue"))

    def test_empty_text_is_refused(self):
        self.assertIsNotNone(self.executor._validate_click_text("  "))

    def test_a_bad_button_is_refused(self):
        self.assertIsNotNone(self.executor._validate_click_text("ok", button="scroll"))

    def test_missing_text_reports_and_does_not_click(self):
        with mock.patch.object(self.executor, "_query_json",
                               return_value=[{"focused": True, "x": 0, "y": 0,
                                              "width": 100, "height": 100}]), \
             mock.patch.object(self.executor, "_ocr_words", return_value=(self.WORDS, "")), \
             mock.patch.object(self.executor, "_press_button") as press:
            result = self.executor.call("click_text", {"text": "Nonexistent Button"})
        self.assertFalse(result.ok)
        self.assertIn("could not find", result.output)
        press.assert_not_called()

    def test_a_hit_moves_the_pointer_then_clicks(self):
        with mock.patch.object(self.executor, "_query_json",
                               return_value=[{"focused": True, "x": 0, "y": 0,
                                              "width": 500, "height": 500}]), \
             mock.patch.object(self.executor, "_ocr_words", return_value=(self.WORDS, "")), \
             mock.patch.object(self.executor, "_dispatch_lua",
                               return_value=Result(True, "ok")) as move, \
             mock.patch.object(self.executor, "_press_button",
                               return_value=Result(True, "")) as press:
            result = self.executor.call("click_text", {"text": "Continue", "double": True})
        self.assertTrue(result.ok)
        self.assertIn("cursor.move", move.call_args[0][0])
        press.assert_called_once_with("left", True)

    def test_without_ydotool_it_says_what_to_run(self):
        with mock.patch("shutil.which", return_value=None):
            result = self.executor._press_button("left", False)
        self.assertFalse(result.ok)
        self.assertIn("ydotool", result.output)
        self.assertEqual(result.output, CLICK_UNAVAILABLE)

    def test_low_confidence_words_are_dropped(self):
        from omarchy_voice.tools import MIN_OCR_CONFIDENCE
        self.assertGreater(MIN_OCR_CONFIDENCE, 0)


class SleepingScreenTests(unittest.TestCase):
    """grim blocks rather than failing when the monitor is in DPMS off, so a
    read at half past midnight hung for the full timeout."""

    def setUp(self):
        self.executor = Executor(Config())
        # The lock probe shells out to the real omarchy-shell. These tests are
        # about DPMS, and on a machine whose session happens to be locked they
        # would otherwise all fail on the lock refusal instead.
        locked = mock.patch.object(self.executor, "_session_is_locked",
                                   return_value=False)
        locked.start()
        self.addCleanup(locked.stop)

    def test_a_sleeping_display_is_reported_not_captured(self):
        with mock.patch.object(self.executor, "_query_json",
                               return_value=[{"name": "HDMI-A-1", "dpmsStatus": False}]), \
             mock.patch("subprocess.run") as run:
            result = self.executor._ocr_region("0,0 100x100")
        self.assertFalse(result.ok)
        self.assertIn("asleep", result.output)
        self.assertIn("dpms", result.output)
        run.assert_not_called()

    def test_an_awake_display_captures_normally(self):
        with mock.patch.object(self.executor, "_query_json",
                               return_value=[{"name": "HDMI-A-1", "dpmsStatus": True}]):
            self.assertIsNone(self.executor._screen_unavailable())

    def test_one_awake_monitor_is_enough(self):
        with mock.patch.object(self.executor, "_query_json", return_value=[
                {"dpmsStatus": False}, {"dpmsStatus": True}]):
            self.assertIsNone(self.executor._screen_unavailable())

    def test_clicking_a_sleeping_screen_is_refused(self):
        with mock.patch.object(self.executor, "_query_json", side_effect=lambda k: {
                "monitors": [{"focused": True, "x": 0, "y": 0, "width": 100,
                              "height": 100, "dpmsStatus": False}]}[k]), \
             mock.patch.object(self.executor, "_press_button") as press:
            result = self.executor.call("click_text", {"text": "Continue"})
        self.assertFalse(result.ok)
        self.assertIn("asleep", result.output)
        press.assert_not_called()


class TruncationTests(unittest.TestCase):
    """`hyprctl -j clients` runs ~750 characters per window; the old 4000-char
    cut silently handed the model unparseable JSON from about the fifth."""

    def test_a_long_client_list_survives_the_query(self):
        clients = [{"address": f"0x{i:x}", "class": "foot", "title": "t" * 200,
                    "pid": i, "floating": False, "fullscreen": 0,
                    "workspace": {"name": "1"}} for i in range(20)]
        executor = Executor(Config())
        with mock.patch.object(Executor, "_shell",
                               return_value=Result(True, json.dumps(clients))):
            result = executor.call("hypr_query", {"kind": "clients"})
        self.assertTrue(result.ok)
        parsed = json.loads(result.output)  # must not raise
        self.assertEqual(len(parsed), 20)

    def test_prose_output_is_still_capped_and_says_so(self):
        with mock.patch("subprocess.Popen") as popen:
            popen.return_value.communicate.return_value = ("x" * 9000, "")
            popen.return_value.returncode = 0
            result = Executor._shell(["echo"], limit=100)
        self.assertTrue(result.ok)
        self.assertIn("truncated", result.output)


class LoggedMistakeTests(unittest.TestCase):
    """Each of these is a call the assistant actually made in the session log."""

    def test_change_id_used_for_navigation_is_refused(self):
        error = _misused_change_id('hl.dsp.workspace.change_id({id = "5"})')
        self.assertIn("hl.dsp.focus", error)

    def test_change_id_with_only_a_workspace_is_refused(self):
        self.assertIsNotNone(
            _misused_change_id('hl.dsp.workspace.change_id({ workspace = "4" })'))

    def test_a_genuine_rename_is_allowed(self):
        self.assertIsNone(
            _misused_change_id('hl.dsp.workspace.change_id({ workspace = "2", id = "7" })'))

    def test_focus_is_untouched(self):
        self.assertIsNone(_misused_change_id('hl.dsp.focus({ workspace = "5" })'))

    def test_signature_placeholders_are_refused_with_advice(self):
        argv, error = normalise_omarchy(
            "launch-or-focus webapp <window-pattern> x https://x.com/")
        self.assertIn("<window-pattern>", error)
        self.assertIn("placeholder", error)

    def test_a_hyphenated_route_is_repaired(self):
        argv, error = normalise_omarchy("launch-or-focus webapp x https://x.com/")
        self.assertIsNone(error)
        self.assertEqual(argv[:4], ["launch", "or", "focus", "webapp"])

    def test_a_doubled_omarchy_prefix_is_dropped(self):
        argv, error = normalise_omarchy("omarchy omarchy launch terminal")
        self.assertIsNone(error)
        self.assertEqual(argv, ["launch", "terminal"])

    def test_describe_shows_the_command_that_will_run(self):
        self.assertEqual(
            Executor.describe("omarchy_cli", {"command": "launch-or-focus webapp x https://x.com/"}),
            "omarchy launch or focus webapp x https://x.com/")

    def test_a_less_than_sign_in_a_real_argument_is_not_a_placeholder(self):
        argv, error = normalise_omarchy('notification dismiss "<3"')
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
