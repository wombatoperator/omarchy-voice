"""Bounded, owner-only diagnostic events. Excludes raw audio and transport credentials."""
import json
import os
import threading
import time
from .security import redact_text


class Trace:
    def __init__(self, path, max_bytes=8 * 1024 * 1024, backups=3):
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self.lock = threading.Lock()
        self.sequence = 0

    @classmethod
    def clean(cls, value):
        if isinstance(value, dict):
            return {k: ("[redacted]" if any(word in k.lower() for word in
                    ("authorization", "api_key", "access_token", "secret", "password", "audio", "encrypted_content"))
                    else cls.clean(v)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls.clean(v) for v in value[:200]]
        if isinstance(value, str):
            value = redact_text(value)
            if len(value) > 32000:
                return value[:32000] + f"\n[truncated; original characters={len(value)}]"
        return value

    def write(self, event, **data):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.sequence += 1
            record = {"at": time.time(), "monotonic": time.monotonic(),
                      "sequence": self.sequence, "event": event, **self.clean(data)}
            encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            if self.path.exists() and self.path.stat().st_size + len(encoded.encode()) > self.max_bytes:
                for number in range(self.backups, 0, -1):
                    source = self.path if number == 1 else self.path.with_name(self.path.name + f".{number-1}")
                    if source.exists():
                        source.chmod(0o600)
                        source.replace(self.path.with_name(self.path.name + f".{number}"))
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded)
