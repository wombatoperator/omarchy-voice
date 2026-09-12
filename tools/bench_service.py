#!/usr/bin/env python3
"""Opt-in, budgeted smoke test through the installed service's control socket.

Sends real desktop context and health readings to OpenAI. The microphone must
already be off. The daemon plays its reply; this script closes the paid session.
"""
import argparse
import contextlib
import json
import os
import time
from pathlib import Path

from bench_desktop import Budget, CASES, ROOT, price, save
from omarchy_voice import config
from omarchy_voice.session import send_control


def run():
    cfg = config.load()
    state = Path("/run/user") / str(os.getuid()) / "omarchy-voice/state.json"
    if json.loads(state.read_text()).get("status") != "idle":
        raise RuntimeError("Installed service is busy; do not interrupt the user")
    log = config.STATE_DIR / "session.log"
    offset = log.stat().st_size
    deadline = time.monotonic() + 5
    while not config.SOCKET_PATH.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("Service control socket is not ready; no budget reserved")
        time.sleep(.1)
    budget = Budget()
    entry = budget.begin({"id": time.strftime("%Y%m%d-%H%M%S") + "-installed-service",
                          "source": "installed", "case": "health", "input": "text",
                          "variant": "installed", "model": cfg.live_backend_model,
                          "tier": cfg.live_service_tier, "reasoning": cfg.live_reasoning_effort})
    folder = ROOT / "benchmarks" / entry["id"]
    started = time.monotonic()
    previous = ""
    last_change = started
    lines = []
    accepted = False
    try:
        if send_control("say " + CASES["health"]) != "queued":
            raise RuntimeError("Service did not accept the benchmark")
        accepted = True
        while time.monotonic() - started < 40:
            with log.open() as stream:
                stream.seek(offset)
                text = stream.read()
            lines = text.splitlines()
            if text != previous:
                previous, last_change = text, time.monotonic()
            finished = any('"calls":0' in line and '"event":"backend_finished"' in line for line in lines)
            responses = sum('"event":"backend_started"' in line for line in lines)
            if responses >= 6 or (finished and time.monotonic() - last_change > 3):
                break
            if "final=true" in text:
                break
            time.sleep(.1)
    finally:
        # Closing the service session also guarantees the microphone remains off.
        with contextlib.suppress(ConnectionError):
            send_control("stop")
        deadline = time.monotonic() + 17
        while accepted and time.monotonic() < deadline:
            with log.open() as stream:
                stream.seek(offset)
                lines = stream.read().splitlines()
            if any("usage   live" in line and "final=true" in line for line in lines):
                break
            time.sleep(.1)
        usage = [json.loads(line[line.index("{"):]) for line in lines if "usage   backend" in line]
        perf = [json.loads(line.split("perf    ", 1)[1]) for line in lines if "perf    " in line]
        final = next((line for line in lines if "usage   live" in line and "final=true" in line), "")
        seconds = float(final.split("seconds=", 1)[1].split()[0]) if final else 0
        charged = (sum(price(u, cfg.live_backend_model) for u in usage) *
                   (2 if cfg.live_service_tier == "priority" else 1) + seconds * .05 / 60)
        tools = [event for event in perf if event["event"] == "tool_finished"]
        result = {"passed": bool(final) and len(tools) == 4 and
                  all(t["tool"] == "system_query" and t["status"] == "completed" for t in tools),
                  "final_close": bool(final), "voice_seconds": seconds,
                  "charged_estimate": (round(charged, 6) if final and usage else entry["reserve"]) if accepted else 0,
                  "backend_responses": len(usage), "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                  "parallel_calls": sum(t.get("parallel", False) for t in tools)}
        save(folder / "detail.json", {"metrics": result, "events": perf, "usage": usage, "log": lines})
        budget.finish(entry, result)
        budget.close()
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", action="store_true")
    if not parser.parse_args().connect:
        parser.error("--connect is required for the paid installed-service test")
    run()
