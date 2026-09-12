"""Read fresh browser text while preserving prior plain-text selection/clipboard data."""
from __future__ import annotations

import subprocess
import tempfile
import time
import uuid
from functools import partial


class SelectionError(Exception):
    pass


def paste_text(text, paste, focused):
    """Paste exact UTF-8 while retaining a plain-text clipboard.

    The caller must verify the visible input afterward. Never replay a paste
    automatically: focus loss or a timeout can leave its outcome uncertain.
    """
    formats = (_read('--list-types', primary=False) or b'').decode(errors='replace').splitlines()
    plain = {'text/plain', 'text/plain;charset=utf-8', 'UTF8_STRING', 'TEXT', 'STRING'}
    if any(kind not in plain for kind in formats):
        raise SelectionError('clipboard has rich/binary data; refused to replace it for browser typing')
    original = _read('--no-newline', '--type', 'text/plain', primary=False) if formats else None
    if original is not None and len(original) > 1_000_000:
        raise SelectionError('clipboard is too large to preserve')
    owned = text.encode('utf-8')
    if not focused():
        raise SelectionError('browser lost focus before paste')
    try:
        _write(owned, primary=False)
        if not focused():
            raise SelectionError('browser lost focus before paste')
        paste()
        # Let the receiving application consume the selection before restoring.
        time.sleep(.15)
        if not focused():
            raise SelectionError('browser lost focus after paste; inspect input before retrying')
    finally:
        if _read('--no-newline', '--type', 'text/plain', primary=False) == owned:
            _write(original, primary=False)


def _read(*args, primary=True):
    try:
        got = subprocess.run(["wl-paste", *(["--primary"] if primary else []), *args], capture_output=True, timeout=2)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SelectionError("primary selection is unavailable") from exc
    if got.returncode:
        if b"Nothing is copied" in got.stderr:
            return None
        raise SelectionError("could not read primary selection")
    return got.stdout


def _write(data, primary=True):
    args = ["wl-copy", *(["--primary"] if primary else [])]
    args += ["--clear"] if data is None else ["--type", "text/plain;charset=utf-8"]
    # wl-copy forks a selection owner; it must not inherit captured pipe ends.
    with tempfile.TemporaryFile() as errors:
        try:
            done = subprocess.run(args, input=data, stdout=subprocess.DEVNULL,
                                  stderr=errors, timeout=2)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SelectionError("could not restore/write primary selection") from exc
    if done.returncode:
        raise SelectionError("could not restore/write primary selection")


def read_selection(select, focused, timeout=0.7, *, primary=True):
    """Select in one verified browser, await fresh primary text, restore selection.

    By default the regular clipboard is never touched. Preserve plain-text content;
    refuse rich/binary selections rather than discarding their formats.
    A changed focus leaves the user's new selection alone and returns no text.
    """
    read = _read if primary else partial(_read, primary=False)
    write = _write if primary else partial(_write, primary=False)
    formats = (read("--list-types") or b"").decode(errors="replace").splitlines()
    plain = {"text/plain", "text/plain;charset=utf-8", "UTF8_STRING", "TEXT", "STRING"}
    if any(kind not in plain for kind in formats):
        raise SelectionError("primary selection has rich data; using screen reading instead")
    original = read("--no-newline", "--type", "text/plain") if formats else None
    if original is not None and len(original) > 1_000_000:
        raise SelectionError("primary selection is too large to preserve")
    marker = ("oma-selection-" + uuid.uuid4().hex).encode()
    owned = marker
    try:
        write(marker)
        if not focused():
            raise SelectionError("browser lost focus before selection")
        select()
        deadline = time.monotonic() + timeout
        while True:
            if not focused():
                raise SelectionError("browser lost focus while reading")
            got = read("--no-newline", "--type", "text/plain")
            if got and got != marker:
                owned = got
                return got.decode("utf-8", errors="replace")[:16000]
            if time.monotonic() >= deadline:
                raise SelectionError("browser did not provide fresh selection text")
            time.sleep(0.04)
    finally:
        # Restore only our temporary selection; do not overwrite a newer copy.
        current = read("--no-newline", "--type", "text/plain")
        if current == marker or (focused() and current == owned):
            write(original)
