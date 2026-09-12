#!/usr/bin/env python3
"""Budgeted Live speech regression: interrupt a task, exhaust it, then ask a new one.

Real Live/Responses; fixed speech and synthetic tools. No microphone, speaker,
window changes, or network tools. Uses the existing shared $3 ledger.
"""
import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
import types
from unittest import mock

from bench_desktop import Budget, ROOT, QuietSpeaker, price, save
from omarchy_voice import config, feedback, live, realtime


async def run(baseline=False):
    config.load_env_file()
    cfg = config.load(notify=False, dry_run=True)
    cfg.live_max_session_seconds = 45
    cfg.live_max_output_tokens = 512
    cfg.live_service_tier = 'default'
    cfg.max_turns = 1  # Deliberately force the failure seen in the real conversation.
    cfg.live_typed_idle_seconds = 3
    budget = Budget()
    variant = 'before-recovery' if baseline else 'recovery'
    folder = ROOT / 'benchmarks' / (time.strftime('%Y%m%d-%H%M%S') + '-conversation-' + variant)
    folder.mkdir(parents=True, mode=0o700)
    cls = live.LiveSession
    if baseline:
        module = types.ModuleType('omarchy_voice.live_before_recovery')
        module.__package__ = 'omarchy_voice'
        sys.modules[module.__name__] = module
        source = subprocess.check_output(['git', 'show', 'codex/live-before-recovery-2026-09-11:src/omarchy_voice/live.py'], text=True)
        exec(compile(source, 'live_before_recovery.py', 'exec'), module.__dict__)
        cls = module.LiveSession
    entry = budget.begin({'id': folder.name, 'case': 'conversation-recovery', 'variant': variant,
                          'source': 'synthetic', 'input': 'speech', 'model': cfg.live_backend_model,
                          'tier': 'default', 'code_sha256': hashlib.sha256(source.encode() if baseline else (ROOT/'src/omarchy_voice/live.py').read_bytes()).hexdigest()})
    usage, calls, transcript, errors, phases, answers = [], [], [], [], [], []
    first_tool = asyncio.Event()
    started = time.monotonic()
    stage = 0
    stage_ended = 0.0
    stage_started = 0.0
    last_reply = 0.0
    last_final = 0.0
    session = None
    with contextlib.ExitStack() as stack:
        for module, fields in ((config, {'STATE_DIR': folder}), (feedback, {
            'STATE_DIR': folder, 'RUNTIME_DIR': folder, 'LOG_FILE': folder/'session.log',
            'STATE_FILE': folder/'state.json', 'LEVEL_FILE': folder/'level'})):
            for key, value in fields.items(): stack.enter_context(mock.patch.object(module, key, value))
        stack.enter_context(mock.patch.object(live.capabilities, 'live_state', return_value='Synthetic test desktop. No real windows may be changed.'))
        session = cls(cfg)
        session.speaker = QuietSpeaker()
        session.active = True
        session._wanted.set()
        original_event = session._on_event
        async def event(data):
            nonlocal last_reply, last_final
            kind = data.get('type')
            if kind in ('session.input_transcript.delta', 'session.output_transcript.delta'):
                transcript.append({'at': time.monotonic()-started, 'stage': stage, 'type': kind, 'text': data.get('delta')})
                if kind == 'session.output_transcript.delta': last_reply = time.monotonic()
            if kind == 'response.event':
                nested = data['event']; response = nested.get('response', {})
                if nested.get('type') == 'response.output_item.done' and nested.get('item', {}).get('type') == 'message':
                    answers.append({'stage': stage, 'at': time.monotonic()-started, 'item': nested['item']})
                if nested.get('type') == 'response.completed':
                    usage.append(response.get('usage', {}))
                    record = session._responses.get(response.get('id'))
                    if record and not record.calls: last_final = time.monotonic()
                if len(usage) >= 8: session._close_requested.set()
            if kind == 'error': errors.append(data.get('error'))
            await original_event(data)
        session._on_event = event
        async def execute(name, args, **kwargs):
            calls.append({'at': time.monotonic()-started, 'stage': stage, 'tool': name, 'args': args,
                          'parallel': kwargs.get('parallel', False)})
            if not first_tool.is_set():
                first_tool.set()
                await asyncio.sleep(7)  # New speech arrives while an action is running.
            if name == 'omarchy_help': return 'Voice assistant: Super+Shift+V toggles listening.'
            if name == 'system_query':
                return {'time': 'Friday 12:45 PM', 'memory': '8 GiB of 32 GiB used, no swap',
                        'processes': 'CPU idle 94%', 'temperature': 'CPU 35 C',
                        'battery': 'Desktop on mains power'}.get(args.get('topic'), 'ERROR: unknown topic')
            return 'ERROR: Synthetic test only permits omarchy_help and system_query.'
        session._execute = execute
        async def stream():
            nonlocal stage, stage_ended, stage_started
            step = cfg.live_sample_rate // 10 * 2
            speech = (ROOT/'benchmarks/speech/steer.pcm').read_bytes()
            stage_started = time.monotonic()
            phases.append({'stage': stage, 'started': stage_started-started})
            while not session._closing and not session._close_requested.is_set():
                now = time.monotonic()
                if not speech and stage == 0 and first_tool.is_set():
                    stage = 1; speech = (ROOT/'benchmarks/speech/correction.pcm').read_bytes()
                    stage_started = now; phases.append({'stage': stage, 'started': now-started})
                elif (not speech and stage == 1 and now-stage_ended > 3
                      and not session._backend_busy() and not session._steering_text
                      and now-last_reply > 1.5):
                    stage = 2; speech = (ROOT/'benchmarks/speech/health.pcm').read_bytes()
                    stage_started = now; phases.append({'stage': stage, 'started': now-started})
                chunk, speech = speech[:step], speech[step:]
                if chunk:
                    stage_ended = now
                    phases[-1]['speech_ended'] = now-started
                if stage == 2 and not chunk and now-stage_ended > 10:
                    session._close_requested.set()
                await session._send({'type': 'session.input_audio.append',
                                     'audio': base64.b64encode(chunk or bytes(step)).decode()})
                await asyncio.sleep(.1)
        session._audio_loop = stream
        worker = asyncio.create_task(session._tool_worker())
        try:
            await asyncio.wait_for(session._connection({'Authorization': 'Bearer '+os.environ[cfg.api_key_env],
                 'OpenAI-Safety-Identifier': realtime._safety_identifier()}), 62)
        except Exception as exc: errors.append(type(exc).__name__ + ': ' + str(exc))
        finally:
            worker.cancel(); await asyncio.gather(worker, return_exceptions=True)
            charged = sum(price(u, cfg.live_backend_model) for u in usage) + session._usage_seconds*.05/60
            health = [c for c in calls if c['stage'] == 2 and c['tool'] == 'system_query']
            topics = {c['args'].get('topic') for c in health}
            after_speech = (next((c['at'] for c in health), None))
            end = next((p.get('speech_ended') for p in phases if p['stage'] == 2), None)
            verified_answer = bool(health) and any(a['stage'] == 2 and a['at'] >= max(c['at'] for c in health)
                and all(value in json.dumps(a['item']) for value in ('35', '32')) for a in answers)
            result = {'passed': topics >= {'processes','memory','temperature','battery'} and verified_answer and not errors,
                      'final_close': session._usage_final, 'voice_seconds': session._usage_seconds,
                      'charged_estimate': round(charged, 6) if session._usage_final and all(usage) else entry['reserve'],
                      'backend_responses': len(usage), 'health_topics': sorted(topics),
                      'speech_to_health_action_ms': round((after_speech-end)*1000, 1) if after_speech is not None and end is not None else None,
                      'parallel_calls': sum(c['parallel'] for c in health), 'verified_final_answer': verified_answer, 'errors': errors}
            save(folder/'detail.json', {'metrics': result, 'calls': calls, 'transcript': transcript, 'phases': phases, 'answers': answers, 'usage': usage})
            budget.finish(entry, result); budget.close()
            print(json.dumps(result), flush=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connect', action='store_true')
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    if not args.connect: parser.error('--connect required for a paid Live test')
    asyncio.run(run(args.baseline))
