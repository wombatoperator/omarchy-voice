#!/usr/bin/env python3
"""Opt-in synthetic Jev/OpenAI decision benchmark; never executes desktop actions."""
import argparse
import http.client
import json
import os
from pathlib import Path
import random
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from omarchy_voice import navigation as nav
from eval_navigation import Client, distribution, output_file
from navigation_cases import INVENTORY, cases


def read_key(path, name):
    key = os.environ.get(name)
    if not key and path:
        for line in path.read_text().splitlines():
            field, sep, value = line.strip().removeprefix('export ').partition('=')
            if sep and field.strip() == name:
                key = value.strip().strip('\"').strip("'")
    if not key:
        raise ValueError('Required provider key is missing')
    return key


def openai_payload(request, inventory, model, representation='slots'):
    body = (nav.candidate_payload(request, inventory) if representation == 'candidates'
            else nav.payload(request, inventory))
    properties = {name: {'type': 'string', 'enum': list(question['criteria'])}
                  for name, question in body['questions'].items() if question['type'] == 'choice'}
    return {'model': model, 'store': False, 'reasoning': {'effort': 'low'},
            'max_output_tokens': 1024, 'service_tier': 'default',
            'instructions': nav.INSTRUCTIONS + ' Return fallback when the supported question is false. '
                            'Select the action and every slot; irrelevant slots may be none.',
            'input': json.dumps({'state': body['state'], 'questions': body['questions']}),
            'tools': [{'type': 'function', 'name': 'select_navigation', 'strict': True,
                       'parameters': {'type': 'object', 'properties': properties,
                                      'required': list(properties), 'additionalProperties': False}}],
            'tool_choice': {'type': 'function', 'name': 'select_navigation'}}


