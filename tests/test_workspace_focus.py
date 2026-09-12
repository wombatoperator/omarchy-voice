import json
import sys
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from omarchy_voice.config import Config
from omarchy_voice.tools import Executor, Result


class WorkspaceFocusTests(unittest.TestCase):
    def setUp(self):
        self.ex = Executor(Config())
        self.ex._dispatch_lua = mock.Mock(return_value=Result(True, 'ok'))

    def test_existing_workspace_cycle_noop_is_not_reported_as_a_move(self):
        self.ex._tool_hypr_query = mock.Mock(return_value=Result(True, json.dumps({'name':'2'})))
        result = self.ex._tool_hypr_dispatch('hl.dsp.focus({ workspace = "e+1" })')
        self.assertFalse(result.ok)
        self.assertIn('still on workspace 2', result.output)

    def test_numeric_destination_is_verified(self):
        self.ex._tool_hypr_query = mock.Mock(side_effect=[Result(True, '{"name":"2"}'), Result(True, '{"name":"6"}')])
        with mock.patch('time.sleep'):
            result = self.ex._tool_hypr_dispatch('hl.dsp.focus({ workspace = "6" })')
        self.assertTrue(result.ok)
        self.assertIn('6 is active', result.output)

    def test_unreadable_state_cannot_confirm_a_move(self):
        self.ex._tool_hypr_query = mock.Mock(return_value=Result(False, 'disconnected'))
        self.assertFalse(self.ex._tool_hypr_dispatch('hl.dsp.focus({ workspace = "6" })').ok)
