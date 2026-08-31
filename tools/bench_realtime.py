#!/usr/bin/env python3
"""Measure realtime-model latency for driving this desktop.

Uses the *production* session.update from realtime.py — same persona, same
capability manifest, same tool schemas — so the numbers describe this app
rather than a toy session.

No microphone needed. Audio input is replaced with a text conversation item,
which exercises everything after the ear: model decision, tool call, and speech
generation. That is where nearly all the latency lives.

Two things are timed, both from `response.create`:
  speak  first audio byte for a plain spoken reply
  tool   the function_call arriving in response.done for "switch to workspace 3"

`tool` is the number that matters for voice control: it is how long until the
desktop actually moves.

  python3 tools/bench_realtime.py [model ...]
"""
from __future__ import annotations

import asyncio, json, os, statistics, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from omarchy_voice import config as cfg, realtime
from omarchy_voice.config import Config

DEFAULT_MODELS = [
    "gpt-realtime-2.1", "gpt-realtime-2.1-mini", "gpt-realtime-2",
    "gpt-realtime-1.5", "gpt-realtime-mini", "gpt-realtime-translate",
]
URL = "wss://api.openai.com/v1/realtime?model={model}"
RUNS = int(os.environ.get("BENCH_RUNS", "3"))
# Opening fresh sockets back to back gets throttled (close code 1013), and a
# throttled run looks exactly like a model that refused to call a tool. Pausing
# between runs took measured tool-call reliability from 2/6 to 4/4 — the earlier
# number was measuring the rate limiter, not the model.
PAUSE = float(os.environ.get("BENCH_PAUSE", "3"))


async def _measure(model: str, config, prompt: str, want_tool: bool) -> tuple[float | None, str]:
    """One measurement on a FRESH connection.

    Fresh every time on purpose. Reusing a conversation made the numbers lie:
    after a few chatty turns a model would answer "switch to workspace 3" with
    speech instead of a tool call, and an unanswered function_call left in the
    history poisoned every later turn. One turn per socket is the only way the
    runs stay independent.
    """
    config.realtime_model = model
    session = realtime.RealtimeSession(config)
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
    try:
        async with realtime._open_socket(URL.format(model=model), headers) as ws:
            update = await session._session_update()
            update["session"]["audio"]["input"]["turn_detection"] = None
            await ws.send(json.dumps(update))
            while True:
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=45))
                if event.get("type") == "session.updated":
                    break
                if event.get("type") == "error":
                    return None, event.get("error", {}).get("message", "")[:44]
            await ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": prompt}]},
            }))
            await ws.send(json.dumps({"type": "response.create"}))
            start = time.monotonic()
            while True:
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=45))
                kind = event.get("type")
                if kind == "error":
                    return None, event.get("error", {}).get("message", "")[:44]
                # The moment we know what to run beats waiting for response.done.
                if want_tool and kind == "response.function_call_arguments.done":
                    return time.monotonic() - start, ""
                if not want_tool and kind == "response.output_audio.delta":
                    return time.monotonic() - start, ""
                if kind == "response.done":
                    outputs = event.get("response", {}).get("output", [])
                    if want_tool:
                        calls = [o for o in outputs if o.get("type") == "function_call"]
                        if calls:
                            return time.monotonic() - start, ""
                        return None, "spoke, no tool call"
                    return None, "no audio (tool-called instead)"
    except Exception as exc:  # noqa: BLE001 - report, never abort the sweep
        return None, f"{type(exc).__name__}: {str(exc)[:32]}"


async def bench(model: str, config) -> dict:
    result = {"model": model, "speak": [], "tool": [], "speak_note": "", "tool_note": ""}
    for prompt, want_tool, key in (
        ("Say hello in one short sentence.", False, "speak"),
        ("Switch to workspace 3.", True, "tool"),
    ):
        for i in range(RUNS):
            if i or key == "tool":
                await asyncio.sleep(PAUSE)
            dt, note = await _measure(model, config, prompt, want_tool)
            if dt is not None:
                result[key].append(dt)
            elif not result[f"{key}_note"]:
                result[f"{key}_note"] = note
    return result


def _fmt(values: list[float]) -> str:
    return f"{statistics.median(values):6.2f}" if values else "     —"


def _count(values: list[float]) -> str:
    return f"{len(values)}/{RUNS}"


async def main() -> int:
    cfg.load_env_file()
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 1
    config = cfg.load(None)
    models = sys.argv[1:] or DEFAULT_MODELS
    print(f"{RUNS} runs each on a fresh connection, median seconds from response.create.")
    print("speak = first audio byte.  tool = function_call_arguments.done.\n")
    print(f"  {'model':24} {'speak':>7} {'ok':>4} {'tool':>7} {'ok':>4}  note")
    print(f"  {'-'*24} {'-'*7} {'-'*4} {'-'*7} {'-'*4}  ----")
    rows = []
    for model in models:
        r = await bench(model, config)
        rows.append(r)
        note = "; ".join(n for n in (r["speak_note"], r["tool_note"]) if n)
        print(f"  {model:24} {_fmt(r['speak']):>7} {_count(r['speak']):>4} "
              f"{_fmt(r['tool']):>7} {_count(r['tool']):>4}  {note[:46]}")
    usable = [r for r in rows if len(r["tool"]) == RUNS]
    if usable:
        best = min(usable, key=lambda r: statistics.median(r["tool"]))
        print(f"\nfastest to act, reliably: {best['model']} "
              f"({statistics.median(best['tool']):.2f}s, {RUNS}/{RUNS})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