class OpenAIClient(Client):
    def evaluate(self, body):
        cold = self.connection is None
        if cold:
            self.connection = http.client.HTTPSConnection('api.openai.com', timeout=self.timeout)
        started = time.perf_counter()
        try:
            self.connection.request('POST', '/v1/responses', json.dumps(body), {
                'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
            response = self.connection.getresponse()
            data = response.read(1_000_001)
            if response.status != 200 or len(data) > 1_000_000:
                raise RuntimeError('Provider request failed; body withheld')
            result = json.loads(data)
            elapsed = (time.perf_counter() - started) * 1000
            if response.will_close:
                self.close()
            return result, round(elapsed, 2), cold
        except Exception:
            self.close()
            raise


def openai_decision(response, inventory, representation='slots'):
    calls = [x for x in response.get('output', []) if x.get('type') == 'function_call']
    if len(calls) != 1 or calls[0].get('name') != 'select_navigation':
        raise ValueError('Missing complete selection')
    selected = json.loads(calls[0]['arguments'])
    body = (nav.candidate_payload('validation', inventory) if representation == 'candidates'
            else nav.payload('validation', inventory))
    questions = {k: v for k, v in body['questions'].items() if v['type'] == 'choice'}
    if set(selected) != set(questions) or any(selected[k] not in q['criteria'] for k, q in questions.items()):
        raise ValueError('Unknown selection')
    if representation == 'candidates':
        key = selected['selection']
        selected = nav.candidates(inventory)[key][0] if key != nav.FALLBACK else {'action': nav.FALLBACK}
    call = nav.compile_selection(selected, inventory)
    return {'valid': True, 'accepted': call is not None, 'selection': selected, 'call': call}


def matches(selected, expected):
    return all(selected.get(k) == v for k, v in expected.items())


def summary(rows):
    result = {}
    for provider in sorted({r['provider'] for r in rows}):
        subset = [r for r in rows if r['provider'] == provider]
        accepted = [r for r in subset if r['accepted']]
        positive = [r for r in subset if r['eligible']]
        result[provider] = {
            'calls': len(subset), 'errors': sum(not r['valid'] for r in subset),
            'correct': sum(r['correct'] for r in subset),
            'raw_correct': sum(r['raw_correct'] for r in subset),
            'eligible': len(positive), 'accepted': len(accepted),
            'eligible_accepted_correctly': sum(r['accepted'] and r['correct'] for r in positive),
            'wrong_accepted': sum(not r['correct'] for r in accepted),
            'latency': distribution([r['duration_ms'] for r in subset]),
            'warm_latency': distribution([r['duration_ms'] for r in subset if not r['cold']]),
            'input_tokens': sum(r['usage'].get('input_tokens', 0) for r in subset),
            'output_tokens': sum(r['usage'].get('output_tokens', 0) for r in subset),
        }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connect', action='store_true')
    parser.add_argument('--provider', choices=('jev', 'openai', 'both'), default='jev')
    parser.add_argument('--representation', choices=('slots', 'candidates'), default='slots')
    parser.add_argument('--split', choices=('development', 'held_out', 'confirmation', 'all'), default='development')
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--threshold', type=float, default=.9)
    parser.add_argument('--jev-model', default='jev-1.13.0')
    parser.add_argument('--openai-model', default='gpt-5.6-terra')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--jev-key-env', default='JEV_API_KEY')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    providers = ['jev', 'openai'] if args.provider == 'both' else [args.provider]
    selected_cases = [c for c in cases() if args.split == 'all' or c['split'] == args.split][:args.limit]
    try:
        if not 1 <= args.limit <= 100 or not 1 <= args.repeat <= 3:
            raise ValueError('Invalid call limits')
        nav._probability(args.threshold)
        count = len(selected_cases) * args.repeat * len(providers)
        if not 0 < count <= 200:
            raise ValueError('Limited to 200 paid requests per invocation')
        if not args.connect:
            print(json.dumps({'actions': len(nav.ACTIONS), 'cases': len(selected_cases),
                              'planned_calls': count, 'network': False}))
            return 0
        if not args.output:
            raise ValueError('A private output path is required')
        clients = {}
        # Read only explicitly selected credentials; never source an environment file.
        for provider in providers:
            name = args.jev_key_env if provider == 'jev' else 'OPENAI_API_KEY'
            cls = Client if provider == 'jev' else OpenAIClient
            clients[provider] = cls(read_key(args.env_file, name), 15)
        rows = []
        rng = random.Random(29)
        with output_file(args.output) as output:
            try:
                for repeat in range(args.repeat):
                    ordered = list(selected_cases)
                    rng.shuffle(ordered)
                    for case in ordered:
                        order = list(providers)
                        rng.shuffle(order)
                        for provider in order:
                            started = time.perf_counter()
                            try:
                                payload_fn = nav.candidate_payload if args.representation == 'candidates' else nav.payload
                                body = (payload_fn(case['request'], INVENTORY, args.jev_model) if provider == 'jev'
                                        else openai_payload(case['request'], INVENTORY, args.openai_model, args.representation))
                                if len(json.dumps(body).encode()) > 60000:
                                    raise ValueError('Request exceeds trial input bound')
                                response, elapsed, cold = clients[provider].evaluate(body)
                                decide_fn = nav.decide_candidate if args.representation == 'candidates' else nav.decide
                                decision = (decide_fn(response, INVENTORY, args.threshold) if provider == 'jev'
                                            else openai_decision(response, INVENTORY, args.representation))
                                if not decision['valid']:
                                    raise ValueError('Malformed provider decision')
                                usage = response.get('usage') or {}
                                usage = {k: v for k, v in usage.items() if k in ('input_tokens', 'output_tokens')
                                         and type(v) is int and v >= 0}
                            except Exception as exc:
                                decision = {'valid': False, 'accepted': False, 'selection': {'action': nav.FALLBACK}}
                                elapsed, cold, usage = round((time.perf_counter() - started) * 1000, 2), True, {}
                                print('Provider failure: ' + type(exc).__name__ + '; details withheld.', file=sys.stderr)
                            effective = decision['selection'] if decision['accepted'] else {'action': nav.FALLBACK}
                            rows.append({'case': case['id'], 'split': case['split'], 'provider': provider,
                                         'repeat': repeat, 'duration_ms': elapsed, 'cold': cold, 'usage': usage,
                                         'valid': decision['valid'], 'accepted': decision['accepted'],
                                         'eligible': case['expected']['action'] != nav.FALLBACK,
                                         'correct': decision['valid'] and matches(effective, case['expected']),
                                         'raw_correct': decision['valid'] and matches(decision['selection'], case['expected']),
                                         'selection': decision['selection'], 'confidence': decision.get('confidence')})
                            if len(rows) % 10 == 0:
                                print(f'Completed {len(rows)}/{count} decisions', file=sys.stderr, flush=True)
                            if not decision['valid']:
                                raise RuntimeError('Stopping after provider failure')
            except RuntimeError:
                pass
            finally:
                for client in clients.values():
                    client.close()
                totals = summary(rows)
                json.dump({'summary': totals, 'rows': rows, 'threshold': args.threshold,
                           'models': {'jev': args.jev_model, 'openai': args.openai_model},
                           'representation': args.representation,
                           'note': 'Synthetic decision-only trial. No speech, desktop execution, or fallback time.'}, output, indent=2)
            print(json.dumps(totals, indent=2))
            return int(any(not row['valid'] for row in rows))
    except (OSError, ValueError, TypeError):
        print('Trial setup failed; check keys, limits and private output path.')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
