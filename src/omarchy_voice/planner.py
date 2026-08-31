"""One-shot OpenAI planner for `omarchy-voice say`.

The daemon itself is speech-to-speech over the Realtime API. This module is
the typed equivalent: the same tools, the same policy gate, no microphone.
It talks to Chat Completions over HTTPS so a command can be tried without
opening a websocket.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import capabilities
from .config import Config
from .persona import PERSONA
from .tools import TOOL_SCHEMAS, Executor, tools_for

CHAT_URL = "https://api.openai.com/v1/chat/completions"


@dataclass
class Turn:
    """One request and everything that came of it."""
    text: str
    reply: str = ""
    actions: list[str] = field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0
    tokens: dict = field(default_factory=dict)


def to_chat_tools(schemas: list[dict] | None = None) -> list[dict]:
    converted = []
    for schema in schemas if schemas is not None else TOOL_SCHEMAS:
        converted.append({
            "type": "function",
            "function": {
                "name": schema["name"],
                "description": schema["description"],
                "parameters": schema["input_schema"],
            },
        })
    return converted


def _system_prompt() -> str:
    return "\n\n".join([
        PERSONA,
        capabilities.manifest(),
        "# The desktop right now\n\n" + capabilities.live_state(),
    ])


class PlannerUnavailable(RuntimeError):
    """Something the one-shot planner needs is missing."""


class Planner:
    def __init__(self, config: Config, executor: Executor):
        self.config = config
        self.executor = executor

    def think(self, text: str) -> Turn:
        turn = Turn(text=text)
        started = time.monotonic()
        try:
            turn.reply = self._loop(text, turn)
        except PlannerUnavailable as exc:
            turn.error = str(exc)
            turn.reply = "My planner isn't configured yet."
        except Exception as exc:  # a voice tool must not die on one bad turn
            turn.error = f"{type(exc).__name__}: {exc}"
            turn.reply = "Something went wrong with that."
        turn.elapsed = time.monotonic() - started
        return turn

    def _loop(self, text: str, turn: Turn) -> str:
        key = os.environ.get(self.config.api_key_env, "")
        if not key:
            raise PlannerUnavailable(
                f"{self.config.api_key_env} is not set — "
                "put it in ~/.config/omarchy-voice/env")

        messages: list[dict] = [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": text},
        ]
        tools = to_chat_tools(tools_for(self.config))
        reply = ""

        for _ in range(self.config.max_turns):
            data = _chat(messages, tools, self.config, key)
            usage = data.get("usage") or {}
            if usage:
                turn.tokens = {
                    "in": usage.get("prompt_tokens", 0),
                    "out": usage.get("completion_tokens", 0),
                }
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            said = (message.get("content") or "").strip()
            if said:
                reply = said
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                return reply or "Done."

            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            })
            for call in tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError as exc:
                    outcome_text = f"ERROR: could not parse arguments: {exc}"
                else:
                    outcome = self.executor.call(name, args)
                    turn.actions.append(self.executor.describe(name, args))
                    outcome_text = outcome.as_tool_result()
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": outcome_text,
                })
            if self.executor.pending:
                return reply or "That needs confirmation."

        return reply or "Ran out of steps on that one."


def _chat(messages: list[dict], tools: list[dict], config: Config, key: str) -> dict:
    body = json.dumps({
        "model": config.planner_model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
    }).encode()
    request = urllib.request.Request(
        CHAT_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:400]
        raise PlannerUnavailable(f"OpenAI HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise PlannerUnavailable(f"could not reach OpenAI: {exc.reason}") from exc
