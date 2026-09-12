"""Durability, isolation, provider contracts and general work: no paid API calls."""
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omarchy_voice.config import Config, load
from omarchy_voice.tasks import TaskManager, TaskStore, SystemdSupervisor
from omarchy_voice.task_worker import Worker, sandbox_command
from omarchy_voice.tools import Executor, tools_for


class Supervisor:
    def __init__(self):
        self.started = []
        self.states = {}

    def start(self, root, task):
        self.started.append(task["id"])
        self.states[task["unit"]] = "active"

    def state(self, unit):
        return self.states.get(unit, "inactive")

    def stop(self, unit):
        self.states[unit] = "inactive"


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(tasks_root=self.temp.name, tasks_max_active=4)
        self.supervisor = Supervisor()
        self.manager = TaskManager(self.config, self.supervisor)
        self.providers = mock.patch.object(TaskManager, "providers", return_value={
            "responses": {"available": True}, "codex": {"available": True}})
        self.providers.start()
        self.addCleanup(self.providers.stop)

    def submit(self, key="one", **kwargs):
        return self.manager.submit("Produce a reproducible result", ["Result is correct"], key, **kwargs)

    def worker(self, **kwargs):
        task = self.submit(**kwargs)
        return Worker(self.manager.store, task["id"])

    def test_model_timeout_preserves_artifacts_and_logs_phase(self):
        worker = self.worker()
        worker.write('result.txt', 'already computed')
        worker.request = mock.Mock(side_effect=TimeoutError('read timed out'))
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'test'}), \
                mock.patch('omarchy_voice.task_worker.cfg.load_env_file'):
            with self.assertRaisesRegex(RuntimeError, 'Existing jobs and artifacts were preserved'):
                worker.responses()
        self.assertEqual((worker.workspace/'result.txt').read_text(), 'already computed')
        view = self.manager.status(worker.task_id)
        self.assertEqual(view['phase'], 'model')
        events = [json.loads(line) for line in (worker.store.root/worker.task_id/'worker-trace.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['event'], 'model_request_error')
        self.assertEqual(events[-1]['error_type'], 'TimeoutError')

    def test_duplicate_submissions_across_managers_start_once(self):
        first = self.submit()
        second = TaskManager(self.config, self.supervisor).submit(
            "Produce a reproducible result", ["Result is correct"], "one")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.supervisor.started), 1)
        with self.assertRaisesRegex(ValueError, "different specification"):
            self.manager.submit("Other work", ["Done"], "one")

    def test_concurrent_submissions_share_receipt(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: self.submit(), range(2)))
        self.assertEqual(values[0]["id"], values[1]["id"])
        self.assertEqual(len(self.supervisor.started), 1)

    def test_capacity_and_no_replay_after_worker_exit(self):
        self.config.tasks_max_active = 1
        task = self.submit()
        with self.assertRaisesRegex(ValueError, "capacity"):
            self.submit("two")
        self.supervisor.stop(self.manager.store.get(task["id"])["unit"])
        with mock.patch("omarchy_voice.tasks.time.time", return_value=time.time() + 10):
            result = self.manager.status(task["id"])
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(len(self.supervisor.started), 1)
        self.assertEqual(len(self.manager.store.notices()), 1)

    def test_cancel_and_resume_keep_identity_and_artifacts(self):
        worker = self.worker()
        worker.write("evidence.txt", "preserved")
        self.manager.cancel(worker.task_id)
        resumed = self.manager.resume(worker.task_id, "inspect previous outputs")
        self.assertEqual(resumed["attempt"], 2)
        self.assertEqual(self.manager.read(worker.task_id, "evidence.txt")["text"], "preserved")
        self.assertEqual(self.manager.store.get(worker.task_id)["guidance"], ["inspect previous outputs"])

    def test_cancel_wins_race_with_failed_child_exit(self):
        task = self.submit()
        self.manager.store.update(task["id"], cancel_requested=True)
        result = self.manager.store.finish(task["id"], "blocked", "child exited on SIGTERM")
        self.assertEqual(result["status"], "cancelled")

    def test_resume_does_not_overlap_live_worker(self):
        task = self.submit()
        self.manager.store.finish(task["id"], "blocked", "test")
        with self.assertRaisesRegex(ValueError, "still active"):
            self.manager.resume(task["id"])

    def test_ambiguous_start_failure_does_not_resubmit(self):
        with mock.patch.object(self.supervisor, "start", side_effect=TimeoutError("unknown launch")):
            with self.assertRaisesRegex(RuntimeError, "recorded"):
                self.submit()
        self.submit()
        self.assertEqual(self.supervisor.started, [])
        self.assertEqual(len(self.manager.store.all()), 1)

    def test_paths_block_traversal_and_symlinks(self):
        worker = self.worker()
        with self.assertRaises(ValueError):
            worker.write("../task.sqlite3", "bad")
        with self.assertRaises(ValueError):
            worker.write("/etc/passwd", "bad")
        (worker.workspace / "outside").symlink_to("/etc")
        with self.assertRaises(ValueError):
            worker.write("outside/file", "bad")
        with self.assertRaises(ValueError):
            self.manager.read(worker.task_id, "outside/passwd")

    def test_multiline_files_and_bounded_read(self):
        worker = self.worker()
        content = "print('hello')\n" * 2000
        worker.write("src/main.py", content)
        first = self.manager.read(worker.task_id, "src/main.py")
        second = self.manager.read(worker.task_id, "src/main.py", first["next_offset"])
        self.assertEqual(len(first["text"]), 12000)
        self.assertEqual(first["text"] + second["text"], content[:24000])

    def test_call_receipts_prevent_repeat_and_argument_substitution(self):
        worker = self.worker()
        call = {"call_id": "c1", "name": "write_file", "arguments": json.dumps({"path": "one", "content": "one"})}
        original = worker.execute_call(call)
        with mock.patch.object(worker, "tool", side_effect=AssertionError("duplicate")):
            self.assertEqual(worker.execute_call(call), original)
        call["arguments"] = json.dumps({"path": "one", "content": "two"})
        with self.assertRaisesRegex(ValueError, "reused"):
            worker.execute_call(call)

    def test_completion_needs_every_criterion_and_artifact(self):
        worker = self.worker()
        with self.assertRaisesRegex(ValueError, "requires artifacts"):
            worker.finish("completed", "done", [], [])
        worker.write("report.txt", "report")
        with self.assertRaisesRegex(ValueError, "every criterion"):
            worker.finish("completed", "done", ["report.txt"], [])

    def test_incomplete_response_executes_no_partial_batch(self):
        worker = self.worker()
        good_call = {"type": "function_call", "status": "completed", "call_id": "one", "name": "write_file",
                     "arguments": json.dumps({"path": "must-not-exist", "content": "bad"})}
        bad_call = {"type": "function_call", "status": "incomplete", "call_id": "two", "name": "run", "arguments": '{"argv":'}
        worker.request = mock.Mock(return_value={"status": "completed", "output": [good_call, bad_call]})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-not-a-key"}), \
                mock.patch("omarchy_voice.task_worker.cfg.load_env_file"):
            with self.assertRaisesRegex(RuntimeError, "incomplete/invalid"):
                worker.responses()
        self.assertFalse((worker.workspace / "must-not-exist").exists())
        self.assertEqual(worker.request.call_count, 2)

    def test_late_response_after_cancel_does_not_write(self):
        worker = self.worker()
        def response(*_):
            worker.cancelled = True
            return {"status": "completed", "output": []}
        worker.request = response
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-not-a-key"}), \
                mock.patch("omarchy_voice.task_worker.cfg.load_env_file"):
            from omarchy_voice.task_worker import Cancelled
            with self.assertRaises(Cancelled):
                worker.responses()

    def test_schema_config_and_dry_run_do_not_start_workers(self):
        self.assertIn("task_submit", {x["name"] for x in tools_for(self.config)})
        self.config.tasks_enabled = False
        self.assertNotIn("task_submit", {x["name"] for x in tools_for(self.config)})
        self.config.tasks_enabled = True
        self.config.dry_run = True
        executor = Executor(self.config)
        with mock.patch.object(executor, "task_manager", side_effect=AssertionError("must not start")):
            result = executor.call("task_submit", {"goal": "Calculate a result", "criteria": ["correct"], "request_key": "dry"})
        self.assertTrue(result.ok)
        self.assertIn("dry-run", result.output)
        config_path = Path(self.temp.name) / "config.toml"
        config_path.write_text('[tasks]\nprovider="codex"\nmax_model_calls=30\n')
        config = load(config_path)
        self.assertEqual(config.tasks_provider, "codex")
        self.assertEqual(config.tasks_max_model_calls, 30)
        self.assertFalse(config.unknown_keys)

    def test_notices_are_durable_and_acknowledged(self):
        task = self.submit()
        self.manager.store.finish(task["id"], "failed", "A check failed")
        store = TaskStore(self.temp.name)
        notices = store.notices()
        self.assertEqual(len(notices), 1)
        store.acknowledge(notices[0]["id"])
        self.assertEqual(self.manager.store.notices(), [])

    def test_supervisor_separate_unit_and_no_keys_in_command(self):
        task = self.submit()
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as run:
            SystemdSupervisor().start(self.manager.store.root, self.manager.store.get(task["id"]))
        command = run.call_args.args[0]
        self.assertIn("--property=KillMode=control-group", command)
        self.assertFalse(any("OPENAI_API_KEY=" in x for x in command))
        self.assertNotIn("--scope", command)


