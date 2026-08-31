"""Configuration loading.

Config lives at ~/.config/omarchy-voice/config.toml. Every key has a working
default, so the file is optional.
"""

from __future__ import annotations

import os
import stat
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
CACHE_HOME = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
STATE_HOME = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
ENV_FILE = CONFIG_HOME / "omarchy-voice" / "env"
SAFETY_ID_FILE = CONFIG_HOME / "omarchy-voice" / "safety-id"


def _runtime_dir() -> Path:
    # Prefer the systemd runtime dir (mode 700). Never fall back to a world-
    # writable /tmp — the control socket would be an unauthenticated desktop
    # remote for every local user.
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "omarchy-voice"
    return STATE_HOME / "omarchy-voice" / "run"


RUNTIME_DIR = _runtime_dir()


def load_env_file(path: Path = ENV_FILE) -> list[str]:
    """Merge ~/.config/omarchy-voice/env into os.environ.

    The systemd unit reads this file through EnvironmentFile, so the daemon has
    the keys either way. Without this, the *CLI* does not. A real environment
    variable always wins. Returns warnings (world-readable file, etc.).
    """
    warnings: list[str] = []
    try:
        st = path.stat()
        text = path.read_text()
    except OSError:
        return warnings
    if st.st_mode & 0o077:
        warnings.append(f"{path} is readable by group/other — chmod 600")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value
    return warnings


CONFIG_DIR = CONFIG_HOME / "omarchy-voice"
CONFIG_FILE = CONFIG_DIR / "config.toml"
CACHE_DIR = CACHE_HOME / "omarchy-voice"
STATE_DIR = STATE_HOME / "omarchy-voice"
SOCKET_PATH = RUNTIME_DIR / "control.sock"
STATE_FILE = RUNTIME_DIR / "state.json"
# Its own file, deliberately. This changes ten times a second while listening;
# status changes a few times a minute. A watcher on the status file should not
# have to wake for every audio frame.
LEVEL_FILE = RUNTIME_DIR / "level"
LOG_FILE = STATE_DIR / "session.log"

# Actions matched by these patterns always ask before running. They are the
# things you cannot undo by saying "no, the other one".
DEFAULT_CONFIRM = [
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bpoweroff\b",
    r"\bsuspend\b",
    r"\bhibernat",
    r"\bomarchy\s+update\b",
    r"\bomarchy\s+drive\b",
    r"\bomarchy\s+pkg\b",
    r"\bomarchy\s+install\b",
    r"\bomarchy\s+refresh\b",
    r"\bomarchy\s+reinstall\b",
    r"\bhl\.dsp\.exit\b",
    r"\bclose[-_ ]?all\b",
]

# Never run, whatever the model decides. A voice channel is an open microphone;
# anything on this list is not worth the tail risk of a misheard sentence.
DEFAULT_DENY = [
    r"\brm\s+-[a-zA-Z]*[rf]",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\b(shred|wipefs)\b",
    r">\s*/dev/[sn][dv]",
    r"\bpasswd\b",
    r"\bsudo\b",
    r"\bpkexec\b",
    r"\bcryptsetup\b",
    r"\bcurl\b.*\|\s*(ba)?sh",
    r"\bgit\s+push\b",
    r"\bssh\b",
]


# Keys that used to mean something. Kept out of `unknown_keys` so an existing
# config does not get reported as full of typos, and named in `doctor` so the
# user learns why the setting stopped having an effect rather than wondering.
RETIRED_KEYS = {
    "mode": "listening is toggle-only now; there is no always-on mode",
}


# Sections whose keys are namespaced rather than flattened, because the plain
# names are already taken by another section.
PREFIXED_SECTIONS = {"realtime"}

# List-valued policy keys union with the built-in lists unless the matching
# `*_replace` flag is set. Unknown keys are kept so doctor can report typos.
LIST_UNION_KEYS = {
    "confirm_patterns": DEFAULT_CONFIRM,
    "deny_patterns": DEFAULT_DENY,
}


