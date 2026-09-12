"""The paid benchmark's ledger is tested without opening connections."""
import importlib.util,json,tempfile,unittest
from pathlib import Path
from unittest import mock
spec=importlib.util.spec_from_file_location('bench_desktop',Path(__file__).resolve().parents[1]/'tools/bench_desktop.py')
bench=importlib.util.module_from_spec(spec);spec.loader.exec_module(bench)

class BudgetTests(unittest.TestCase):
 def test_cache_writes_are_not_counted_as_plain_input(self):
  usage={'input_tokens':1000,'input_tokens_details':{'cached_tokens':400,'cache_write_tokens':500},'output_tokens':100}
  self.assertAlmostEqual(bench.price(usage,'gpt-5.6-terra'),.00273)
 def test_reasoning_tokens_are_already_in_output_total(self):
  usage={'input_tokens':0,'output_tokens':100,'output_tokens_details':{'reasoning_tokens':80}}
  self.assertAlmostEqual(bench.price(usage,'gpt-5.6-terra'),.0012)
 def test_reserve_survives_interrupted_run(self):
  with tempfile.TemporaryDirectory() as root,mock.patch.object(bench,'LEDGER',Path(root)/'budget.json'):
   budget=bench.Budget();budget.begin({'model':'gpt-5.6-terra'})
   self.assertAlmostEqual(budget.used(),.65)
   budget.close()
   with self.assertRaisesRegex(RuntimeError,'Unfinalized'):bench.Budget()
 def test_insufficient_reserve_prevents_new_paid_run(self):
  with tempfile.TemporaryDirectory() as root,mock.patch.object(bench,'LEDGER',Path(root)/'budget.json'):
   budget=bench.Budget();budget.data['runs']=[{'status':'complete','charged_estimate':2.3}]
   with self.assertRaisesRegex(RuntimeError,'Budget reserve'):budget.begin({'model':'gpt-5.6-terra'})
 def test_final_cost_replaces_reservation(self):
  with tempfile.TemporaryDirectory() as root,mock.patch.object(bench,'LEDGER',Path(root)/'budget.json'):
   budget=bench.Budget();entry=budget.begin({'model':'gpt-5.6-terra'})
   budget.finish(entry,{'charged_estimate':.03})
   budget.close()
   self.assertAlmostEqual(bench.Budget().used(),.03)
   self.assertEqual(bench.LEDGER.stat().st_mode & 0o777,0o600)

 def test_concurrent_benchmark_cannot_double_spend_reservation(self):
  with tempfile.TemporaryDirectory() as root,mock.patch.object(bench,'LEDGER',Path(root)/'budget.json'):
   budget=bench.Budget()
   with self.assertRaisesRegex(RuntimeError,'Another benchmark'):bench.Budget()
   budget.close()
   self.assertEqual(bench.Budget().used(),0)
 def test_premium_tier_reserves_double_backend_allowance(self):
  with tempfile.TemporaryDirectory() as root,mock.patch.object(bench,'LEDGER',Path(root)/'budget.json'):
   budget=bench.Budget();budget.begin({'model':'gpt-5.6-terra','tier':'priority'})
   self.assertAlmostEqual(budget.used(),1.3)

 def test_unknown_model_cannot_start_a_paid_run(self):
  with tempfile.TemporaryDirectory() as root,mock.patch.object(bench,'LEDGER',Path(root)/'budget.json'):
   budget=bench.Budget()
   with self.assertRaisesRegex(RuntimeError,'No verified pricing'):budget.begin({'model':'unknown'})
   self.assertEqual(budget.used(),0)