@unittest.skipUnless(shutil.which("bwrap"), "bubblewrap not installed")
class SandboxTests(unittest.TestCase):
    setUp = TaskTests.setUp
    submit = TaskTests.submit
    worker = TaskTests.worker
    def test_real_process_output_exit_and_isolation(self):
        worker = self.worker()
        secret = Path(self.temp.name) / "private.txt"
        secret.write_text("not for the task")
        code = "import os,pathlib,sys; print(pathlib.Path(" + repr(str(secret)) + ").exists()); print(os.environ.get('OMA_TEST_SECRET')); print('failure',file=sys.stderr); sys.exit(7)"
        with mock.patch.dict(os.environ, {"OMA_TEST_SECRET": "secret"}):
            job = worker.run_command(["python3", "-c", code])
        self.assertEqual(job["exit_code"], 7, job)
        self.assertEqual(job["stdout_tail"], "False\nNone\n")
        self.assertEqual(job["stderr_tail"], "failure\n")
        self.assertEqual(TaskStore(self.temp.name).jobs(worker.task_id)[0]["exit_code"], 7)

    def test_timeout_stops_process(self):
        worker = self.worker()
        job = worker.run_command(["python3", "-c", "import time; time.sleep(10)"], 1)
        self.assertEqual(job["status"], "failed")
        self.assertIn("time limit", job["error"])
        self.assertLess(job["seconds"], 4)

    def test_external_absolute_workspace_paths_are_usable_in_verifier(self):
        worker = self.worker()
        worker.write("entry", "#!/usr/bin/python3\nprint('ok')\n")
        (worker.workspace / "entry").chmod(0o700)
        job = worker.run_command([str(worker.workspace / "entry")])
        self.assertEqual(job["exit_code"], 0, job)
        self.assertEqual(job["stdout_tail"], "ok\n")

    def test_real_general_workloads_and_final_validation(self):
        for key, code, check in [
            ("numeric", "import json; json.dump({'sum':sum(range(101))},open('result.json','w'))", "import json; assert json.load(open('result.json'))['sum']==5050"),
            ("text", "open('result.txt','w').write('alpha beta beta'.replace('beta','gamma'))", "assert open('result.txt').read()=='alpha gamma gamma'"),
            ("simulation", "import random,json; r=random.Random(42); json.dump([r.randint(1,6) for _ in range(50)],open('result.json','w'))", "import json; x=json.load(open('result.json')); assert len(x)==50 and all(1<=i<=6 for i in x)")]:
            with self.subTest(key=key):
                worker = self.worker(key=key)
                artifact = "result.txt" if key == "text" else "result.json"
                worker.write("experiment.py", code + "\n")
                self.assertEqual(worker.run_command(["python3", "experiment.py"])["exit_code"], 0)
                worker.finish("completed", "Verified", [artifact, "experiment.py"], [{"criterion": 0, "argv": ["python3", "-c", check]}])
                self.assertEqual(self.manager.status(worker.task_id)["status"], "completed")

    def test_failed_assertion_cannot_be_reported_completed(self):
        worker = self.worker()
        worker.write("report.txt", "claim")
        worker.finish("completed", "Everything passed", ["report.txt"], [{"criterion": 0, "argv": ["python3", "-c", "assert False"]}])
        task = self.manager.status(worker.task_id)
        self.assertEqual(task["status"], "failed")
        self.assertFalse(task["result"]["checks"][0]["passed"])

    def test_direct_provider_full_loop_uses_files_run_and_finish(self):
        worker = self.worker()
        calls = [
            ("write_file", {"path": "report.txt", "content": "verified\n"}),
            ("run", {"argv": ["python3", "-c", "assert open('report.txt').read()=='verified\\n'"]}),
            ("finish", {"outcome": "completed", "summary": "Verified fixture", "artifacts": ["report.txt"],
                        "checks": [{"criterion": 0, "argv": ["python3", "-c", "assert open('report.txt').read()=='verified\\n'"]}]})]
        responses = [{"id": f"r{i}", "status": "completed", "usage": {"input_tokens": 1, "output_tokens": 2},
                      "output": [{"type": "function_call", "status": "completed", "call_id": str(i),
                                  "name": name, "arguments": json.dumps(args)}]} for i, (name, args) in enumerate(calls)]
        worker.request = mock.Mock(side_effect=responses)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-not-a-key"}), \
                mock.patch("omarchy_voice.task_worker.cfg.load_env_file"):
            worker.run()
        task = self.manager.status(worker.task_id)
        self.assertEqual(task["status"], "completed", task)
        self.assertEqual(task["usage"]["responses"], 3)
        self.assertEqual(len(task["jobs"]), 3)  # preflight, command, final validator


if __name__ == "__main__":
    unittest.main()
