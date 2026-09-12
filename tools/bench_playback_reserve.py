#!/usr/bin/env python3
"""Free deterministic playback stress: quiet periods, clock skew and jitter.

Measures gaps inside known speech bursts, not intentional silence. No devices,
network, microphone or API calls. Compare with --baseline <git revision>.
"""
import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from omarchy_voice.playback import LiveSpeaker


async def measure(cls, label, skew=1.02):
    speaker = cls(24000)
    speaker._pump = mock.Mock(done=lambda: False)
    # Three 3s speech bursts separated by 5s quiet; each burst has its own
    # PCM value, so every spoken sample and any internal gap can be measured.
    packets = []
    for packet in range(240):
        burst = packet // 80 + 1 if packet % 80 >= 50 else 0
        pcm = int(burst * 4096).to_bytes(2, 'little') * 2400
        arrival = packet * .1 * skew + (.08 if packet % 80 == 65 else 0)
        packets.append((arrival, pcm, burst))
    output = bytearray()
    index = 0
    with mock.patch('omarchy_voice.playback.time.monotonic') as clock:
        # Baseline modules also import the shared time module.
        for tick in range(1300):
            now = tick * .02
            clock.return_value = now
            while index < len(packets) and packets[index][0] <= now + 1e-9:
                await speaker.write(packets[index][1])
                index += 1
            pcm = speaker._next_frame(now)
            if any(pcm):
                speaker._audible_until = now + .06
            output.extend(pcm)
    samples = memoryview(output).cast('h')
    bursts = []
    for burst in (1, 2, 3):
        positions = [i for i, value in enumerate(samples) if value == burst * 4096]
        source_at = next(at for at, _, number in packets if number == burst)
        bursts.append({'burst': burst, 'samples_preserved': len(positions) == 72000,
                       'internal_gap_ms': round((positions[-1] - positions[0] + 1 - len(positions)) / 24, 1),
                       'onset_after_first_packet_ms': round(positions[0] / 24 - source_at * 1000, 1)})
    speaker._pump = None
    await speaker.close()
    return {'player': label, 'packet_pacing_ratio': skew, 'bursts': bursts}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default='HEAD')
    args = parser.parse_args()
    source = subprocess.check_output(['git', 'show', args.baseline + ':src/omarchy_voice/playback.py'])
    with tempfile.TemporaryDirectory(prefix='oma-buffer-bench-') as directory:
        path = Path(directory) / 'baseline.py'
        path.write_bytes(source)
        spec = importlib.util.spec_from_file_location('omarchy_voice.playback_baseline', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        results = [await measure(cls, label, skew) for skew in (1, 1.02)
                   for cls, label in ((module.LiveSpeaker, args.baseline), (LiveSpeaker, 'candidate'))]
    dest = Path(__file__).resolve().parent.parent / 'benchmarks/playback-reserve.json'
    dest.write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))

if __name__ == '__main__':
    asyncio.run(main())
