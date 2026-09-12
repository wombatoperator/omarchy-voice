"""The mouth: how the assistant tells you what it heard and did.

Three channels, all optional and all cheap:
  * a notification (Omarchy's shell renders these)
  * a state file, so a bar widget can show a live listening indicator
  * text to speech, if piper or espeak-ng is around
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import threading
import time

from .config import Config, LEVEL_FILE, LOG_FILE, STATE_DIR, STATE_FILE, RUNTIME_DIR
from .security import redact_text

ICONS = {
    "idle": "󰍬",
    "listening": "󰍬",
    "thinking": "󱚟",
    "acting": "󱐋",
    "confirm": "󰀦",
    "error": "󰍭",
}


class Feedback:
    def __init__(self, config: Config):
        self.config = config
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._level_at = 0.0

    # -- bar state ----------------------------------------------------------
    def state(self, status: str, text: str = "") -> None:
        """Write the current status where a bar widget can poll it."""
        payload = {
            "status": status,
            "icon": ICONS.get(status, ICONS["idle"]),
            "text": text,
            "class": status,
            "updated": time.time(),
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(STATE_FILE)

    def level(self, value: float, voice: float = 0.0) -> None:
        """Publish the two loudnesses the orb draws: yours, then hers.

        Written as two space-separated floats, "0.612 0.000". The order is not
        arbitrary — `parseFloat` stops at the space, so anything written against
        the old single-float format keeps reading the microphone level exactly
        as before and simply never learns about the second channel.

        Both are needed because the conversation is half duplex. The microphone
        gate pins `value` to zero for the whole of every reply, so an orb with
        only the first number goes dead still precisely while she is talking —
        the moment it should be most alive.

        Capped at 20 Hz. Frames arrive at 10 Hz today, but the cap means a
        smaller frame size later cannot turn this into a write storm.
        """
        now = time.monotonic()
        if now - self._level_at < 0.05:
            return
        self._level_at = now
        try:
            tmp = LEVEL_FILE.with_suffix(".tmp")
            tmp.write_text(f"{max(0.0, min(1.0, value)):.3f} "
                           f"{max(0.0, min(1.0, voice)):.3f}")
            tmp.replace(LEVEL_FILE)
        except OSError:
            pass

    # -- user-visible -------------------------------------------------------
    def notify(self, title: str, body: str = "", urgency: str = "low") -> bool:
        if not self.config.notify or not shutil.which("notify-send"):
            return False
        result = subprocess.run(
            ["notify-send", "-a", "OMA", "-u", urgency, "--", title, body],
            capture_output=True,
        )
        return result.returncode == 0

    def speak(self, text: str) -> None:
        if not self.config.speak or not text:
            return
        threading.Thread(target=self._speak_now, args=(text,), daemon=True).start()

    def _speak_now(self, text: str) -> None:
        if self.config.tts_command:
            cmd = shlex.split(self.config.tts_command)
            subprocess.run([*cmd, "--", text], capture_output=True)
            return
        if shutil.which("piper") and shutil.which("aplay"):
            piper = subprocess.Popen(
                ["piper", "--output-raw"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
            assert piper.stdin is not None
            aplay = subprocess.Popen(
                ["aplay", "-r", "22050", "-f", "S16_LE", "-t", "raw", "-"],
                stdin=piper.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                piper.stdin.write(text.encode())
                piper.stdin.close()
            except BrokenPipeError:
                pass
            aplay.wait()
            piper.wait()
            return
        if shutil.which("espeak-ng"):
            subprocess.run(["espeak-ng", "-s", "165", "--", text], capture_output=True)

    # -- log ----------------------------------------------------------------
    def log(self, line: str) -> None:
        line = redact_text(line)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(f"{stamp}  {line}\n")
        if self.config.verbose:
            print(f"  {line}", flush=True)