@dataclass
class Config:
    # --- openai ------------------------------------------------------------
    planner_model: str = "gpt-4.1"
    api_key_env: str = "OPENAI_API_KEY"
    # Tool rounds allowed on one spoken instruction before the assistant has to
    # be asked again. A goal worked properly is a loop — act, wait, look, act —
    # so 8 ran out halfway through anything with three steps in it and the user
    # had to say "carry on". Each round costs a turn against the per-minute
    # token budget, which is why this is 12 and not 30.
    max_turns: int = 12

    # --- ears --------------------------------------------------------------
    # There is no mode. Listening is off when the daemon starts and only the
    # toggle turns it on — see RETIRED_KEYS. An always-on microphone is not a
    # setting worth having on a machine that streams room audio to an API.
    device: str = ""  # PipeWire target; empty means the default source
    # Whether the microphone stays live while she is speaking.
    #
    # Off by default, and the default matters: with speakers, her voice leaves
    # the room and comes back into an open mic. The server's turn detection
    # hears it, cancels the reply mid-word, and transcribes it as the user —
    # a session log has her saying "OH-mah, OH-mah, OH-mah", hearing it back as
    # "어마", and answering herself. Worse, a stray fragment that transcribes as
    # an instruction gets *run*: one arrived as "Бела." and pressed CTRL+R.
    #
    # Turn it on if you wear headphones, or if you have set up PipeWire's
    # echo-cancel module — then you get to interrupt her mid-sentence, which is
    # the thing this costs.
    barge_in: bool = False

    # --- realtime ----------------------------------------------------------
    # These live under [realtime] in the config file; the loader prefixes that
    # section's keys, because `model` already means the planner model.
    realtime_model: str = "gpt-realtime-2.1"
    realtime_voice: str = "marin"
    realtime_turn_detection: str = "semantic_vad"
    realtime_sample_rate: int = 24000
    # Transcribes YOUR audio back to us. Off by default upstream, so without it
    # the log records what the assistant said and nothing about what was asked —
    # which makes a misheard command impossible to tell from a bad decision.
    # Empty string disables it. Shape verified against the live API:
    # session.audio.input.transcription = {"model": ...}
    realtime_transcribe_model: str = "gpt-4o-mini-transcribe"

    # --- hands -------------------------------------------------------------
    allow_shell: bool = False
    confirm_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_CONFIRM))
    deny_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_DENY))
    confirm_patterns_replace: bool = False
    deny_patterns_replace: bool = False
    confirm_words: list[str] = field(default_factory=lambda: ["confirm", "yes do it", "go ahead"])
    cancel_words: list[str] = field(default_factory=lambda: ["cancel", "never mind", "nevermind"])

    # --- mouth -------------------------------------------------------------
    notify: bool = True
    speak: bool = False  # TTS replies, needs piper or espeak-ng
    tts_command: str = ""

    # --- misc --------------------------------------------------------------
    dry_run: bool = False
    verbose: bool = False
    unknown_keys: list[str] = field(default_factory=list)
    retired_keys: list[str] = field(default_factory=list)


def load(path: Path | None = None, **overrides) -> Config:
    """Load config.toml, then apply keyword overrides (CLI flags win)."""
    path = path or CONFIG_FILE
    data: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        for key, value in raw.items():
            if isinstance(value, dict):
                prefix = f"{key}_" if key in PREFIXED_SECTIONS else ""
                data.update({f"{prefix}{k}": v for k, v in value.items()})
            else:
                data[key] = value

    known = {f.name for f in Config.__dataclass_fields__.values()}
    unknown = sorted(set(data) - known - set(RETIRED_KEYS))
    cfg = Config(**{k: v for k, v in data.items() if k in known})
    cfg.unknown_keys = unknown
    cfg.retired_keys = sorted(set(data) & set(RETIRED_KEYS))

    for key, builtin in LIST_UNION_KEYS.items():
        if key in data and not data.get(f"{key}_replace", False):
            merged = list(dict.fromkeys([*builtin, *data[key]]))
            cfg = replace(cfg, **{key: merged})

    return replace(cfg, **{k: v for k, v in overrides.items() if v is not None})


def _secure_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def ensure_dirs() -> None:
    for directory in (CONFIG_DIR, CACHE_DIR, STATE_DIR, RUNTIME_DIR):
        _secure_mkdir(directory)


def dir_is_private(path: Path) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(st.st_mode)
        and (st.st_mode & 0o077) == 0
        and st.st_uid == os.getuid()
    )


def warn_env_permissions(warnings: list[str]) -> None:
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
