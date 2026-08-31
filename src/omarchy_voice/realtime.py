"""The realtime engine: speech in, speech out, tools in between.

The microphone streams to OpenAI's Realtime API over a websocket, the model
answers in its own voice, and the only thing this module does in the middle
is run the tool calls — through `tools.Executor`, so the deny/confirm policy
applies.

    pw-record ──▶ websocket ──▶ gpt-realtime ──▶ audio ──▶ pw-cat
                                     │
                                     ▼  function_call
                               policy gate ──▶ denied / held
                                     │
                                     ▼
                        hyprctl · omarchy · wtype · uwsm-app

While listening is active, room audio is streamed continuously to OpenAI.
The mute gate is the lifetime of the `pw-record` process — when you toggle
listening off, capture stops, rather than being captured and discarded.
"""

from __future__ import annotations

import array
import asyncio
import base64
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import capabilities
from .config import Config, CONFIG_DIR, ENV_FILE, SAFETY_ID_FILE
from .feedback import Feedback
from .persona import PERSONA
from .session import ControlServer, _matches
from .tools import TOOL_SCHEMAS, Executor, tools_for

REALTIME_URL = "wss://api.openai.com/v1/realtime"

# 100 ms of 24 kHz mono PCM16. Small enough that turn detection feels immediate,
# large enough that we are not sending a websocket frame every few milliseconds.
FRAME_BYTES = 4800

# Server errors that mean "you were slightly late", not "something is wrong".
# Cancelling a response that finished a moment earlier is unavoidable: the
# decision to barge in is made here, the completion happens there.
BENIGN_ERRORS = {"response_cancel_not_active", "item_not_found"}

# A response can come back `failed` because the org ran out of tokens-per-minute
# rather than because anything about the request was wrong, and the fix is to
# wait and ask again. Retried this many times per user turn before giving up and
# saying so out loud.
RATE_LIMIT_RETRIES = 2
# Fallback wait when the server does not say how long to hold off for.
RATE_LIMIT_PAUSE = 2.0

# A websocket does not survive a sleeping laptop or a wifi hiccup. Rebuild it
# rather than ending the run; give up only after this many tries in a row.
RECONNECT_ATTEMPTS = 6
RECONNECT_BASE_DELAY = 2.0
RECONNECT_MAX_DELAY = 30.0


def frame_level(chunk: bytes) -> float:
    """Loudness of one PCM16 frame as 0..1, shaped for an eye not a meter.

    Every fourth sample is enough for a glow and keeps this off the hot path of
    the audio pump. The square root matters: speech RMS sits low in a linear
    scale, so a linear orb barely moves while someone talks normally. The curve
    lifts speech into a range that reads as motion without letting room tone
    look like conversation.
    """
    usable = len(chunk) // 2 * 2
    if usable < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(chunk[:usable])
    window = samples[::4] or samples
    rms = math.sqrt(sum(s * s for s in window) / len(window)) / 32768.0
    # Calibrated against tones: silence and room tone land on 0, quiet speech
    # ~0.36, normal ~0.61, loud ~0.87, clipping 1.0. The earlier 1.8 with no
    # gate saturated at normal speech and let room tone twitch the orb.
    value = (rms ** 0.5) * 1.28
    return 0.0 if value < 0.10 else min(1.0, (value - 0.10) / 0.90)

# The desktop snapshot goes stale as the user moves windows around; refresh it
# when a new turn starts, but not more often than this.
STATE_REFRESH_SECONDS = 5.0

# How often the background watcher asks tmux whether a watched command has
# finished. Two seconds is well under the time it takes anyone to notice, and
# the poll is one `tmux list-panes`, which costs nothing.
WATCH_POLL_SECONDS = 2.0
# Do not interrupt more often than this, however many jobs land together. An
# assistant that talks over itself is worse than one that is slightly late.
WATCH_MIN_GAP_SECONDS = 8.0


class RealtimeUnavailable(RuntimeError):
    """Something the realtime path needs is missing; the message says what."""


# --- what the model is told -------------------------------------------------

REALTIME_PERSONA = """\
# Speaking

You are talking out loud, not writing. Keep replies to one short sentence —
what you say is heard, not read. Do not read out window addresses, Lua, or
tool names; say what happened in human words.

Act first, narrate after. Never describe an action as done before the tool
call that does it has returned successfully.

Ordinary actions are instant — switching workspace, closing a window, changing
volume. For those, call the tool and say what happened once it returns. Do not
announce them first: "Okay, switching to workspace four now" ahead of the call
adds a second of speech before anything moves, and if the call then fails you
have already told the user something untrue.

Speak first ONLY when the tool will visibly take a while — `compose_windows`,
`wait_for`, or anything opening several applications. One short line, then the
call.

Working towards a goal takes several rounds, and the user hears nothing during
them. Say what you are setting off to do before the first step, and say what
happened at the end. Do not narrate every step in between — a running
commentary is as bad as silence — but never go more than a few actions without
a word.

# Speaking is not doing

Saying something out loud does not make it happen. This desktop changes only
when a tool call runs. A reply of "switching workspaces now" with no tool call
in it changed nothing, and told the user something untrue.

So: if the user asks for anything to change, the response must contain a tool
call. Speech instead of a call is a no-op dressed up as success.

Do not go silent either. Speak on EVERY turn, without exception:

* After the tools return, one short line saying what happened — "Switched to
  workspace 2", "Closed the browser".
* If a tool failed, say so plainly and say what you are doing instead. Never
  fall silent on an error; silence looks identical to a crash from the outside.
* If you did not understand, say that and ask. Guessing quietly is worse.

A turn where you act but say nothing is a bug, not efficiency. The user cannot
see your tool calls — speech is the only evidence you are alive.

If you are unsure which tool fits, call `hypr_query` and look — an extra look
is cheap, a silent no-op is not.

# The snapshot

A "# The desktop right now" note arrives in this conversation at the start of
every turn. It is read off the live system that moment: monitors, which
workspaces have windows, the focused window, and every open window with its
address.

Use it. Do not call `hypr_query` for something the newest snapshot already
tells you — that is a whole extra round trip before anything happens, and the
answer is already on screen in front of you. Query when you need something the
snapshot does not carry (`binds`, `devices`, `layers`), or when you have just
changed the desktop and need to see the result.


# Confirmations

Some actions are held by a safety gate on this machine — shutting down,
rebooting, suspending, package installs, closing everything. When a tool tells
you an action needs spoken confirmation:

1. Stop. Do not look for another route around the gate — there isn't one, and
   trying is a bug, not resourcefulness.
2. Say out loud what is being held and ask the user to confirm it.
3. When they answer, call `confirm_last` with `heard_phrase` set to exactly the
   words you heard them say — verbatim, not your interpretation of them. This
   machine, not you, decides whether those words count as a confirmation.
   Never call `confirm_last` in the same response that created the hold, and
   never call it unless the user has just spoken.
4. If they decline or change the subject, call `cancel_last` instead.

Never call `confirm_last` with words the user did not actually say. If you did
not hear a clear answer, ask again. The user can also confirm from the bar
widget or `omarchy-voice listen confirm`, which does not go through you at all.
"""

