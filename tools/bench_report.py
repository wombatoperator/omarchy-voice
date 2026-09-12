#!/usr/bin/env python3
"""Summarize the local experiment ledger without making any API requests."""
import json
from collections import defaultdict
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent


def report():
    ledger = json.loads((ROOT / "benchmarks/budget.json").read_text())
    completed = [r for r in ledger["runs"] if r["status"] == "complete"]
    charged = sum(r.get("charged_estimate", r.get("reserve", 0)) for r in ledger["runs"])
    rows = [r for r in completed if r.get("completion_ms") is not None]
    lines = [
        f"Accounted experiment spend: ${charged:.4f} / ${ledger['cap_usd']:.2f}.",
        f"Completed paid Live sessions: {sum('source' in r for r in completed)}; "
        f"speech fixtures: {sum(r.get('model') == 'gpt-4o-mini-tts' for r in completed)}.",
        "",
        "Backend completion excludes connection setup, spoken playback and idle shutdown. "
        "Desktop runs use real tools; synthetic runs fabricate tool results.",
        "",
        "| Data | Case/input | Model/prompt | Reasoning/tier | n | Median first tool | Median completion |",
        "| --- | --- | --- | --- | ---: | ---: | ---: |",
    ]
    groups = defaultdict(list)
    for row in rows:
        key = (row["source"], row["case"], row.get("input", "text"),
               row["model"], row["variant"], row.get("reasoning", "low"),
               row.get("tier", "default"))
        groups[key].append(row)
    for key, values in sorted(groups.items()):
        source, case, mode, model, prompt, reasoning, tier = key
        first = [v["first_action_ms"] for v in values if v.get("first_action_ms") is not None]
        latency = median(v["completion_ms"] for v in values)
        first_text = f"{median(first) / 1000:.3f} s" if first else "—"
        lines.append(f"| {source} | {case}/{mode} | {model}/{prompt} | {reasoning}/{tier} "
                     f"| {len(values)} | {first_text} | {latency / 1000:.3f} s |")
    lines += ["", "Failures and uncertain outcomes require inspecting the owner-only detail logs. "
              "A model finishing its response does not establish task success. "
              "Early app/news checks were revised to wait for settled browser identities; "
              "do not treat their original Boolean scores as comparable to reruns."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(report(), end="")
