"""Config loading: unknown keys, additive policy lists."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from omarchy_voice import config as cfg
from omarchy_voice.config import DEFAULT_CONFIRM, DEFAULT_DENY


class ConfigLoadTests(unittest.TestCase):
    def write(self, text: str) -> Path:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "config.toml"
        path.write_text(text)
        return path

    def test_unknown_keys_are_kept_for_doctor(self):
        path = self.write('[ears]\nenginee = "realtime"\n')
        loaded = cfg.load(path)
        self.assertIn("enginee", loaded.unknown_keys)

    def test_a_retired_key_is_not_reported_as_a_typo(self):
        # Listening is toggle-only now. Every config written before that says
        # `mode = "push"`, and none of them should make doctor shout about it.
        path = self.write('[ears]\nmode = "push"\n')
        loaded = cfg.load(path)
        self.assertEqual(loaded.unknown_keys, [])
        self.assertIn("mode", loaded.retired_keys)

    def test_a_retired_key_still_gets_explained(self):
        self.assertIn("toggle", cfg.RETIRED_KEYS["mode"])

    def test_there_is_no_always_on_setting(self):
        path = self.write('[ears]\nmode = "always"\n')
        loaded = cfg.load(path)
        self.assertFalse(hasattr(loaded, "mode"))

    def test_confirm_patterns_union_with_defaults(self):
        path = self.write('[hands]\nconfirm_patterns = ["\\\\bformat\\\\b"]\n')
        loaded = cfg.load(path)
        self.assertIn(r"\bformat\b", loaded.confirm_patterns)
        for builtin in DEFAULT_CONFIRM:
            self.assertIn(builtin, loaded.confirm_patterns)

    def test_confirm_patterns_replace_drops_defaults(self):
        path = self.write(
            '[hands]\n'
            'confirm_patterns = ["\\\\bformat\\\\b"]\n'
            'confirm_patterns_replace = true\n'
        )
        loaded = cfg.load(path)
        self.assertEqual(loaded.confirm_patterns, [r"\bformat\b"])
        self.assertNotIn(r"\breboot\b", loaded.confirm_patterns)

    def test_deny_patterns_union_with_defaults(self):
        path = self.write('[hands]\ndeny_patterns = ["\\\\bwipe\\\\b"]\n')
        loaded = cfg.load(path)
        self.assertIn(r"\bwipe\b", loaded.deny_patterns)
        self.assertIn(DEFAULT_DENY[0], loaded.deny_patterns)


if __name__ == "__main__":
    unittest.main()
