"""Searching, and the session where searching did not work.

From the log, 2026-08-31 11:54 — asked to search for something, the assistant
pressed CTRL+T, typed the query, and tried Return five ways:

    action  press CTRL+t in activewindow
    action  type 'SpaceX stock price and earnings calendar'
    action  press Return in activewindow      (x3, then KP_Enter, then Escape)
    action  click left on 'spacex stock price earnings report'   (x4)
    guard   stopped after 12 tool rounds with no new user turn

The key was never the problem. The window was opened by `omarchy launch webapp`,
which is `chrome --app=<url>`: an app window with no tab bar and no address bar,
so CTRL+T and CTRL+L are no-ops and there was nowhere for the text to go. The
fix is to stop typing — put the query in the URL.

Run with: python3 -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from omarchy_voice.config import Config
from omarchy_voice.tools import (
    RESTORE_BUBBLE, SEARCH_SCOPES, VISUAL_SCOPES, Executor, Result,
)

WINDOW = {
    "address": "0xfeed", "class": "chrome-www.google.com__search-Default",
    "title": "spacex - Google Search", "at": [0, 0], "size": [1200, 900],
    "workspace": {"name": "1"}, "focusHistoryID": 0,
}


class SearchingExecutor(Executor):
    """An executor whose browser always opens, so the wiring can be tested."""

    _DEFAULT = object()

    def __init__(self, config=None, window=_DEFAULT,
                 page_text="Al Overview\nresults here"):
        super().__init__(config or Config())
        self.launched: list[list[str]] = []
        self.closed: list[str] = []
        self._window = dict(WINDOW) if window is self._DEFAULT else window
        self._page_text = page_text
        self._session_is_locked = lambda: False

    def _shell(self, cmd, **kwargs):          # type: ignore[override]
        self.launched.append(cmd)
        return Result(True, "started")

    def _query_json(self, kind):
        return [self._window] if self._window else []

    def _await_new_window(self, before, timeout, hint=""):
        return self._window["address"] if self._window else None

    def _ocr_region(self, geometry):
        return Result(True, self._page_text)

    def _dispatch_lua(self, lua):
        if "window.close" in lua:
            self.closed.append(lua)
        return Result(True, "ok")


def search(**kwargs):
    ex = SearchingExecutor(**{k: v for k, v in kwargs.items()
                              if k in ("config", "window", "page_text")})
    return ex


class QueryGoesInTheUrlTests(unittest.TestCase):
    def test_nothing_is_typed_and_no_shortcut_is_pressed(self):
        """The whole point. Typing was the bug."""
        ex = search()
        with mock.patch("time.sleep"):
            ex.call("web_search", {"query": "spacex next earnings"})
        flat = " ".join(" ".join(c) for c in ex.launched)
        self.assertNotIn("wtype", flat)
        self.assertNotIn("send_shortcut", flat)

    def test_the_query_is_url_encoded_into_the_search_url(self):
        ex = search()
        with mock.patch("time.sleep"):
            ex.call("web_search", {"query": "spacex next earnings"})
        url = ex.launched[0][-1]
        self.assertIn("q=spacex+next+earnings", url)

    def test_a_query_with_punctuation_cannot_add_url_parameters(self):
        """An & or ? spoken into the query must not become part of the URL's
        own grammar — the whole query has to stay inside one q= value."""
        ex = search()
        with mock.patch("time.sleep"):
            ex.call("web_search", {"query": 'what is "1 & 2"? &tbm=isch'})
        url = ex.launched[0][-1]
        query_value = url.split("q=", 1)[1]
        self.assertNotIn(" ", url)
        self.assertNotIn("&", query_value)
        self.assertNotIn("?", query_value)
        self.assertIn("%26", query_value)

    def test_it_opens_a_window_not_a_tab(self):
        """`omarchy launch browser <url>` opens a tab inside a window that
        already exists — invisible to hyprctl, so it cannot be waited for."""
        ex = search()
        with mock.patch("time.sleep"):
            ex.call("web_search", {"query": "anything"})
        self.assertEqual(ex.launched[0][:3], ["omarchy", "launch", "webapp"])

    def test_every_scope_has_a_url(self):
        from omarchy_voice.tools import TOOL_SCHEMAS
        schema = next(t for t in TOOL_SCHEMAS if t["name"] == "web_search")
        listed = schema["input_schema"]["properties"]["scope"]["enum"]
        self.assertEqual(sorted(listed), sorted(SEARCH_SCOPES))

    def test_scopes_reach_different_pages(self):
        seen = set()
        for scope in SEARCH_SCOPES:
            ex = search()
            with mock.patch("time.sleep"):
                ex.call("web_search", {"query": "x", "scope": scope})
            seen.add(ex.launched[0][-1])
        self.assertEqual(len(seen), len(SEARCH_SCOPES))

    def test_an_unknown_scope_is_refused(self):
        ex = search()
        result = ex.call("web_search", {"query": "x", "scope": "telepathy"})
        self.assertFalse(result.ok)
        self.assertEqual(ex.launched, [])

    def test_an_empty_query_is_refused(self):
        self.assertFalse(search().call("web_search", {"query": "  "}).ok)


class ResultsComeBackTests(unittest.TestCase):
    def test_the_page_text_is_returned(self):
        ex = search(page_text="Al Overview SpaceX's next earnings report is November 3")
        with mock.patch("time.sleep"):
            result = ex.call("web_search", {"query": "spacex earnings"})
        self.assertTrue(result.ok)
        self.assertIn("November 3", result.output)

    def test_the_window_address_comes_back_so_it_can_be_followed_up(self):
        ex = search()
        with mock.patch("time.sleep"):
            result = ex.call("web_search", {"query": "x"})
        self.assertIn("0xfeed", result.output)

    def test_pictures_are_not_described_from_ocr(self):
        for scope in VISUAL_SCOPES:
            with self.subTest(scope=scope):
                ex = search()
                with mock.patch("time.sleep"):
                    result = ex.call("web_search", {"query": "rubber duck", "scope": scope})
                self.assertIn("look", result.output)

    def test_a_browser_that_never_opens_is_reported_not_assumed(self):
        ex = SearchingExecutor(window=None)
        with mock.patch("time.sleep"):
            result = ex.call("web_search", {"query": "x"})
        self.assertFalse(result.ok)
        self.assertIn("did not open", result.output)

    def test_results_that_cannot_be_read_still_report_the_window(self):
        ex = SearchingExecutor()
        ex._ocr_region = lambda geometry: Result(False, "no readable text")
        with mock.patch("time.sleep"):
            result = ex.call("web_search", {"query": "x"})
        self.assertTrue(result.ok)
        self.assertIn("on screen", result.output)

    def test_the_crash_restore_bubble_is_called_out(self):
        """Chromium's 'Restore pages' prompt covers the top of the page, which
        is where search results are. It cost a whole turn in the log."""
        ex = search(page_text="Restore pages? Chrome didn't shut down correctly")
        with mock.patch("time.sleep"):
            result = ex.call("web_search", {"query": "x"})
        self.assertIn("Restore pages", result.output)
        self.assertIn("Escape", result.output)
        self.assertIn(RESTORE_BUBBLE, result.output.lower())


class OneSearchWindowTests(unittest.TestCase):
    def test_the_previous_search_window_is_replaced_not_stacked(self):
        ex = search()
        with mock.patch("time.sleep"):
            ex.call("web_search", {"query": "first"})
            ex.call("web_search", {"query": "second"})
        self.assertEqual(len(ex.closed), 1)
        self.assertIn("0xfeed", ex.closed[0])

    def test_the_first_search_closes_nothing(self):
        ex = search()
        with mock.patch("time.sleep"):
            ex.call("web_search", {"query": "first"})
        self.assertEqual(ex.closed, [])

    def test_only_its_own_window_is_closed(self):
        """Matching by class would also take down a duckduckgo pane the user
        asked for by name; closing a window somebody wanted is the worse bug."""
        ex = search()
        with mock.patch("time.sleep"):
            ex.call("web_search", {"query": "first"})
        ex._last_search_window = None          # as after a daemon restart
        with mock.patch("time.sleep"):
            ex.call("web_search", {"query": "second"})
        self.assertEqual(ex.closed, [])


class ProfileErrorDialogTests(unittest.TestCase):
    """Chrome's "Profile error occurred" box. Not corruption — the profile's
    databases check out `integrity: ok`. It is lock contention between two
    browser processes racing for the same profile, which is what launching
    windows back to back causes."""

    def setUp(self):
        self.executor = Executor(Config())
        self.closed = []
        self.executor._dispatch_lua = lambda lua: (
            self.closed.append(lua), Result(True, "ok"))[1]

    def windows(self, *clients):
        self.executor._query_json = lambda kind: list(clients)

    def test_the_dialog_is_closed_on_sight(self):
        self.windows({"address": "0xbad", "class": "", "title": "Profile error occurred"})
        self.assertEqual(self.executor._dismiss_browser_error_dialogs(), 1)
        self.assertIn("0xbad", self.closed[0])

    def test_a_real_window_is_never_closed_however_it_is_titled(self):
        """Matched on an empty class as well as the title, so a page that
        happens to be about profile errors is safe."""
        self.windows({"address": "0xgood", "class": "chrome-example.com__-Default",
                      "title": "Profile error occurred - Stack Overflow"})
        self.assertEqual(self.executor._dismiss_browser_error_dialogs(), 0)
        self.assertEqual(self.closed, [])

    def test_other_unclassed_dialogs_are_left_alone(self):
        self.windows({"address": "0xdlg", "class": "", "title": "Save file as"})
        self.assertEqual(self.executor._dismiss_browser_error_dialogs(), 0)

    def test_several_are_cleared_at_once(self):
        self.windows({"address": "0xa", "class": "", "title": "Profile error occurred"},
                     {"address": "0xb", "class": None, "title": "Profile error occurred"})
        self.assertEqual(self.executor._dismiss_browser_error_dialogs(), 2)


class OpenPageTests(unittest.TestCase):
    def test_it_opens_a_real_window(self):
        ex = search()
        with mock.patch("time.sleep"):
            result = ex.call("open_page", {"url": "https://platform.openai.com/usage"})
        self.assertTrue(result.ok)
        self.assertEqual(ex.launched[0][:3], ["omarchy", "launch", "webapp"])
        self.assertIn("0xfeed", result.output)

    def test_reading_can_be_skipped(self):
        ex = search()
        ex._ocr_region = lambda geometry: self.fail("should not have read the page")
        with mock.patch("time.sleep"):
            result = ex.call("open_page", {"url": "https://example.test", "read": False})
        self.assertTrue(result.ok)

    def test_only_http_urls(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x.test", "notaurl"):
            with self.subTest(url=url):
                ex = search()
                self.assertFalse(ex.call("open_page", {"url": url}).ok)
                self.assertEqual(ex.launched, [])

    def test_open_page_does_not_disturb_the_search_window(self):
        ex = search()
        with mock.patch("time.sleep"):
            ex.call("web_search", {"query": "first"})
            ex.call("open_page", {"url": "https://example.test"})
        self.assertEqual(ex.closed, [])


class GateTests(unittest.TestCase):
    def test_a_search_is_described_for_the_log(self):
        self.assertIn("rubber duck", Executor.describe(
            "web_search", {"query": "rubber duck", "scope": "images"}))

    def test_an_opened_page_is_described_for_the_log(self):
        self.assertIn("example.test",
                      Executor.describe("open_page", {"url": "https://example.test"}))

    def test_dry_run_does_not_open_a_browser(self):
        ex = SearchingExecutor(Config(dry_run=True))
        result = ex.call("web_search", {"query": "x"})
        self.assertIn("dry-run", result.output)
        self.assertEqual(ex.launched, [])


if __name__ == "__main__":
    unittest.main()
