"""The mouth: how the assistant tells you what it heard and did.

Three channels, all optional and all cheap:
  * a notification (Omarchy's shell renders these)
  * a state file, so a bar widget can show a live listening indicator
  * text to speech, if piper or espeak-ng is around
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import threading
import time

from .config import Config, LEVEL_FILE, LOG_FILE, STATE_DIR, STATE_FILE, RUNTIME_DIR

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

    def level(self, value: float) -> None:
        """Publish a 0..1 microphone level for the orb overlay.

        Capped at 20 Hz. Frames arrive at 10 Hz today, but the cap means a
        smaller frame size later cannot turn this into a write storm.
        """
        now = time.monotonic()
        if now - self._level_at < 0.05:
            return
        self._level_at = now
        try:
            tmp = LEVEL_FILE.with_suffix(".tmp")
            tmp.write_text(f"{max(0.0, min(1.0, value)):.3f}")
            tmp.replace(LEVEL_FILE)
        except OSError:
            pass

    # -- user-visible -------------------------------------------------------
    def notify(self, title: str, body: str = "", urgency: str = "low") -> None:
        if not self.config.notify or not shutil.which("notify-send"):
            return
        subprocess.run(
            ["notify-send", "-a", "OMA", "-u", urgency, "--", title, body],
            capture_output=True,
        )

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
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as fh:
            fh.write(f"{stamp}  {line}\n")
        if self.config.verbose:
            print(f"  {line}", flush=True)
