import asyncio
import json
import os
import unittest
from unittest import mock

from omarchy_voice.browser import BrowserSurface, BrowserWorker, MODEL, cost
from omarchy_voice.config import Config
from omarchy_voice.tools import Result


class SurfaceTests(unittest.TestCase):
    def setUp(self):
        self.ex = mock.Mock()
        self.ex._screen_unavailable.return_value = None
        self.window = {'address': '0xab', 'at': [100, 200], 'size': [800, 600],
                       'class': 'google-chrome', 'focusHistoryID': 0}
        self.ex._window_geometry.return_value = (self.window, '')
        self.ex.call.return_value = Result(True, 'ok')
        self.ex._press_button.return_value = Result(True, 'ok')
        self.ex._shell.return_value = Result(True, 'ok')
        self.surface = BrowserSurface(self.ex, lambda: True, mock.Mock())
        self.surface.target = 'address:0xab'
        self.surface.geometry = (100, 200, 800, 600)

    def test_window_coordinates_translate_to_desktop(self):
        self.surface.perform({'type': 'click', 'x': 200, 'y': 150, 'button': 'left'})
        self.assertIn('x = 300, y = 350', self.ex.call.call_args.args[1]['lua'])
        self.ex._press_button.assert_called_once_with('left', False)

    def test_input_requires_an_observed_screenshot(self):
        self.surface.geometry = None
        with self.assertRaisesRegex(ValueError, "Observe the browser"):
            self.surface.perform({'type': 'click', 'x': 1, 'y': 2})
        self.ex.call.assert_not_called()

    def test_scaled_screenshot_coordinates_map_back_to_window(self):
        self.surface.image_size = (400, 300)
        self.assertEqual(self.surface._point({'x': 100, 'y': 75}), (300, 350))

    def test_mouse_modifiers_are_not_silently_ignored(self):
        with self.assertRaisesRegex(ValueError, 'Modified mouse'):
            self.surface.perform({'type': 'click', 'x': 1, 'y': 2, 'keys': ['CTRL']})
        self.ex.call.assert_not_called()

    def test_focus_change_prevents_typing(self):
        self.window['focusHistoryID'] = 1
        with self.assertRaisesRegex(RuntimeError, 'lost focus'):
            self.surface.perform({'type': 'type', 'text': 'hello'})
        self.ex.call.assert_not_called()

    def test_resized_window_prevents_stale_clicks(self):
        self.window['size'] = [900, 600]
        with self.assertRaisesRegex(RuntimeError, 'resized'):
            self.surface.perform({'type': 'click', 'x': 200, 'y': 150})
        self.ex.call.assert_not_called()

    def test_out_of_bounds_and_nonfinite_coordinates_rejected(self):
        for x in [-1, 800, float('nan'), True, '2']:
            with self.subTest(x=x), self.assertRaises(ValueError):
                self.surface.perform({'type': 'click', 'x': x, 'y': 1})
        self.ex._press_button.assert_not_called()

    def test_chord_targets_the_bound_browser(self):
        self.surface.perform({'type': 'keypress', 'keys': ['CTRL', 'L']})
        self.ex.call.assert_called_once_with('send_shortcut', {'mods': 'CTRL', 'key': 'l', 'window': 'address:0xab'})

    def test_system_and_developer_shortcuts_rejected(self):
        for keys in [['SUPER', 'Enter'], ['CTRL', 'SHIFT', 'I'], ['F12']]:
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                self.surface.perform({'type': 'keypress', 'keys': keys})
        self.ex.call.assert_not_called()

    def test_nonbrowser_refused(self):
        self.window['class'] = 'Alacritty'
        with self.assertRaisesRegex(RuntimeError, 'browser window'):
            self.surface.perform({'type': 'type', 'text': 'bad'})


    def test_text_helper_reads_only_bound_browser(self):
        result = self.surface.helper('read_browser_text', {})
        self.assertTrue(result.ok)
        self.ex.call.assert_called_once_with('read_page_text', {'target': 'address:0xab'})

    def test_text_helper_discards_result_after_focus_changed(self):
        def changed(*args):
            self.window['focusHistoryID'] = 1
            return Result(True, 'page text')
        self.ex.call.side_effect = changed
        with self.assertRaisesRegex(RuntimeError, 'lost focus'):
            self.surface.helper('read_browser_text', {})

    def test_url_helper_rejects_non_web_schemes(self):
        for url in ['file:///etc/passwd', 'javascript:alert(1)', 'https://example.com/\nfoo']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.surface.helper('open_browser_url', {'url': url})
        self.ex.call.assert_not_called()

    def test_url_helper_invalidates_old_coordinates(self):
        with mock.patch.object(self.surface, 'prepare') as prepare:
            self.surface.helper('open_browser_url', {'url': 'https://example.com/'})
        prepare.assert_called_once_with(url='https://example.com/')
        self.assertIsNone(self.surface.geometry)
        with self.assertRaisesRegex(ValueError, 'Observe the browser'):
            self.surface.perform({'type': 'click', 'x': 1, 'y': 2})


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def run_worker(self, responses, current=None):
        cfg = Config(live_browser_max_turns=3)
        self.worker = BrowserWorker(cfg, mock.Mock(), mock.Mock(), current or (lambda: True))
        self.worker.surface = mock.Mock()
        self.worker.surface.prepare.return_value = self.worker.surface.capture.return_value = 'data:image/png;base64,AAA'
        self.worker.surface.helper.return_value = Result(True, 'Verified headline')
        self.worker.surface.target = 'address:0xab'
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'test'}), \
             mock.patch('omarchy_voice.browser.request', side_effect=responses) as request:
            result = await self.worker.run('Open the article and read its headline')
        return result, request

    def response(self, output):
        return {'id': 'resp_x', 'status': 'completed', 'output': output,
                'usage': {'input_tokens': 10, 'output_tokens': 10}}

    def call(self, **kwargs):
        return {'type': 'computer_call', 'call_id': 'call_x', 'actions': [
            {'type': 'click', 'x': 1, 'y': 2}, {'type': 'keypress', 'keys': ['ENTER']}], **kwargs}

    def final(self):
        return self.response([{'type': 'message', 'content': [{'type': 'output_text', 'text': 'Verified headline'}]}])

    async def test_batched_actions_then_screenshot_then_verified_result(self):
        result, request = await self.run_worker([self.response([self.call()]), self.final()])
        self.assertTrue(result.ok, result.output)
        self.assertEqual(self.worker.surface.perform.call_count, 2)
        payload = request.call_args.args[0]
        self.assertEqual(payload['model'], MODEL)
        self.assertEqual([part['type'] for part in payload['input'][0]['content']], ['input_text'])
        output = payload['input'][-1]
        self.assertEqual(output['call_id'], 'call_x')
        self.assertEqual(output['output']['type'], 'computer_screenshot')
        self.assertFalse(payload['store'])

    async def test_correction_discards_late_model_actions(self):
        active = [True]
        def response(*args):
            active[0] = False
            return self.response([self.call()])
        result, _ = await self.run_worker(response, current=lambda: active[0])
        self.assertFalse(result.ok)
        self.worker.surface.perform.assert_not_called()

    async def test_safety_check_stops_before_actions(self):
        result, _ = await self.run_worker([self.response([self.call(pending_safety_checks=[{'code': 'x'}])])])
        self.assertFalse(result.ok)
        self.worker.surface.perform.assert_not_called()

    async def test_step_limit_is_bounded(self):
        result, request = await self.run_worker([self.response([self.call()])] * 3)
        self.assertFalse(result.ok)
        self.assertEqual(request.call_count, 3)
        self.assertIn('step limit', result.output)
        self.assertEqual(request.call_args.args[0]['tool_choice'], 'none')
        self.assertEqual(self.worker.surface.perform.call_count, 4)

    async def test_text_helper_result_reaches_astra_without_backend_round_trip(self):
        call = {'type': 'function_call', 'call_id': 'read_1', 'name': 'read_browser_text', 'arguments': '{}'}
        result, request = await self.run_worker([self.response([call]), self.final()])
        self.assertTrue(result.ok)
        self.worker.surface.helper.assert_called_once_with('read_browser_text', {})
        self.assertIn({'type': 'function_call_output', 'call_id': 'read_1', 'output': 'Verified headline'},
                      request.call_args.args[0]['input'])
        self.worker.surface.perform.assert_not_called()

    async def test_new_speech_blocks_browser_helper(self):
        current = iter([True, False])
        call = {'type': 'function_call', 'call_id': 'read_1', 'name': 'read_browser_text', 'arguments': '{}'}
        result, _ = await self.run_worker([self.response([call])], current=lambda: next(current))
        self.assertFalse(result.ok)
        self.worker.surface.helper.assert_not_called()

    async def test_incomplete_response_does_not_execute_actions(self):
        result, _ = await self.run_worker([{**self.response([self.call()]), 'status': 'incomplete'}])
        self.assertFalse(result.ok)
        self.worker.surface.perform.assert_not_called()

    async def test_interrupted_lookup_preserves_observed_text_for_backend(self):
        active = [True]
        call = {'type': 'function_call', 'call_id': 'read_1', 'name': 'read_browser_text', 'arguments': '{}'}
        count = 0
        def response(*args):
            nonlocal count
            count += 1
            if count == 1:
                return self.response([call])
            active[0] = False
            return self.response([self.call()])
        result, _ = await self.run_worker(response, current=lambda: active[0])
        self.assertFalse(result.ok)
        self.assertIn('Verified headline', result.output)
        self.assertIn('may now be stale', result.output)
        self.worker.surface.perform.assert_not_called()

    async def test_cost_includes_cache_writes_and_reasoning_output(self):
        self.assertAlmostEqual(cost({'input_tokens': 1000, 'output_tokens': 100,
                                     'input_tokens_details': {'cached_tokens': 100, 'cache_write_tokens': 200}}), .0146)