# Realtime-only tools. Not in tools.TOOL_SCHEMAS: confirmations for `say` are
# handled by the CLI prompt, not by the model reporting a phrase.
GATE_TOOLS = [
    {
        "type": "function",
        "name": "confirm_last",
        "description": (
            "Release the action that the safety gate is holding, after the user has "
            "confirmed it out loud in a later turn. Pass the words you actually heard; "
            "this machine checks them against its own confirmation phrases and refuses "
            "if they do not match. Do not call this in the same response that held the "
            "action, and do not call it unless the user just spoke."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "heard_phrase": {
                    "type": "string",
                    "description": "Verbatim what the user just said, e.g. \"confirm\".",
                },
            },
            "required": ["heard_phrase"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "cancel_last",
        "description": (
            "Drop the action the safety gate is holding, because the user declined it "
            "or asked for something else instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


def to_realtime_tools(schemas: list[dict] | None = None) -> list[dict]:
    """Executor tool schemas -> Realtime function schemas."""
    converted = []
    for schema in schemas if schemas is not None else TOOL_SCHEMAS:
        converted.append({
            "type": "function",
            "name": schema["name"],
            "description": schema["description"],
            "parameters": schema["input_schema"],
        })
    return converted + GATE_TOOLS


# --- audio ------------------------------------------------------------------

class Speaker:
    """Model audio out, through pw-cat.

    One long-lived process fed PCM16 as it arrives. `interrupt` kills it, which
    is the point: terminating the process drops whatever PipeWire had buffered,
    so a barge-in stops the reply mid-word instead of finishing the sentence
    over the top of the user.
    """

    def __init__(self, rate: int):
        self.rate = rate
        self._proc: asyncio.subprocess.Process | None = None
        # Playback runs on its own task. pw-cat drinks audio at real time while
        # the model sends it far faster, so draining its pipe inline stalled
        # whoever was writing — and that was the single loop reading the
        # websocket. A spoken reply therefore froze event handling, tool calls
        # included, for as long as it took to say it. Queueing hands the wait to
        # a task nobody else is waiting on.
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._pump: asyncio.Task | None = None

    async def _ensure(self) -> asyncio.subprocess.Process:
        if self._proc is None or self._proc.returncode is not None:
            self._proc = await asyncio.create_subprocess_exec(
                "pw-cat", "--playback", "--raw",
                "--rate", str(self.rate), "--channels", "1", "--format", "s16", "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        return self._proc

    async def write(self, pcm: bytes) -> None:
        """Hand a chunk to the player. Never waits on playback."""
        if self._pump is None or self._pump.done():
            self._pump = asyncio.create_task(self._pump_loop())
        self._queue.put_nowait(pcm)

    async def _pump_loop(self) -> None:
        while True:
            pcm = await self._queue.get()
            try:
                proc = await self._ensure()
                if proc.stdin is None:
                    continue
                proc.stdin.write(pcm)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                self._proc = None
            except asyncio.CancelledError:
                raise
            finally:
                self._queue.task_done()

    def _drop_queued(self) -> None:
        """Throw away audio not yet played. Barge-in must not be finished later."""
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._queue.task_done()

    async def interrupt(self) -> None:
        await self.close()

    async def close(self) -> None:
        pump, self._pump = self._pump, None
        if pump is not None and not pump.done():
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
        self._drop_queued()
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                pass
        await _terminate(proc)


# --- the session ------------------------------------------------------------

class RealtimeSession:
    def __init__(self, config: Config):
        self.config = config
        self.feedback = Feedback(config)
        self.executor = Executor(config, on_action=self._on_action)
        self.speaker = Speaker(config.realtime_sample_rate)
        # Always starts muted. There is no configuration that changes this:
        # the only thing that opens the microphone is the toggle.
        self.active = False
        self.ws: Any = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._send_lock = asyncio.Lock()
        self._active_event = asyncio.Event()
        self._stop = asyncio.Event()
        self._mic: asyncio.subprocess.Process | None = None
        self._transcript = ""
        self._state_refreshed = 0.0
        self._refreshing = False
        # Item id of the snapshot currently in the conversation, so the next one
        # can replace it instead of piling up behind it.
        self._state_item: str | None = None
        self._state_seq = 0
        self._refresh_task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None
        self._last_interruption = 0.0
        self._user_turn_since_hold = False
        self._ignored_responses: set[str] = set()
        self._audio_item_id: str | None = None
        self._audio_response_id: str | None = None
        self._audio_bytes = 0
        # Set between response.created and response.done. Barge-in used to fire
        # response.cancel unconditionally, so every interruption that arrived
        # between turns logged "no active response found".
        self._response_running = False
        # Tool rounds since the user last said anything. Each round of tool
        # calls ends by asking for another response, so a model that keeps
        # retrying drives itself: a failing launch looped every ~30 s, opening
        # terminals long after listening had been switched off. config.max_turns
        # existed but nothing in this engine enforced it.
        self._tool_rounds = 0
        self._appended_audio = False
        # Retries spent on rate-limited responses since the user last spoke.
        self._rate_limit_retries = 0
        self._user_quit = False
        # Set when the websocket dies on us rather than being closed on purpose.
        self._dropped = False
        self._exit_code = 0

    # -- plumbing -----------------------------------------------------------
    def _on_action(self, name: str, description: str) -> None:
        self.feedback.state("acting", description)
        self.feedback.log(f"action  {description}")

    async def _send(self, payload: dict) -> None:
        if self.ws is None:
            return
        async with self._send_lock:
            try:
                await self.ws.send(json.dumps(payload))
            except Exception as exc:  # a dead socket ends the session, not this call
                self.feedback.log(f"error   send failed: {type(exc).__name__}: {exc}")
                # Not a clean stop: the socket died under us. Distinguished from
                # `listen quit` so the reconnect loop knows to come back, and so
                # that giving up exits non-zero for Restart=on-failure.
                self._dropped = True
                self._exit_code = 1
                self._stop.set()

    # -- session configuration ----------------------------------------------
    async def _instructions(self) -> str:
        manifest, live = await asyncio.gather(
            asyncio.to_thread(capabilities.manifest),
            asyncio.to_thread(capabilities.live_state),
        )
        self._state_refreshed = time.monotonic()
        return "\n\n".join([
            PERSONA, REALTIME_PERSONA, manifest,
            "# The desktop right now\n\n" + live,
        ])

    def _turn_detection(self) -> dict | None:
        kind = (self.config.realtime_turn_detection or "").strip()
        if kind in ("", "none", "off", "manual"):
            return None
        return {"type": kind}

    async def _session_update(self) -> dict:
        rate = self.config.realtime_sample_rate
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": self.config.realtime_model,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": rate},
                        "turn_detection": self._turn_detection(),
                        **({"transcription": {"model": self.config.realtime_transcribe_model}}
                           if self.config.realtime_transcribe_model else {}),
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": rate},
                        "voice": self.config.realtime_voice,
                    },
                },
                "instructions": await self._instructions(),
                "tools": to_realtime_tools(tools_for(self.config)),
                "tool_choice": "auto",
            },
        }

    def _kick_refresh(self) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.create_task(self._refresh_state())

    async def _watch_loop(self) -> None:
        """Say when a watched command finishes, without being asked.

        Everything else this daemon does is a reply. This is the one thing it
        starts on its own: a build that ends on workspace two is news, and the
        user should not have to remember to come back and ask.

        The mechanics are the same move the desktop snapshot already makes —
        put an item in the conversation, ask for a response — so the model
        speaks about it in its own voice rather than a canned string being read
        out. What is new is the trigger.
        """
        while not self._stop.is_set():
            try:
                await asyncio.sleep(WATCH_POLL_SECONDS)
                finished = await asyncio.to_thread(self.executor.poll_watches)
                for job in finished:
                    await self._announce(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a watcher must never take the session down
                self.feedback.log(f"warn    watcher: {type(exc).__name__}: {exc}")

    async def _announce(self, job: dict) -> None:
        """Interrupt with a finished job — or notify, if nobody is listening."""
        if job["vanished"]:
            headline = f"The pane running {job['label']} was closed."
        elif job["timed_out"]:
            headline = f"{job['label']} is still going after a long time."
        else:
            headline = f"{job['label']} finished in {job['seconds']:.0f} seconds."
        self.feedback.log(f"watch   {job['target']}: {headline}")

        # Muted means the microphone is off, and speaking into a room that is
        # not listening is just noise. A notification waits until it is read.
        if not self.active:
            self.feedback.notify("Oma", headline)
            return
        # Never cut across a reply in flight, and never pile announcements on
        # top of each other. Late is fine; talking over yourself is not.
        while self._response_running and not self._stop.is_set():
            await asyncio.sleep(0.5)
        gap = time.time() - self._last_interruption
        if gap < WATCH_MIN_GAP_SECONDS:
            await asyncio.sleep(WATCH_MIN_GAP_SECONDS - gap)
        if self._stop.is_set():
            return
        self._last_interruption = time.time()
        tail = (job["tail"] or "").strip()
        await self._send({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "system", "content": [{
                "type": "input_text",
                "text": (
                    f"# A watched command finished\n\n{headline} It ran in tmux pane "
                    f"{job['target']}.\n\nThe last of what it printed:\n\n{tail}\n\n"
                    "Tell the user now, unprompted and in one short sentence: what "
                    "finished, and whether it looks like it worked, from the output "
                    "above rather than from hope. Then ask if they want you to carry "
                    "on. They did not just speak to you — do not answer as though "
                    "they had."
                )}]},
        })
        await self._send({"type": "response.create"})

    async def _refresh_state(self, force: bool = False) -> None:
        """Append a fresh desktop snapshot to the conversation, between turns.

        This used to rewrite `instructions` with a whole new session.update. It
        worked, and it was the single most expensive thing the session did: the
        instructions are the cached prefix — persona, the ~6.6k-token capability
        manifest, the tool schemas — and changing one byte of them invalidates
        that cache. Every turn re-prefilled about 9k tokens that had not changed
        since the socket opened, and the user waited through it before the model
        had thought about what they said.

        A conversation item *appends* instead, so the prefix survives and only
        the ~80 tokens of snapshot are new. It is sent on speech_started, while
        the user is still talking, so it costs no perceptible time at all.

        The old snapshot is then deleted, which matters twice over. Left in, a
        long session accumulates one stale window list per turn — paid for on
        every later turn, against a tokens-per-minute limit that counts cached
        tokens too — and the model gets a pile of contradictory descriptions of
        the same desktop, only the last of which is true.
        """
        # The interval guard stops repeated speech_started events refreshing
        # several times inside one turn. It must not suppress the snapshot for a
        # turn that is definitely new: `_instructions` stamps the clock at
        # session start, so a turn arriving in the first few seconds was getting
        # no snapshot at all, and the model answered from nothing.
        if self._refreshing:
            return
        if not force and time.monotonic() - self._state_refreshed < STATE_REFRESH_SECONDS:
            return
        self._refreshing = True
        try:
            live = await asyncio.to_thread(capabilities.live_state)
            self._state_refreshed = time.monotonic()
            self._state_seq += 1
            item_id = f"item_omastate{self._state_seq:016d}"
            await self._send({
                "type": "conversation.item.create",
                "item": {
                    "id": item_id,
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": (
                        "# The desktop right now (current as of this turn)\n\n"
                        + live +
                        "\n\nThis snapshot is fresh. Trust it for workspaces, monitors "
                        "and window addresses instead of calling hypr_query again."
                    )}],
                },
            })
            stale, self._state_item = self._state_item, item_id
            if stale:
                await self._send({"type": "conversation.item.delete", "item_id": stale})
        except Exception as exc:
            self.feedback.log(f"error   state refresh: {type(exc).__name__}: {exc}")
        finally:
            self._refreshing = False

    # -- microphone ---------------------------------------------------------
    async def _mic_loop(self) -> None:
        """Capture only while active.

        The gate is the process, not a branch on the send path: while listening
        is off there is no recorder running, so there is nothing to leak.
        """
        while not self._stop.is_set():
            waiter = asyncio.create_task(self._active_event.wait())
            stopper = asyncio.create_task(self._stop.wait())
            done, pending = await asyncio.wait(
                {waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if self._stop.is_set():
                return

            cmd = ["pw-record", "--rate", str(self.config.realtime_sample_rate),
                   "--channels", "1", "--format", "s16", "--latency", "20ms"]
            if self.config.device:
                cmd += ["--target", self.config.device]
            cmd.append("-")
            try:
                self._mic = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
            except FileNotFoundError:
                self.feedback.log("error   pw-record is missing — install pipewire-audio")
                self._stop.set()
                self._exit_code = 1
                return

            await self._send({"type": "input_audio_buffer.clear"})
            self.feedback.log("mic     capturing")
            stdout = self._mic.stdout
            assert stdout is not None
            try:
                while self._active_event.is_set() and not self._stop.is_set():
                    chunk = await stdout.read(FRAME_BYTES)
                    if not chunk:
                        # Toggling off terminates pw-record while this read is
                        # already blocked, so EOF is the normal way a capture
                        # ends. Only an EOF we did not ask for is a failure —
                        # treating every EOF as fatal took the whole daemon
                        # down each time listening was switched off.
                        if self._active_event.is_set() and not self._stop.is_set():
                            self.feedback.log("error   pw-record ended unexpectedly")
                            self._exit_code = 1
                            self._stop.set()
                        break
                    self._appended_audio = True
                    self.feedback.level(frame_level(chunk))
                    await self._send({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(chunk).decode(),
                    })
            finally:
                await self._kill_mic()
                # Leave the meter at rest, or the orb keeps the last loud frame.
                self.feedback.level(0.0)
                self.feedback.log("mic     stopped")

    async def _kill_mic(self) -> None:
        proc, self._mic = self._mic, None
        if proc is None or proc.returncode is not None:
            return
        await _terminate(proc)

    # -- control socket -----------------------------------------------------
    def _control(self, command: str) -> str:
        """Called on the ControlServer thread; hands work to the event loop."""
        if self.loop is None:
            return "not ready"
        verb, _, rest = command.partition(" ")
        if verb == "toggle":
            future = asyncio.run_coroutine_threadsafe(self._toggle(), self.loop)
        elif verb in ("start", "stop"):
            future = asyncio.run_coroutine_threadsafe(
                self._set_active(verb == "start"), self.loop)
        elif verb == "say":
            future = asyncio.run_coroutine_threadsafe(self._inject(rest), self.loop)
        elif verb == "confirm":
            future = asyncio.run_coroutine_threadsafe(self._local_confirm(), self.loop)
        elif verb == "cancel":
            future = asyncio.run_coroutine_threadsafe(self._local_cancel(), self.loop)
        elif verb == "quit":
            self._user_quit = True
            self.loop.call_soon_threadsafe(self._stop.set)
            return "stopping"
        else:
            return f"unknown command {verb!r}"
        try:
            return future.result(timeout=10)
        except Exception as exc:
            return f"error: {type(exc).__name__}: {exc}"

    async def _toggle(self) -> str:
        return await self._set_active(not self.active)

    async def _set_active(self, active: bool) -> str:
        self.active = active
        if active:
            self._active_event.set()
        else:
            self._active_event.clear()
            await self._commit_if_manual()
            await self._kill_mic()
            await self.speaker.interrupt()
        self.feedback.state("listening" if active else "idle")
        self.feedback.notify("Listening" if active else "Sleeping")
        self.feedback.log(f"gate    {'listening' if active else 'muted'}")
        return "listening" if active else "idle"

    async def _commit_if_manual(self) -> None:
        """With VAD off, toggling the microphone off is the end of the turn."""
        if self._turn_detection() is not None or not self._appended_audio:
            return
        self._appended_audio = False
        await self._send({"type": "input_audio_buffer.commit"})
        await self._send({"type": "response.create"})

    async def _inject(self, text: str) -> str:
        """`omarchy-voice listen say ...` — a typed turn, mic untouched."""
        text = text.strip()
        if not text:
            return "nothing to say"
        self._user_turn_since_hold = True
        self._tool_rounds = 0
        self._rate_limit_retries = 0
        self.feedback.log(f"typed   {text!r}")
        # Awaited, not kicked off: a typed turn asks for a response immediately,
        # so a background refresh would land after the model had already decided.
        # Without this a typed turn was the only kind that arrived with no
        # snapshot, and it spent a round trip on a hypr_query for state that a
        # spoken turn would have been handed for free.
        await self._refresh_state(force=True)
        await self._send({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": text}]},
        })
        await self._send({"type": "response.create"})
        return "sent"

    async def _local_confirm(self) -> str:
        """Keybind / CLI confirm — does not trust the model."""
        if not self.executor.pending:
            return "nothing to confirm"
        held = self.executor.describe(*self.executor.pending)
        self.feedback.log(f"confirm local release: {held}")
        result = await asyncio.to_thread(self.executor.run_pending)
        self._settle()
        return result.as_tool_result()

    async def _local_cancel(self) -> str:
        held = self.executor.drop_pending()
        if held is None:
            return "nothing to cancel"
        self.feedback.log(f"cancel  {held}")
        self._settle()
        return f"Cancelled: {held}. It was not run."

    # -- events -------------------------------------------------------------
    async def _on_event(self, event: dict) -> None:
        kind = event.get("type", "")

        if kind == "session.created":
            self.feedback.log(f"start   realtime session {event.get('session', {}).get('id', '?')}")
        elif kind == "input_audio_buffer.speech_started":
            self.feedback.state("listening")
            self._user_turn_since_hold = True
            # A new instruction earns a fresh budget of tool rounds.
            self._tool_rounds = 0
            self._rate_limit_retries = 0
            await self._barge_in()
            self._kick_refresh()
        elif kind == "input_audio_buffer.speech_stopped":
            self.feedback.state("thinking")
        elif kind == "response.output_audio.delta":
            await self._on_audio_delta(event)
        elif kind == "response.output_audio_transcript.delta":
            self._transcript += event.get("delta") or ""
        elif kind == "response.output_audio_transcript.done":
            said = (event.get("transcript") or self._transcript).strip()
            self._transcript = ""
            if said:
                self.feedback.log(f"reply   {said!r}")
                self.feedback.notify(said)
        elif kind == "conversation.item.input_audio_transcription.completed":
            heard = (event.get("transcript") or "").strip()
            if heard:
                self.feedback.log(f"heard   {heard!r}")
        elif kind == "conversation.item.input_audio_transcription.failed":
            error = event.get("error") or {}
            self.feedback.log(
                f"heard   (transcription failed: {error.get('message', 'unknown')})")
        elif kind == "rate_limits.updated":
            # Never logged before, which is why "tier 3 but enforced at 40k"
            # took a session to notice. The server states the real ceiling on
            # every turn; write it down.
            for limit in event.get("rate_limits") or []:
                self.feedback.log(
                    f"limits  {limit.get('name')}: {limit.get('remaining')}"
                    f"/{limit.get('limit')} left, resets in {limit.get('reset_seconds')}s")
        elif kind == "response.created":
            self._response_running = True
        elif kind == "response.done":
            self._response_running = False
            # Audio is measured per item; a finished response must not leave
            # its byte count to be charged against the next one.
            self._audio_item_id = None
            self._audio_bytes = 0
            await self._on_response_done(event)
        elif kind == "error":
            error = event.get("error", {})
            message = f'{error.get("code", "error")}: {error.get("message", "")}'
            if error.get("param"):
                message += f' (param {error["param"]})'
            if error.get("code") in BENIGN_ERRORS:
                # A race, not a fault: the response finished server-side between
                # the barge-in decision and the cancel arriving. Logged quietly
                # so it cannot turn the orb red or fire a notification, and so
                # real errors are not lost in the noise.
                self.feedback.log(f"note    {message}")
                return
            self.feedback.log(f"error   {message}")
            self.feedback.state("error", message)
            self.feedback.notify("Voice error", message, urgency="normal")

    async def _barge_in(self) -> None:
        await self.speaker.interrupt()
        if self._audio_item_id:
            rate = self.config.realtime_sample_rate
            audio_end_ms = int(self._audio_bytes / max(rate * 2, 1) * 1000)
            await self._send({
                "type": "conversation.item.truncate",
                "item_id": self._audio_item_id,
                "content_index": 0,
                "audio_end_ms": audio_end_ms,
            })
        # Audio already playing means a response is in flight even if the
        # response.created that started it was never seen — so either signal is
        # enough to justify the cancel. What this rules out is the case that
        # actually logged "no active response found": a barge-in arriving
        # between turns, with nothing running at all.
        if self._response_running or self._audio_item_id:
            await self._send({"type": "response.cancel"})
            self._response_running = False
        if self._audio_response_id:
            self._ignored_responses.add(self._audio_response_id)
        self._audio_item_id = None
        self._audio_response_id = None
        self._audio_bytes = 0

    async def _on_audio_delta(self, event: dict) -> None:
        response_id = event.get("response_id") or ""
        if response_id in self._ignored_responses:
            return
        delta = event.get("delta") or ""
        if not delta:
            return
        try:
            pcm = base64.b64decode(delta)
        except (ValueError, TypeError) as exc:
            self.feedback.log(f"error   bad audio delta: {exc}")
            return
        self._audio_response_id = response_id or self._audio_response_id
        item_id = event.get("item_id") or self._audio_item_id
        if item_id != self._audio_item_id:
            # New item: restart the count. Truncation is expressed as an offset
            # into one item, so a running total across items asks the server to
            # cut at a point past the end of what it holds — the source of
            # "Audio content of 1850ms is already shorter than 4850ms".
            self._audio_item_id = item_id
            self._audio_bytes = 0
        self._audio_bytes += len(pcm)
        await self.speaker.write(pcm)

    async def _on_response_done(self, event: dict) -> None:
        response = event.get("response") or {}
        status = response.get("status") or "completed"
        if status != "completed":
            await self._report_dead_response(status, response.get("status_details") or {})
            self._settle()
            return

        outputs = response.get("output") or []
        calls = [item for item in outputs if item.get("type") == "function_call"]
        if not calls:
            self._settle()
            return

        created_hold = False
        for call in calls:
            name = call.get("name", "")
            try:
                args = json.loads(call.get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                output = f"ERROR: could not parse arguments: {exc}"
            else:
                if name == "confirm_last":
                    if created_hold or not self._user_turn_since_hold:
                        output = (
                            "ERROR: confirmation must come from a new user turn after "
                            "the action was held. Do not call confirm_last in the same "
                            "response as the gated tool."
                        )
                        self.feedback.log("reject  same-batch or no-new-turn confirm_last")
                    else:
                        output = await self._confirm(str(args.get("heard_phrase", "")))
                elif name == "cancel_last":
                    output = self._cancel()
                else:
                    output = await self._dispatch(name, args)
                    if self.executor.pending:
                        created_hold = True
                        self._user_turn_since_hold = False
            await self._send({
                "type": "conversation.item.create",
                "item": {"type": "function_call_output",
                         "call_id": call.get("call_id", ""),
                         "output": output},
            })

        self._tool_rounds += 1
        if self._tool_rounds >= self.config.max_turns:
            # Stop driving the conversation. The session stays open and the next
            # thing the user says starts a fresh budget; what ends here is the
            # model's ability to keep acting on its own.
            self.feedback.log(
                f"guard   stopped after {self._tool_rounds} tool rounds "
                f"with no new user turn (max_turns={self.config.max_turns})")
            self.feedback.notify(
                "OMA stopped", "Too many steps without a new instruction.")
            self._settle()
            return

        await self._send({"type": "response.create"})
        self._settle()

    async def _report_dead_response(self, status: str, details: dict) -> None:
        """Say something when a turn produces nothing.

        A response that comes back `failed` or `incomplete` used to be dropped
        on the floor: no log line, no notification, no speech. From the user's
        side that is indistinguishable from not being heard, and the session log
        showed `heard ...` with nothing after it — someone asked to switch
        workspace four times in a row and got silence four times.

        The usual cause is the token rate limit rather than anything about the
        request, and that one is worth naming out loud, because saying it again
        louder is exactly the wrong response to it.
        """
        error = details.get("error") or {}
        code = error.get("code") or details.get("reason") or status
        message = (error.get("message") or "").strip()
        self.feedback.log(f"error   response {status}: {code}"
                          + (f" — {message[:160]}" if message else ""))

        if code == "rate_limit_exceeded" and self._rate_limit_retries < RATE_LIMIT_RETRIES:
            self._rate_limit_retries += 1
            pause = _retry_after(message)
            self.feedback.state("thinking", "rate limited — retrying")
            self.feedback.log(f"retry   rate limited, waiting {pause:.1f}s "
                              f"({self._rate_limit_retries}/{RATE_LIMIT_RETRIES})")
            await asyncio.sleep(pause)
            await self._send({"type": "response.create"})
            return

        if code == "rate_limit_exceeded":
            spoken = "I have hit the API rate limit — give me a moment."
        elif status == "incomplete":
            spoken = "That turn was cut short."
        else:
            spoken = "That did not go through."
        self.feedback.state("error", f"{code}: {message[:80]}" if message else code)
        self.feedback.notify("Oma could not answer", spoken, urgency="normal")
        self.feedback.speak(spoken)

    def _settle(self) -> None:
        """Put the bar back to whatever the session is actually doing."""
        if self.executor.pending:
            held = self.executor.describe(*self.executor.pending)
            self.feedback.state("confirm", held)
            self.feedback.notify("Waiting for confirmation", held, urgency="normal")
        else:
            self.feedback.state("listening" if self.active else "idle")

    # -- tool dispatch ------------------------------------------------------
    async def _dispatch(self, name: str, args: dict) -> str:
        if name == "confirm_last":
            return await self._confirm(str(args.get("heard_phrase", "")))
        if name == "cancel_last":
            return self._cancel()
        result = await asyncio.to_thread(self.executor.call, name, args)
        if self.executor.pending:
            self._settle()
        return result.as_tool_result()

    async def _confirm(self, heard_phrase: str) -> str:
        """The gate the model cannot talk its way through.

        The user's "yes, do it" goes to the model as audio and never reaches
        this process, so the model reports what it heard and *this* code
        decides whether that counts. Same-batch and no-new-turn calls are
        rejected in `_on_response_done` before we get here. The model can
        still lie about the words; the local `listen confirm` path does not
        go through this at all.
        """
        if not self.executor.pending:
            return "ERROR: nothing is waiting for confirmation. Do not call this again."
        held = self.executor.describe(*self.executor.pending)
        if not _matches(heard_phrase, self.config.confirm_words, allow_negation=False):
            self.feedback.log(f"reject  {heard_phrase!r} is not a confirmation of: {held}")
            phrases = ", ".join(f'"{w}"' for w in self.config.confirm_words)
            return (f"ERROR: {heard_phrase!r} is not a confirmation phrase, so {held} is "
                    f"still held. Ask the user to say one of: {phrases}.")
        self.feedback.log(f"confirm {heard_phrase!r} released: {held}")
        result = await asyncio.to_thread(self.executor.run_pending)
        self._settle()
        return result.as_tool_result()

    def _cancel(self) -> str:
        held = self.executor.drop_pending()
        if held is None:
            return "Nothing was being held."
        self.feedback.log(f"cancel  {held}")
        self._settle()
        return f"Cancelled: {held}. It was not run."

    # -- main loop ----------------------------------------------------------
    async def run(self) -> int:
        self.loop = asyncio.get_running_loop()
        key = os.environ.get(self.config.api_key_env, "")
        if not key:
            raise RealtimeUnavailable(
                f"{self.config.api_key_env} is not set — "
                "the realtime engine needs an OpenAI API key")

        url = f"{REALTIME_URL}?model={self.config.realtime_model}"
        headers = {
            "Authorization": f"Bearer {key}",
            "OpenAI-Safety-Identifier": _safety_identifier(),
        }

        control = ControlServer(self._control)
        control.start()
        self._watch_task = asyncio.create_task(self._watch_loop())
        self.feedback.state("listening" if self.active else "idle")
        self.feedback.log(f"start   engine=realtime model={self.config.realtime_model} "
                          f"voice={self.config.realtime_voice} "
                          f"dry_run={self.config.dry_run}")
        self.feedback.log("gate    muted — press SUPER + SHIFT + V to start listening")

        try:
            await self._serve(url, headers)
        except RealtimeUnavailable:
            raise
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.feedback.state("error", str(exc))
            self.feedback.log(f"error   {type(exc).__name__}: {exc}")
            print(f"realtime session failed: {type(exc).__name__}: {exc}")
            return 1
        finally:
            self._stop.set()
            for task in (self._refresh_task, self._watch_task):
                if task is not None:
                    task.cancel()
            await self._kill_mic()
            await self.speaker.close()
            await asyncio.to_thread(control.stop)
            self.feedback.state("idle")
            self.ws = None
        return 0 if self._user_quit else self._exit_code

    async def _serve(self, url: str, headers: dict) -> None:
        """Hold a session open, and rebuild it when the socket dies.

        A websocket to OpenAI does not survive a laptop sleeping, a wifi hiccup,
        or an idle timeout, and it used to take the daemon with it: `_send` saw
        `keepalive ping timeout`, set the stop flag, and the process wound down.
        It exited 0, so `Restart=on-failure` left it down — the service read as
        healthy while the microphone key did nothing. That is the "it stopped
        working" everyone hits eventually.

        So a drop is not the end of the run. Reconnect, with backoff, and only
        give up after RECONNECT_ATTEMPTS in a row — at which point exiting
        non-zero is right and systemd should have a turn.
        """
        delay = RECONNECT_BASE_DELAY
        attempt = 0
        while not self._user_quit:
            self._dropped = False
            self._stop.clear()
            try:
                await self._open_one(url, headers)
                if not self._dropped:
                    attempt = 0
                    delay = RECONNECT_BASE_DELAY
            except (RealtimeUnavailable, asyncio.CancelledError):
                raise
            except Exception as exc:
                self._dropped = True
                self.feedback.log(f"error   {type(exc).__name__}: {exc}")

            if self._user_quit or not self._dropped:
                return
            attempt += 1
            if attempt > RECONNECT_ATTEMPTS:
                self.feedback.log(f"stop    gave up after {RECONNECT_ATTEMPTS} reconnects")
                self.feedback.notify("Voice control stopped",
                                     "Lost the connection and could not get it back.",
                                     urgency="normal")
                self._exit_code = 1
                return
            was_listening, self.active = self.active, False
            self._active_event.clear()
            self.feedback.state("error", f"reconnecting ({attempt}/{RECONNECT_ATTEMPTS})")
            self.feedback.log(f"retry   reconnecting in {delay:.0f}s "
                              f"({attempt}/{RECONNECT_ATTEMPTS})")
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)
            # Come back the way we left: if the mic was open, reopen it.
            if was_listening:
                self.active = True
                self._active_event.set()
                self.feedback.state("listening")

    async def _open_one(self, url: str, headers: dict) -> None:
        """One socket, held until it closes or the user stops it."""
        mic_task: asyncio.Task | None = None
        try:
            async with _open_socket(url, headers) as ws:
                self.ws = ws
                # A reconnect is a new conversation: the snapshot item we were
                # tracking lives in a session that no longer exists.
                self._state_item = None
                self._state_refreshed = 0.0
                self._response_running = False
                await self._send(await self._session_update())
                mic_task = asyncio.create_task(self._mic_loop())
                stopper = asyncio.create_task(self._stop.wait())
                reader = asyncio.create_task(self._read(ws))
                done, pending = await asyncio.wait(
                    {stopper, reader}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if reader in done and not reader.cancelled():
                    reader.result()
                    if not self._user_quit:
                        self._dropped = True
                        self.feedback.log("stop    the server closed the connection")
        finally:
            if mic_task is not None:
                mic_task.cancel()
            await self._kill_mic()
            self.ws = None

    async def _read(self, ws) -> None:
        async for raw in ws:
            if self._stop.is_set():
                return
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if self.config.verbose and event.get("type") not in (
                    "response.output_audio.delta",
                    "response.output_audio_transcript.delta"):
                print(f"  << {event.get('type')}", flush=True)
            try:
                await self._on_event(event)
            except Exception as exc:
                self.feedback.log(f"error   event {event.get('type')}: {type(exc).__name__}: {exc}")


# --- helpers ----------------------------------------------------------------

def _retry_after(message: str) -> float:
    """Seconds to wait, read out of the server's own "try again in ..." text.

    OpenAI puts the figure in the error message rather than anywhere structured
    on the websocket, so it is either parsed out of prose or guessed. Clamped:
    the message occasionally names a wait long enough that the user has given up
    and asked again by the time it elapses.
    """
    match = re.search(r"try again in\s+([\d.]+)\s*(ms|s)\b", message, re.IGNORECASE)
    if not match:
        return RATE_LIMIT_PAUSE
    try:
        value = float(match.group(1))
    except ValueError:
        return RATE_LIMIT_PAUSE
    seconds = value / 1000 if match.group(2).lower() == "ms" else value
    return max(0.3, min(seconds + 0.2, 8.0))


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM, then SIGKILL if it will not go, then reap."""
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
        return
    except (asyncio.TimeoutError, TimeoutError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=1)
    except (asyncio.TimeoutError, TimeoutError, ProcessLookupError):
        pass


def _safety_identifier() -> str:
    """Stable per-install identifier, not a hash of user@host.

    OpenAI uses this to group abuse signals. A random secret written once into
    CONFIG_DIR is enough to be stable without being reconstructible from the
    username and hostname.
    """
    try:
        if not SAFETY_ID_FILE.exists():
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            SAFETY_ID_FILE.write_text(os.urandom(32).hex())
            SAFETY_ID_FILE.chmod(0o600)
        secret = SAFETY_ID_FILE.read_text().strip()
    except OSError:
        secret = "omarchy-voice:anonymous"
    return hashlib.sha256(f"omarchy-voice:{secret}".encode()).hexdigest()[:32]


def _open_socket(url: str, headers: dict):
    """Return an async context manager for the websocket.

    websockets moved from `extra_headers` to `additional_headers` in 14; Arch's
    python-websockets is well past that, but an older one should say so clearly
    rather than raising a TypeError from inside the connect call.
    """
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        try:
            from websockets.legacy.client import connect as legacy_connect  # type: ignore
        except ImportError as exc:
            raise RealtimeUnavailable(
                "python-websockets is not installed — "
                "run: sudo pacman -S python-websockets") from exc
        return legacy_connect(url, extra_headers=headers, max_size=None,
                              ping_interval=20, ping_timeout=20)
    return connect(url, additional_headers=headers, max_size=None,
                   ping_interval=20, ping_timeout=20)


def default_source() -> str:
    """The current default PipeWire source name, or '' if there is none."""
    import subprocess
    try:
        out = subprocess.run(["pactl", "get-default-source"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return "" if out in ("", "@DEFAULT_SOURCE@") else out


def check_ready(config: Config) -> list[str]:
    """Everything standing between here and a working realtime session."""
    problems = []
    try:
        import websockets  # noqa: F401
    except ImportError:
        problems.append("python-websockets is not installed (sudo pacman -S python-websockets)")
    if not os.environ.get(config.api_key_env):
        problems.append(f"{config.api_key_env} is not set")
    for tool, package in (("pw-record", "pipewire-audio"), ("pw-cat", "pipewire-audio")):
        if not shutil.which(tool):
            problems.append(f"{tool} is missing (install {package})")
    source = default_source()
    if not source:
        problems.append("PipeWire reports no audio input — is a microphone plugged in?")
    elif source.endswith(".monitor"):
        problems.append(
            f"the default input is {source}, which is a loopback of speaker "
            "output, not a microphone — plug one in, or set `device` in config")
    return problems


def run(config: Config) -> int:
    """Entry point used by `omarchy-voice run`.

    Not having an API key yet is a normal state, not a crash. Installing the
    add-on before pasting a key used to leave a service that failed, restarted,
    failed, and hit the start limit — which is what a fresh install looked like
    to anyone who had not read the README first. Now it says what is missing,
    puts that on the bar where it will be seen, and exits 0 so systemd lets it
    stay down instead of retrying.
    """
    problems = check_ready(config)
    # A missing microphone is a warning, not a hard start failure: the user can
    # still type at it with `listen say`.
    soft = ("audio input", "loopback")
    hard = [p for p in problems if not any(s in p for s in soft)]
    unconfigured = [p for p in hard if config.api_key_env in p]
    if unconfigured:
        note = f"{config.api_key_env} is not set — put it in {ENV_FILE}"
        print(note)
        print("then: systemctl --user restart omarchy-voice")
        Feedback(config).state("unconfigured", note)
        return 0
    if hard:
        for problem in hard:
            print(f"cannot start realtime engine: {problem}")
        return 1
    for problem in problems:
        print(f"warning: {problem}")
    try:
        return _run_until_done(RealtimeSession(config))
    except RealtimeUnavailable as exc:
        print(f"cannot start realtime engine: {exc}")
        return 1
    except KeyboardInterrupt:
        return 0


def _run_until_done(session: RealtimeSession) -> int:
    """asyncio.run, but a stuck worker thread cannot wedge the exit.

    Tool calls run through `asyncio.to_thread`, which uses the loop's default
    executor, and `asyncio.run` waits for that executor to drain before it
    returns. A tool still blocked on a subprocess therefore kept the process
    alive after the session had ended and its control socket was gone: `ps`
    showed a healthy daemon, `omarchy-voice status` said no daemon is running,
    and systemd — seeing a process that had not exited — never restarted it.

    Owning the executor lets us abandon it instead of waiting on it. The threads
    are daemon threads doing bounded subprocess work; the process is exiting
    either way.
    """
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="omarchy-voice")
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.set_default_executor(executor)
        return loop.run_until_complete(session.run())
    finally:
        try:
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(None)
        # wait=False is the point: do not block on a tool that is still running.
        executor.shutdown(wait=False, cancel_futures=True)
