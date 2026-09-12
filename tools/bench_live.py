#!/usr/bin/env python3
"""Opt-in Live API smoke test. Sends silence and runs only a harmless probe tool.

Uses paid API time (up to 45 seconds, plus a small backend response). Never
captures a microphone, plays audio, reads the desktop, or executes desktop tools.
"""
import argparse
import asyncio
import base64
import contextlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from omarchy_voice import config, realtime
from omarchy_voice.live import LIVE_URL


async def probe(count=1, parallel=False):
    config.load_env_file()
    settings = config.load()
    key = os.environ.get(settings.api_key_env)
    if not key:
        print(f"No {settings.api_key_env} configured; no API request made.")
        return 2
    session = {"model": settings.live_model, "store": False,
               "instructions": "Wait quietly. Report only the verified backend result in one sentence.",
               "audio": {"format": {"type": "audio/pcm", "rate": 24000},
                         "output": {"voice": settings.live_voice}},
               "delegation": {"type": "responses", "responses": {
                   "model": settings.live_backend_model, "instructions": f"Call probe for each index from 1 to {count}, exactly once each. All calls are independent; request them together when parallel calls are allowed. Then report success briefly.",
                   "max_output_tokens": 256, "reasoning": {"effort": "low"},
                   "service_tier": "default", "parallel_tool_calls": parallel,
                   "tools": [{"type": "function", "name": "probe", "description": "Harmless connection probe.",
                              "parameters": {"type": "object", "properties": {"index": {"type": "integer", "enum": list(range(1, count + 1))}}, "required": ["index"], "additionalProperties": False},
                              "strict": False}]}}}
    calls = []
    answered = set()
    responses = 0
    widest_batch = 0
    started = time.monotonic()
    closing = False
    sender = None
    async with realtime._open_socket(LIVE_URL, {"Authorization": f"Bearer {key}"}) as ws:
        async def send(event):
            await ws.send(json.dumps(event))
        async def silence():
            while True:
                await send({"type": "session.input_audio.append", "audio": base64.b64encode(bytes(4800)).decode()})
                await asyncio.sleep(0.1)
        await send({"type": "session.start", "session": session})
        try:
            async with asyncio.timeout(40):
                async for raw in ws:
                    event = json.loads(raw)
                    kind = event.get("type")
                    if kind == "session.started":
                        print("Live session started.")
                        sender = asyncio.create_task(silence())
                        await send({"type": "response.item.create", "item": {
                            "type": "message", "role": "user", "content": [{"type": "input_text", "text": f"Run all {count} independent connection probes."}]}})
                        await send({"type": "response.create"})
                    elif kind == "response.event":
                        nested = event["event"]
                        if nested["type"] == "response.output_item.done" and nested.get("item", {}).get("type") == "function_call":
                            calls.append(nested["item"])
                        elif nested["type"] == "response.completed":
                            responses += 1
                            widest_batch = max(widest_batch, len(calls))
                            if calls:
                                for call in calls:
                                    if call["name"] != "probe":
                                        raise RuntimeError("Unexpected tool; refused")
                                    index = json.loads(call["arguments"])["index"]
                                    if index not in range(1, count + 1) or index in answered:
                                        raise RuntimeError("Unexpected or repeated probe index")
                                    answered.add(index)
                                    await send({"type": "response.item.create", "item": {
                                        "type": "function_call_output", "call_id": call["call_id"], "output": f"Probe {index} succeeded."}})
                                calls.clear()
                                await send({"type": "response.create"})
                            else:
                                closing = True
                                if sender:
                                    sender.cancel()
                                await send({"type": "session.close"})
                            print("Backend usage:", json.dumps(nested.get("response", {}).get("usage")))
                        elif nested["type"] in ("response.failed", "response.incomplete"):
                            raise RuntimeError(json.dumps(nested.get("response", {}).get("error")))
                    elif kind == "error":
                        raise RuntimeError(json.dumps(event.get("error")))
                    elif kind == "session.closed":
                        print("Final Live usage:", json.dumps(event.get("usage")))
                        passed = len(answered) == count
                        print("Function round trip:", "passed" if passed else "not completed")
                        print("Timing:", json.dumps({"parallel": parallel, "calls": len(answered), "backend_responses": responses, "widest_batch": widest_batch, "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}))
                        return 0 if passed else 1
        finally:
            if sender:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            if not closing:
                with contextlib.suppress(Exception):
                    await send({"type": "session.close"})
                    async with asyncio.timeout(5):
                        async for raw in ws:
                            event = json.loads(raw)
                            if event.get("type") == "session.closed":
                                print("Final Live usage:", json.dumps(event.get("usage")))
                                break
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", action="store_true", help="authorize the paid API smoke test")
    parser.add_argument("--calls", type=int, choices=range(1, 5), default=1)
    parser.add_argument("--parallel", action="store_true", help="request independent calls in one backend response")
    args = parser.parse_args()
    if not args.connect:
        parser.print_help()
        sys.exit(0)
    try:
        sys.exit(asyncio.run(probe(args.calls, args.parallel)))
    except Exception as exc:
        print(f"Live probe failed: {type(exc).__name__}: {exc}")
        sys.exit(1)
