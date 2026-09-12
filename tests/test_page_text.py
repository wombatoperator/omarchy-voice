import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from omarchy_voice import page_text
from omarchy_voice.config import Config
from omarchy_voice.tools import Executor, Result


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.original = b"previous primary text\n"
        self.value = self.original
        self.writes = []
        self.focused = True
        self.formats = b"text/plain\ntext/plain;charset=utf-8\nUTF8_STRING\n"
        def read(*args, **kwargs):
            return self.formats if "--list-types" in args else self.value
        def write(value, **kwargs):
            self.value = value
            self.writes.append(value)
        for name, fn in (("_read", read), ("_write", write)):
            patcher = mock.patch.object(page_text, name, side_effect=fn)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_fresh_selection_is_read_and_previous_text_restored(self):
        def select():
            self.value = b"1. Game A\n2. Game B"
        text = page_text.read_selection(select, lambda: self.focused)
        self.assertEqual(text, "1. Game A\n2. Game B")
        self.assertEqual(self.value, self.original)

    def test_stale_clipboard_cannot_be_mistaken_for_page(self):
        with self.assertRaisesRegex(page_text.SelectionError, "fresh"):
            page_text.read_selection(lambda: None, lambda: True, timeout=0)
        self.assertEqual(self.value, self.original)

    def test_regular_clipboard_copy_is_restored(self):
        def copy():
            self.value = b'fresh article'
        self.assertEqual(page_text.read_selection(copy, lambda: True, primary=False), 'fresh article')
        self.assertEqual(self.value, self.original)
        self.assertTrue(all(call.kwargs == {'primary': False} for call in page_text._read.call_args_list))

    def test_rich_regular_clipboard_is_not_overwritten(self):
        self.formats += b'text/html\n'
        copy = mock.Mock()
        with self.assertRaises(page_text.SelectionError):
            page_text.read_selection(copy, lambda: True, primary=False)
        copy.assert_not_called()
        self.assertEqual(self.writes, [])

    def test_new_user_copy_is_preserved(self):
        def copy():
            self.focused = False
            self.value = b'user copy'
        with self.assertRaises(page_text.SelectionError):
            page_text.read_selection(copy, lambda: self.focused, primary=False)
        self.assertEqual(self.value, b'user copy')

    def test_rich_primary_content_is_untouched(self):
        self.formats += b"text/html\n"
        select = mock.Mock()
        with self.assertRaises(page_text.SelectionError):
            page_text.read_selection(select, lambda: True)
        select.assert_not_called()
        self.assertEqual(self.writes, [])

    def test_focus_change_does_not_overwrite_new_selection(self):
        def select():
            self.focused = False
            self.value = b"user selected something else"
        with self.assertRaisesRegex(page_text.SelectionError, "lost focus"):
            page_text.read_selection(select, lambda: self.focused)
        self.assertEqual(self.value, b"user selected something else")

    def test_selection_failure_restores_marker(self):
        with self.assertRaisesRegex(page_text.SelectionError, "shortcut failed"):
            page_text.read_selection(mock.Mock(side_effect=page_text.SelectionError("shortcut failed")), lambda: True)
        self.assertEqual(self.value, self.original)

    def test_empty_primary_is_restored_to_empty(self):
        self.formats = b""
        self.value = None
        def select():
            self.value = b"page contents"
        page_text.read_selection(select, lambda: True)
        self.assertIsNone(self.value)

    def test_all_clipboard_commands_use_primary(self):
        # Exercise real command builders, not the selection fixture mocks.
        self.doCleanups()
        with mock.patch.object(page_text.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=b"text")) as run:
            page_text._read("--list-types")
            page_text._write(b"text")
            page_text._write(None)
        for call in run.call_args_list:
            self.assertIn("--primary", call.args[0])


class DesktopRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.ex = Executor(Config())
        self.window = {"address": "0x123", "class": "chrome-store-Default", "workspace": {"name": "6"}, "focusHistoryID": 0}
        self.ex._screen_unavailable = lambda: None
        self.ex._window_geometry = lambda target: (self.window, "")
        self.ex._visible_workspaces = lambda: {"6"}
        self.ex._query_json = lambda what: [self.window]
        self.ex._dispatch_lua = mock.Mock(return_value=Result(True, "ok"))
        self.ex._shell = mock.Mock(return_value=Result(True, ""))
        self.ex._tool_read_screen = mock.Mock(return_value=Result(True, "Steam Top Sellers"))

    def test_recovery_hides_exact_panel_then_reads_target(self):
        with mock.patch("time.sleep"):
            result = self.ex.call("reveal_window", {"target": "address:0x123", "panel": "audio"})
        self.assertTrue(result.ok)
        self.ex._shell.assert_called_once_with(["omarchy", "shell", "shell", "hide", "omarchy.audio"], timeout=3)
        self.ex._tool_read_screen.assert_called_once_with("address:0x123")
        self.assertIn("Steam Top Sellers", result.output)

    def test_recovery_failure_does_not_claim_visibility(self):
        self.ex._shell.return_value = Result(False, "shell unavailable")
        result = self.ex.call("reveal_window", {"target": "address:0x123", "panel": "audio"})
        self.assertFalse(result.ok)
        self.ex._tool_read_screen.assert_not_called()

    def test_unrecognized_panel_or_address_is_refused(self):
        for args in ({"target": "activewindow"}, {"target": "address:0x123", "panel": "lock"}):
            self.assertFalse(self.ex.call("reveal_window", args).ok)
        self.ex._dispatch_lua.assert_not_called()

    def test_dry_run_never_moves_focus_or_selection(self):
        self.ex.config.dry_run = True
        for name in ("reveal_window", "read_page_text"):
            self.assertTrue(self.ex.call(name, {"target": "address:0x123"}).ok)
        self.ex._dispatch_lua.assert_not_called()

    def test_exact_read_is_preferred_to_ocr_when_opening_page(self):
        with mock.patch.object(self.ex, "_exact_page_text", return_value=Result(True, "Steam ranking " * 30)), \
             mock.patch.object(self.ex, "_ocr_region") as ocr, mock.patch("time.sleep"):
            result = self.ex._read_web_window(self.window)
        self.assertTrue(result.ok)
        ocr.assert_not_called()

    def test_missing_selection_support_falls_back_once(self):
        with mock.patch.object(page_text, "read_selection", side_effect=page_text.SelectionError("no primary")):
            result = self.ex.call("read_page_text", {"target": "address:0x123"})
        self.assertTrue(result.ok)
        self.assertIn("OCR fallback", result.output)
        self.ex._tool_read_screen.assert_called_once()

    def test_non_browser_never_gets_select_all(self):
        self.window["class"] = "foot"
        with mock.patch.object(page_text, "read_selection") as select:
            self.ex.call("read_page_text", {"target": "address:0x123"})
        select.assert_not_called()
        self.ex._dispatch_lua.assert_not_called()

    def test_repeat_selection_uses_copy_fallback_once(self):
        with mock.patch.object(page_text, 'read_selection', side_effect=[
                page_text.SelectionError('browser did not provide fresh selection text'), 'full article']) as read:
            result = self.ex.call('read_page_text', {'target': 'address:0x123'})
            read.call_args_list[1].args[0]()
        self.assertIn('full article', result.output)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(read.call_args_list[1].kwargs, {'primary': False})
        self.ex._tool_read_screen.assert_not_called()

class PasteTests(unittest.TestCase):
    setUp = SelectionTests.setUp
    def test_exact_unicode_paste_preserves_clipboard(self):
        observed = []
        with mock.patch.object(page_text.time, 'sleep'):
            page_text.paste_text('café — 42\nnext', lambda: observed.append(self.value), lambda: self.focused)
        self.assertEqual(observed, ['café — 42\nnext'.encode()])
        self.assertEqual(self.value, self.original)

    def test_paste_refuses_rich_clipboard_without_touching_it(self):
        self.formats = b'image/png\n'
        paste = mock.Mock()
        with self.assertRaisesRegex(page_text.SelectionError, 'rich/binary'):
            page_text.paste_text('text', paste, lambda: True)
        paste.assert_not_called()
        self.assertEqual(self.writes, [])

    def test_paste_preserves_new_user_copy(self):
        def paste():
            self.value = b'new user copy'
        with mock.patch.object(page_text.time, 'sleep'):
            page_text.paste_text('text', paste, lambda: True)
        self.assertEqual(self.value, b'new user copy')

    def test_focus_loss_before_paste_never_types(self):
        paste = mock.Mock()
        with self.assertRaisesRegex(page_text.SelectionError, 'lost focus'):
            page_text.paste_text('text', paste, lambda: False)
        paste.assert_not_called()
        self.assertEqual(self.writes, [])

    def test_paste_failure_restores_original(self):
        with self.assertRaises(RuntimeError):
            page_text.paste_text('text', mock.Mock(side_effect=RuntimeError('failed')), lambda: True)
        self.assertEqual(self.value, self.original)


class BrowserTypingTests(unittest.TestCase):
    def test_xwayland_browser_uses_exact_paste_with_targeted_shortcut(self):
        ex = Executor(Config())
        ex._window_geometry = mock.Mock(return_value=({'address':'0x123','class':'Google-chrome','xwayland':True}, ''))
        ex._tool_send_shortcut = mock.Mock(return_value=Result(True, 'ok'))
        ex._shell = mock.Mock()
        def paste(text, shortcut, focused):
            self.assertEqual(text, 'Ben Shelton score')
            self.assertTrue(focused())
            shortcut()
        with mock.patch.object(page_text, 'paste_text', side_effect=paste):
            self.assertTrue(ex._tool_type_text('Ben Shelton score').ok)
        ex._tool_send_shortcut.assert_called_once_with('CTRL', 'v', 'address:0x123')
        ex._shell.assert_not_called()

    def test_failed_paste_is_not_replayed_as_keystrokes(self):
        ex = Executor(Config())
        ex._window_geometry = mock.Mock(return_value=({'address':'0x123','class':'Google-chrome','xwayland':True}, ''))
        ex._shell = mock.Mock()
        with mock.patch.object(page_text, 'paste_text', side_effect=page_text.SelectionError('rich clipboard')):
            self.assertFalse(ex._tool_type_text('text').ok)
        ex._shell.assert_not_called()
