#!/usr/bin/env python3
"""Free integration check: real systemd workers, a fake coding CLI, real sandboxed verification.

Does not invoke a model or the installed Codex CLI. Tests the adapter contract,
process lifecycle and persistence, not a coding model's quality or authentication.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
from unittest import mock

package_root = (Path.home() / ".local/share/omarchy-voice/src" if "--installed" in sys.argv
                else Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, str(package_root))
from omarchy_voice.config import Config
from omarchy_voice.tasks import TaskManager


FIXTURE = '''#!/usr/bin/python3
import json,sys,time
from pathlib import Path
prompt = sys.stdin.read()
Path("started.txt").write_text("fixture running")
if "SLOW_FIXTURE" in prompt:
    time.sleep(30)
else:
    time.sleep(2)
Path("result.json").write_text(json.dumps({"sum": sum(range(101))}))
Path(sys.argv[sys.argv.index("--output-last-message")+1]).write_text(json.dumps({
    "outcome": "completed", "summary": "Computed and verified the numeric fixture",
    "artifacts": ["result.json"], "checks": [{"criterion": 0,
    "argv": ["python3", "-c", "import json; assert json.load(open('result.json'))['sum']==5050"]}]}))
print(json.dumps({"type":"fixture.completed"}))
'''


def wait_for(manager, identity, predicate, timeout=20):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        task = manager.status(identity)
        if predicate(task):
            return task
        time.sleep(.1)
    raise RuntimeError("Fixture timed out: " + json.dumps(task))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/task-worker.json"))
    parser.add_argument("--installed", action="store_true", help="test the installed package")
    parser.add_argument("--state-parent", type=Path, help="temporary state parent visible outside PrivateTmp")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="oma-task-qa-", dir=args.state_parent) as temporary:
        root = Path(temporary)
        fixture = root / "fixture-agent"
        fixture.write_text(FIXTURE)
        fixture.chmod(0o700)
        config = Config(tasks_root=str(root / "state"), tasks_provider="codex", tasks_timeout_seconds=60)
        manager = TaskManager(config)
        which = shutil.which
        tasks = []
        started = time.monotonic()
        try:
            with mock.patch("omarchy_voice.tasks.shutil.which", side_effect=lambda name: str(fixture) if name == "codex" else which(name)):
                first = manager.submit("Compute the sum of integers 0 through 100", ["Sum equals 5050"], "numeric")
                tasks.append(first["id"])
                # Reconstruct all client-side state while the independent worker is alive.
                manager = TaskManager(config)
                result = wait_for(manager, first["id"], lambda x: x["status"] in {"completed", "blocked", "failed"})
                assert result["status"] == "completed", result
                assert result["result"]["checks"][0]["passed"], result
                assert json.loads(manager.read(first["id"], "result.json")["text"])["sum"] == 5050
                duplicate = manager.submit("Compute the sum of integers 0 through 100", ["Sum equals 5050"], "numeric")
                assert duplicate["id"] == first["id"]
                slow = manager.submit("SLOW_FIXTURE cancellation test", ["Complete fixture"], "slow")
                tasks.append(slow["id"])
                wait_for(manager, slow["id"], lambda x: (Path(x["workspace"]) / "started.txt").exists())
                cancelled = manager.cancel(slow["id"])
                assert cancelled["status"] == "cancelled", cancelled
                assert not (Path(cancelled["workspace"]) / "result.json").exists()
                # Change the fixture, not the user's task, to make a deterministic resume.
                fixture.write_text(FIXTURE.replace('if "SLOW_FIXTURE" in prompt:', 'if False:'))
                resumed = manager.resume(slow["id"], "Inspect preserved started.txt before continuing")
                assert resumed["id"] == slow["id"] and resumed["attempt"] == 2
                final = wait_for(manager, slow["id"], lambda x: x["status"] in {"completed", "blocked", "failed"})
                assert final["status"] == "completed", final
                receipts = manager.store.jobs(slow["id"])
                assert {x["attempt"] for x in receipts} == {1, 2}
                metrics = {"passed": True, "seconds": round(time.monotonic() - started, 3),
                           "installed": args.installed,
                           "model_calls": 0, "real_coding_agent_calls": 0,
                           "checks": ["independent systemd worker", "manager reconstruction during execution",
                                      "artifact-backed final verification", "idempotent submission", "process-group cancellation",
                                      "resume preserves task identity and earlier job receipts"],
                           "limits": "Fake CLI adapter; does not verify provider authentication or model task quality"}
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(metrics, indent=2) + "\n")
                print(json.dumps(metrics, indent=2))
        finally:
            for identity in tasks:
                task = manager.store.get(identity)
                # Also stop terminal units that may still be completing teardown.
                manager.supervisor.stop(task["unit"])


if __name__ == "__main__":
    main()
