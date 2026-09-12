"""A close acknowledgement must not race a dependent window launch."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omarchy_voice.config import Config
from omarchy_voice.tools import Executor, Result


class CloseTests(unittest.TestCase):
    def setUp(self):
        self.executor = Executor(Config())
        self.lua = 'hl.dsp.window.close({ window = "address:0xabc" })'

    def test_waits_for_surface_removal_after_dispatch_acknowledgement(self):
        pending = Result(True, json.dumps([{"address": "0xabc"}]))
        with mock.patch.object(self.executor, "_dispatch_lua", return_value=Result(True, "ok")), \
             mock.patch.object(self.executor, "_tool_hypr_query", side_effect=[pending, pending, Result(True, "[]")]) as query, \
             mock.patch("omarchy_voice.tools.time.sleep"):
            result = self.executor._tool_hypr_dispatch(self.lua)
        self.assertTrue(result.ok)
        self.assertEqual(query.call_count, 3)

    def test_unreadable_inventory_never_confirms_close(self):
        for result in [Result(False, "offline"), Result(True, "invalid"), Result(True, "{}")]:
            with self.subTest(result=result), \
                 mock.patch.object(self.executor, "_dispatch_lua", return_value=Result(True, "ok")), \
                 mock.patch.object(self.executor, "_tool_hypr_query", return_value=result):
                self.assertFalse(self.executor._tool_hypr_dispatch(self.lua).ok)

    def test_blocked_close_does_not_claim_success_or_wait_forever(self):
        with mock.patch.object(self.executor, "_dispatch_lua", return_value=Result(True, "ok")), \
             mock.patch.object(self.executor, "_tool_hypr_query", return_value=Result(True, '[{"address":"0xabc"}]')), \
             mock.patch("omarchy_voice.tools.time.monotonic", side_effect=[0, 2]):
            result = self.executor._tool_hypr_dispatch(self.lua)
        self.assertFalse(result.ok)
        self.assertIn("still present", result.output)

    def test_unaddressed_operations_keep_their_existing_semantics(self):
        with mock.patch.object(self.executor, "_dispatch_lua", return_value=Result(True, "ok")), \
             mock.patch.object(self.executor, "_tool_hypr_query") as query:
            self.assertTrue(self.executor._tool_hypr_dispatch('hl.dsp.window.close({})').ok)
        query.assert_not_called()
