"""Offline navigation decisions, synthetic inventories, and execution boundaries."""
import contextlib
import copy
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from omarchy_voice import navigation as nav
from omarchy_voice.config import Config
from omarchy_voice.security import dispatch_literal_error
from omarchy_voice.tools import Executor, TOOL_SCHEMAS
from navigation_cases import INVENTORY, cases
import bench_navigation as bench


def response(action='workspace', **slots):
    answers = {}
    for name, question in nav.questions(INVENTORY).items():
        if question['type'] == 'noul':
            answers[name] = {'type': 'noul', 'noul': 1.0}
        else:
            value = action if name == 'action' else slots.get(name, 'none')
            answers[name] = {'type': 'choice', 'choice': value, 'confidence': 1.0,
                             'probabilities': {k: float(k == value) for k in question['criteria']}}
    return {'answers': answers}


class NavigationTests(unittest.TestCase):
    def test_workspace_switch_never_renames_and_move_preserves_follow(self):
        result = nav.decide(response(workspace='4'), INVENTORY)
        self.assertEqual(result['call'], {'name': 'hypr_dispatch', 'arguments': {'lua': 'hl.dsp.focus({ workspace = "4" })'}})
        for action, flag in (('move_workspace', 'true'), ('send_workspace', 'false')):
            result = nav.decide(response(action, workspace='4'), INVENTORY)
            self.assertIn('follow = ' + flag, result['call']['arguments']['lua'])

    def test_all_development_cases_compile_to_existing_tools_and_literal_dispatches(self):
        schemas = {s['name']: s['input_schema'] for s in TOOL_SCHEMAS}
        for case in cases():
            if case['expected']['action'] == 'fallback':
                continue
            selected = case['expected']
            decision = nav.decide(response(**selected), INVENTORY)
            self.assertTrue(decision['accepted'], case['id'])
            call = decision['call']
            self.assertLessEqual(set(schemas[call['name']].get('required', [])), set(call['arguments']))
            if call['name'] == 'hypr_dispatch':
                self.assertIsNone(dispatch_literal_error(call['arguments']['lua']), case['id'])

    def test_required_slot_uncertainty_falls_back_but_unused_slots_do_not(self):
        data = response(workspace='4')
        data['answers']['workspace']['confidence'] = .4
        self.assertFalse(nav.decide(data, INVENTORY)['accepted'])
        data = response('close')
        data['answers']['workspace']['confidence'] = .4
        self.assertTrue(nav.decide(data, INVENTORY)['accepted'])
        data['answers']['supported']['noul'] = .4
        self.assertFalse(nav.decide(data, INVENTORY)['accepted'])

    def test_missing_slots_unknown_labels_and_malformed_probabilities_fail_closed(self):
        self.assertFalse(nav.decide(response(), INVENTORY)['accepted'])
        for value in (float('nan'), float('inf'), True, -1, 2, '1'):
            data = response(workspace='4')
            data['answers']['action']['confidence'] = value
            self.assertFalse(nav.decide(data, INVENTORY)['valid'])
        for data in ({}, None, {'answers': []}):
            self.assertFalse(nav.decide(data, INVENTORY)['valid'])
        data = response(workspace='4')
        data['answers']['action']['choice'] = 'run_shell'
        self.assertFalse(nav.decide(data, INVENTORY)['valid'])

    def test_capability_absence_and_inventory_overflow_do_not_guess(self):
        inventory = {**INVENTORY, 'dispatchers': [], 'routes': [], 'active_window': ''}
        self.assertNotIn('close', nav.available(inventory))
        self.assertNotIn('workspace', nav.questions(inventory)['action']['criteria'])
        inventory = {**INVENTORY, 'apps': [{'id': f'example-{i}'} for i in range(255)]}
        with self.assertRaises(ValueError):
            nav.payload('Open a calculator', inventory)

    def test_inventory_values_cannot_supply_code(self):
        inventory = copy.deepcopy(INVENTORY)
        inventory['windows'][1]['address'] = '0x102" }); hl.dsp.exec_cmd("bad")'
        self.assertIsNone(nav.compile_selection({'action': 'focus_window', 'window': 'w1'}, inventory))
        inventory['apps'][0]['id'] = 'example; bad'
        self.assertIsNone(nav.compile_selection({'action': 'launch_app', 'app': 'a0'}, inventory))

    def test_policy_deny_confirmation_and_dry_run_are_preserved(self):
        call = nav.compile_selection({'action': 'close'}, INVENTORY)
        for field in ('deny_patterns', 'confirm_patterns'):
            config = Config(dry_run=True, **{field: [r'window\.close']})
            executor = Executor(config)
            with mock.patch.object(executor, '_dispatch_lua') as dispatch:
                result = executor.call(call['name'], call['arguments'])
            self.assertFalse(result.ok)
            dispatch.assert_not_called()
            self.assertEqual(bool(executor.pending), field == 'confirm_patterns')

    def test_benchmark_defaults_are_offline_and_do_not_read_keys(self):
        with mock.patch.object(bench, 'read_key') as key, mock.patch.object(bench, 'Client') as client, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bench.main([]), 0)
        key.assert_not_called()
        client.assert_not_called()

    def test_ground_truth_does_not_enter_provider_payload(self):
        case = cases()[0]
        body = nav.payload(case['request'], INVENTORY)
        self.assertNotIn('expected', body)
        self.assertNotIn('expected', body['state'])
        self.assertEqual(set(body['questions']), {'action', 'workspace', 'window', 'direction', 'monitor', 'app', 'group_index', 'supported'})


if __name__ == '__main__':
    unittest.main()
