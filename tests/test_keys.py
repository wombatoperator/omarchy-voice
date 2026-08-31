"""Key names, and the silent no-op they used to be.

`hl.dsp.send_shortcut({ key = "Enter" })` returns `ok` and presses nothing,
because there is no keysym called "Enter". Everything here exists so that
never again looks like success.

Run with: python3 -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from omarchy_voice.config import Config
from omarchy_voice.keys import canonical_keysym, normalise_key, normalise_mods
from omarchy_voice.tools import Executor, Result


class SpokenKeyNameTests(unittest.TestCase):
    def test_enter_becomes_return(self):
        """The bug. 'Enter' is the word people say and is not a keysym."""
        for said in ("Enter", "enter", "ENTER", "the enter key", "enter key"):
            with self.subTest(said=said):
                self.assertEqual(normalise_key(said), ("Return", None))

    def test_return_still_works(self):
        for said in ("Return", "return", "RETURN"):
            with self.subTest(said=said):
                self.assertEqual(normalise_key(said)[0], "Return")

    def test_abbreviations_people_say(self):
        for said, want in [("esc", "Escape"), ("del", "Delete"),
                           ("backspace", "BackSpace"), ("pgdn", "Page_Down"),
                           ("page down", "Page_Down"), ("page up", "Page_Up"),
                           ("space bar", "space"), ("print screen", "Print")]:
            with self.subTest(said=said):
                self.assertEqual(normalise_key(said), (want, None))

    def test_arrows_either_way_round(self):
        for said in ("up arrow", "arrow up", "Up", "up"):
            with self.subTest(said=said):
                self.assertEqual(normalise_key(said)[0], "Up")

    def test_punctuation_by_name_and_by_character(self):
        for said, want in [(".", "period"), ("dot", "period"), ("/", "slash"),
                           ("forward slash", "slash"), ("-", "minus"),
                           ("dash", "minus"), ("=", "equal"), (",", "comma")]:
            with self.subTest(said=said):
                self.assertEqual(normalise_key(said), (want, None))

    def test_letters_fold_to_the_unshifted_keysym(self):
        """CTRL+T is the t key with control held, not the capital-T keysym."""
        self.assertEqual(normalise_key("T")[0], "t")
        self.assertEqual(normalise_key("t")[0], "t")

    def test_function_keys_and_digits_pass_through(self):
        for said in ("F5", "f5", "5", "Tab", "Escape", "Home", "End"):
            with self.subTest(said=said):
                keysym, error = normalise_key(said)
                self.assertIsNone(error)
                self.assertTrue(keysym)


class RefusalTests(unittest.TestCase):
    def test_a_name_that_is_not_a_key_is_refused_not_pressed(self):
        keysym, error = normalise_key("frobnicate")
        self.assertIsNone(keysym)
        self.assertIn("nothing was pressed", error)

    def test_a_near_miss_is_offered_the_real_name(self):
        self.assertIn("Return", normalise_key("Entr")[1])

    def test_an_empty_key_is_refused(self):
        self.assertIsNone(normalise_key("")[0])

    def test_without_xkb_nothing_is_rejected(self):
        """A machine we cannot ask gets the alias table and the benefit of the
        doubt. Refusing every key would be a worse bug than the one being fixed."""
        with mock.patch("omarchy_voice.keys._xkb", return_value=None):
            self.assertEqual(normalise_key("enter"), ("Return", None))
            self.assertEqual(normalise_key("whatever this is"), ("whatever this is", None))


class ModifierTests(unittest.TestCase):
    def test_no_modifier_is_fine(self):
        self.assertEqual(normalise_mods(""), ("", None))

    def test_other_desktops_names_are_translated(self):
        for said, want in [("cmd", "SUPER"), ("command", "SUPER"), ("win", "SUPER"),
                           ("control", "CTRL"), ("option", "ALT"), ("meta", "SUPER")]:
            with self.subTest(said=said):
                self.assertEqual(normalise_mods(said), (want, None))

    def test_separators_do_not_matter(self):
        for said in ("CTRL SHIFT", "ctrl+shift", "Ctrl, Shift", "ctrl  shift"):
            with self.subTest(said=said):
                self.assertEqual(normalise_mods(said)[0], "CTRL SHIFT")

    def test_a_repeated_modifier_is_collapsed(self):
        self.assertEqual(normalise_mods("CTRL ctrl")[0], "CTRL")

    def test_an_unknown_modifier_is_refused(self):
        mods, error = normalise_mods("hyper")
        self.assertIsNone(mods)
        self.assertIn("not pressed", error)


class CanonicalNameTests(unittest.TestCase):
    def test_case_is_normalised_to_what_xkb_calls_it(self):
        self.assertEqual(canonical_keysym("return"), "Return")
        self.assertEqual(canonical_keysym("ESCAPE"), "Escape")

    def test_a_name_with_no_keysym_is_none(self):
        self.assertIsNone(canonical_keysym("Enter"))


class SendShortcutTests(unittest.TestCase):
    """What actually reaches hyprctl."""

    def setUp(self):
        self.executor = Executor(Config())
        self.sent = []

        def fake_shell(cmd, **kwargs):
            self.sent.append(cmd)
            return Result(True, "ok")

        patched = mock.patch.object(Executor, "_shell", staticmethod(fake_shell))
        patched.start()
        self.addCleanup(patched.stop)

    def last(self):
        return self.sent[-1][-1]

    def test_enter_dispatches_return(self):
        result = self.executor.call("send_shortcut",
                                    {"mods": "", "key": "Enter", "window": "activewindow"})
        self.assertTrue(result.ok)
        self.assertIn('key = "Return"', self.last())
        self.assertNotIn("Enter", self.last())

    def test_the_translation_is_reported_so_it_is_not_a_secret(self):
        result = self.executor.call("send_shortcut",
                                    {"mods": "", "key": "enter", "window": "activewindow"})
        self.assertIn("Return", result.output)

    def test_a_mere_case_fold_is_not_narrated(self):
        """"read 'T' as t" is noise the model would say out loud."""
        result = self.executor.call("send_shortcut",
                                    {"mods": "CTRL", "key": "T", "window": "activewindow"})
        self.assertNotIn("read", result.output)

    def test_a_bad_key_never_reaches_hyprctl(self):
        result = self.executor.call("send_shortcut",
                                    {"mods": "", "key": "Zorp", "window": "activewindow"})
        self.assertFalse(result.ok)
        self.assertEqual(self.sent, [])

    def test_a_bad_modifier_never_reaches_hyprctl(self):
        result = self.executor.call("send_shortcut",
                                    {"mods": "hyper", "key": "T", "window": "activewindow"})
        self.assertFalse(result.ok)
        self.assertEqual(self.sent, [])

    def test_the_same_check_applies_through_hypr_dispatch(self):
        """hypr_dispatch is the back door to every dispatcher, this one too."""
        result = self.executor.call("hypr_dispatch", {
            "lua": 'hl.dsp.send_shortcut({ mods = "", key = "Enter", window = "activewindow" })'})
        self.assertTrue(result.ok)
        self.assertIn('key = "Return"', self.last())

    def test_a_bad_key_through_hypr_dispatch_is_refused(self):
        result = self.executor.call("hypr_dispatch", {
            "lua": 'hl.dsp.send_shortcut({ mods = "", key = "Zorp", window = "activewindow" })'})
        self.assertFalse(result.ok)
        self.assertEqual(self.sent, [])

    def test_the_log_records_the_key_that_was_really_sent(self):
        self.executor.call("send_shortcut",
                           {"mods": "cmd", "key": "Enter", "window": "activewindow"})
        self.assertIn("press SUPER+Return", self.executor.transcript[-1])

    def test_dry_run_refuses_a_bad_key_too(self):
        executor = Executor(Config(dry_run=True))
        result = executor.call("send_shortcut",
                               {"mods": "", "key": "Zorp", "window": "activewindow"})
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
