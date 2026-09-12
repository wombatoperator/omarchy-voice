import unittest
from unittest import mock
from omarchy_voice.config import Config
from omarchy_voice.tools import Executor, _terminal_program_error

class TerminalPreflightTests(unittest.TestCase):
    def test_missing_program_is_identified_before_launcher_runs(self):
        ex = Executor(Config())
        with mock.patch('omarchy_voice.tools.shutil.which', side_effect=lambda name: '/usr/bin/btop' if name=='btop' else None), mock.patch.object(ex, '_shell') as shell:
            result=ex._tool_omarchy_cli('launch or focus tui computerstats')
        self.assertFalse(result.ok)
        self.assertIn('not installed',result.output)
        self.assertIn('btop',result.output)
        shell.assert_not_called()

    def test_app_id_is_a_label_not_the_program(self):
        with mock.patch('omarchy_voice.tools.shutil.which', side_effect=lambda name: '/usr/bin/btop' if name=='btop' else None):
            self.assertIsNone(_terminal_program_error(['launch','tui','--app-id=ComputerStats','btop']))

    def test_default_shell_and_other_launches_are_unaffected(self):
        self.assertIsNone(_terminal_program_error(['launch','terminal']))
        self.assertIsNone(_terminal_program_error(['launch','browser','https://example.com']))
