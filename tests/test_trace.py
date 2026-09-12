import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from omarchy_voice.trace import Trace


class TraceTests(unittest.TestCase):
    def test_rotation_permissions_redaction_and_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'trace.jsonl'
            trace = Trace(path, max_bytes=600, backups=2)
            for number in range(5):
                trace.write('test', number=number, authorization='private', audio='raw', text='x'*400)
            self.assertEqual(len(list(Path(tmp).iterdir())), 3)
            for file in Path(tmp).iterdir():
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                record = json.loads(file.read_text())
                self.assertEqual(record['authorization'], '[redacted]')
                self.assertEqual(record['audio'], '[redacted]')
            self.assertEqual(json.loads(path.read_text())['number'], 4)
            self.assertIn('original characters=40000', Trace.clean('x'*40000))
