"""Offline replay tests with synthetic requests; no desktop or API access."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import eval_navigation as replay


CASE = {"state": {"request": "Show workspace two"},
        "candidates": {"two": "Switch to workspace 2", "fallback": "Use the existing planner"},
        "expected": "two"}
RESPONSE = {"answers": {
    "selection": {"type": "choice", "choice": "two", "confidence": .98,
                  "probabilities": {"two": .99, "fallback": .01}},
    "supported": {"type": "noul", "noul": .99}}}


class ReplayTests(unittest.TestCase):
    def test_ground_truth_never_enters_request_and_questions_are_batched(self):
        body = replay.payload(CASE, "jev-latest")
        self.assertNotIn("expected", body)
        self.assertEqual(body["state"], CASE["state"])
        self.assertEqual(set(body["questions"]), {"selection", "supported"})

    def test_low_certainty_or_compound_request_falls_back(self):
        self.assertEqual(replay.decide(RESPONSE, CASE["candidates"], .9)["choice"], "two")
        for question, field in (("selection", "confidence"), ("supported", "noul")):
            response = copy.deepcopy(RESPONSE)
            response["answers"][question][field] = .5
            self.assertEqual(replay.decide(response, CASE["candidates"], .9)["choice"], "fallback")

    def test_malformed_and_unlisted_choices_fail_closed(self):
        variants = [None, {}, {"answers": []}]
        for field, value in (("choice", "shell"), ("probabilities", {"two": 1}),
                             ("confidence", float("nan")), ("confidence", True),
                             ("probabilities", {"two": .4, "fallback": .6}),
                             ("probabilities", {"two": .9, "fallback": .9})):
            response = copy.deepcopy(RESPONSE)
            response["answers"]["selection"][field] = value
            variants.append(response)
        for value in variants:
            with self.subTest(value=value):
                result = replay.decide(value, CASE["candidates"], .9)
                self.assertFalse(result["valid"])
                self.assertEqual(result["choice"], "fallback")

    def test_missing_fallback_or_ground_truth_is_rejected(self):
        for case in ({**CASE, "candidates": {"two": "Workspace 2"}}, {**CASE, "expected": "unknown"}):
            with self.assertRaises(ValueError):
                replay.payload(case, "jev-latest")

    def test_default_cli_never_reads_credentials_or_calls_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text(json.dumps([CASE]))
            with mock.patch.object(replay, "Client") as client, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(replay.main([str(path), "--env-file", str(Path(tmp) / "missing")]), 0)
            client.assert_not_called()

    def test_private_output_permissions_and_no_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(Path, "cwd", return_value=Path(tmp)):
            out = Path(tmp) / "benchmarks/result.json"
            with replay.output_file(out) as stream:
                stream.write("{}")
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)
            self.assertEqual(out.parent.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(FileExistsError):
                replay.output_file(out)
            with self.assertRaises(ValueError):
                replay.output_file(Path(tmp) / "public.json")
            link = out.parent / "link.json"
            link.symlink_to(out)
            with self.assertRaises(ValueError):
                replay.output_file(link)
            linked_dir = out.parent / "linked"
            linked_dir.symlink_to(Path(tmp), target_is_directory=True)
            with self.assertRaises(ValueError):
                replay.output_file(linked_dir / "result.json")

    def test_provider_error_stops_and_never_prints_sensitive_body(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(Path, "cwd", return_value=Path(tmp)):
            cases = Path(tmp) / "cases.json"
            cases.write_text(json.dumps([CASE, CASE]))
            out = Path(tmp) / "benchmarks/result.json"
            stdout = io.StringIO()
            with mock.patch.dict(os.environ, {"JEV_API_KEY": "synthetic-key"}), \
                 mock.patch.object(replay.Client, "evaluate", side_effect=RuntimeError("sensitive-body")) as call, \
                 contextlib.redirect_stdout(stdout):
                code = replay.main([str(cases), "--connect", "--output", str(out)])
            self.assertEqual(code, 1)
            self.assertEqual(call.call_count, 1)
            self.assertNotIn("sensitive-body", stdout.getvalue() + out.read_text())
            self.assertNotIn(CASE["state"]["request"], out.read_text())
            self.assertEqual(json.loads(out.read_text())["summary"]["correct"], 0)

    def test_redirect_is_not_followed(self):
        response = mock.Mock(status=302, read=mock.Mock(return_value=b"private response"))
        with mock.patch.object(replay.http.client, "HTTPSConnection") as connection:
            connection.return_value.getresponse.return_value = response
            client = replay.Client("synthetic-key", 1)
            with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
                client.evaluate(replay.payload(CASE, "jev-latest"))
            connection.assert_called_once_with("api.typesafe.ai", timeout=1)
            connection.return_value.request.assert_called_once()
            connection.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
