#!/usr/bin/env python3
"""Lightweight publication guard. Reports locations/rules, never matched secrets."""
import argparse
from pathlib import PurePosixPath
import re
import subprocess

RULES = {
    'private key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'OpenAI token': r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{30,}',
    'GitHub token': r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})',
    'AWS access key': r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    'credential URL': r'https?://[^\s/]+:[^\s/@]+@',
    'personal home path': r'/home/(?!you(?:/|\b)|user(?:/|\b)|test(?:/|\b)|example(?:/|\b))[-A-Za-z0-9_]+/',
    'production session identifier': r'\blive_u7_[A-Za-z0-9]+',
}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--staged',action='store_true');args=parser.parse_args()
    files=subprocess.check_output(['git','ls-files','-z']).decode().split('\0')
    failures=[]
    for name in files:
        if not name:continue
        path=PurePosixPath(name)
        if any(part in ('benchmarks','private','secrets','.venv') for part in path.parts) or path.suffix in ('.jsonl','.sqlite3','.log','.pem','.key','.p12','.pfx') or path.name.startswith('.env'):
            failures.append((name,0,'private/runtime file'))
        if args.staged:
            result=subprocess.run(['git','show',':'+name],capture_output=True)
            if result.returncode:continue
            content=result.stdout.decode(errors='replace')
        else:
            from pathlib import Path
            if not Path(name).is_file():continue
            content=Path(name).read_text(errors='replace')
        for rule,pattern in RULES.items():
            for match in re.finditer(pattern,content):
                failures.append((name,content.count('\n',0,match.start())+1,rule))
    for name,line,rule in failures:print(f'{name}:{line}: {rule}')
    print(f'Publication scan: {len(failures)} findings across {len(files)-1} indexed paths.')
    return bool(failures)

if __name__=='__main__':raise SystemExit(main())
