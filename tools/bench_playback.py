#!/usr/bin/env python3
"""No API spend: compare old/new players through a temporary PipeWire null sink."""
import array
import asyncio
import contextlib
import json
import math
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from omarchy_voice.playback import LiveSpeaker
from omarchy_voice.realtime import Speaker


async def probe(player, name, sink):
    capture = await asyncio.create_subprocess_exec(
        'parec', '--device', sink + '.monitor', '--latency-msec=20', '--process-time-msec=20', '--raw', '--format=s16le', '--rate=24000', '--channels=1',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    reader = asyncio.create_task(capture.stdout.read())
    await asyncio.sleep(.25)
    tone = array.array('h', [round(7000 * math.sin(2 * math.pi * 440 * i / 24000)) for i in range(72000)]).tobytes()
    started = time.monotonic()
    for packet in range(30):
        arrival = packet * .1 + (.08 if packet in (5, 14, 23) else 0)
        await asyncio.sleep(max(0, started + arrival - time.monotonic()))
        await player.write(tone[packet * 4800:(packet + 1) * 4800])
    await asyncio.sleep(.8)
    await player.close()
    capture.terminate()
    await capture.wait()
    raw = await reader
    samples = array.array('h'); samples.frombytes(raw[:len(raw) // 2 * 2])
    # 10 ms RMS windows: detect inserted silence within a sustained 3 s tone.
    levels = [math.sqrt(sum(x*x for x in samples[i:i+240]) / max(1,len(samples[i:i+240])))
              for i in range(0, len(samples), 240)]
    active = [i for i,v in enumerate(levels) if v > 1000]
    middle = levels[active[0]:active[-1]+1] if active else []
    gaps, run = [], 0
    for value in middle + [5000]:
        if value < 1000: run += 1
        elif run: gaps.append(run * 10); run=0
    return {'player': name, 'captured_ms': round(len(samples)/24),
            'tone_detected': bool(active), 'internal_gap_ms': sum(gaps), 'largest_gap_ms': max(gaps, default=0),
            'tone_span_ms': len(middle)*10}


async def main():
    sink = 'oma_audio_qa_' + str(__import__('os').getpid())
    module = subprocess.check_output(['pactl','load-module','module-null-sink',f'sink_name={sink}',
                                     'rate=24000','channels=1'], text=True).strip()
    stats=[]
    try:
        old = Speaker(24000)
        async def ensure():
            if old._proc is None:
                old._proc = await asyncio.create_subprocess_exec('pw-cat','--playback','--raw','--rate','24000',
                    '--channels','1','--format','s16','--target',sink,'-', stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            return old._proc
        old._ensure=ensure
        results=[await probe(old,'previous',sink)]
        new=LiveSpeaker(24000,report=lambda event,**v:stats.append({'event':event,**v}),target=sink)
        results.append(await probe(new,'buffered',sink))
        result={'results':results,'playback_events':stats}
        dest=Path(__file__).resolve().parent.parent/'benchmarks'/'playback-comparison.json'
        dest.write_text(json.dumps(result,indent=2));dest.chmod(0o600)
        print(json.dumps(result))
    finally:
        subprocess.run(['pactl','unload-module',module],check=True)

if __name__=='__main__':asyncio.run(main())
