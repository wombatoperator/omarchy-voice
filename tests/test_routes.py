"""The `omarchy voice ...` wrappers.

The bare route's file is named omarchy-voice, the same as the CLI, and it wins
the PATH lookup wherever the omarchy bin directory comes before ~/.local/bin.
Quickshell's PATH and the session PATH Hyprland inherits can both do that, so
the bar widget's click and the keybinding end up here: the wrapper has to
behave like the CLI when it is handed a command line, and only fall back to
`status` when it is called bare as `omarchy voice`.
"""

import os
import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTES = ROOT / "omarchy" / "bin"


def run(route, *args):
    env = dict(os.environ, OMARCHY_VOICE_SRC=str(ROOT / "src"))
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [str(ROUTES / route), *args],
        capture_output=True, text=True, timeout=60, env=env)


class BareRouteTests(unittest.TestCase):

    def test_a_command_line_is_passed_through_not_appended_to_status(self):
        # `exec ... status "$@"` made every argument an argparse error, which
        # is what the bar widget's click and the keybinding were getting.
        result = run("omarchy-voice", "--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("omarchy-voice", result.stdout)
        self.assertNotIn("unrecognized arguments", result.stderr)

    def test_a_subcommand_is_understood(self):
        # Read-only stand-in for the `listen toggle` the widget and binding
        # send; under the old wrapper this became `status log`, an argparse error.
        result = run("omarchy-voice", "log")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("unrecognized arguments", result.stderr)

    def test_no_arguments_is_still_the_status_route(self):
        result = run("omarchy-voice")
        self.assertEqual(result.returncode, 0, result.stderr)


class NamedRouteTests(unittest.TestCase):

    def test_every_route_carries_the_metadata_omarchy_reads(self):
        for route in sorted(ROUTES.iterdir()):
            with self.subTest(route=route.name):
                text = route.read_text()
                self.assertIn("# omarchy:summary=", text)
                self.assertIn("# omarchy:group=voice", text)
                self.assertTrue(os.access(route, os.X_OK))


if __name__ == "__main__":
    unittest.main()
