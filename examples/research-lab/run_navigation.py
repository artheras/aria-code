"""Generate/replay three logical-navigation experiments with a host-selected binary."""
import argparse
import hashlib
import json
import platform
from pathlib import Path
import queue
import subprocess
import threading

from aria_code.runtime.research_bundle import audit_bundle
from aria_code.runtime.task_verification import _read_file

CASES = [
    {'name': 'open', 'obstacles': [], 'status': 'completed', 'steps': 4},
    {'name': 'detour', 'obstacles': [[1, 0]], 'status': 'completed', 'steps': 6},
    {'name': 'unreachable', 'obstacles': [[1, 0], [0, 1]], 'status': 'needs_input', 'steps': 0},
]


def experiment(native):
    process = subprocess.Popen([str(native), 'physical', 'agent-jsonl'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    responses = queue.Queue(maxsize=32)
    def reader():
        for line in process.stdout:
            responses.put(line)
    threading.Thread(target=reader, daemon=True).start()
    def call(**params):
        process.stdin.write(json.dumps(params) + '\n')
        process.stdin.flush()
        response = json.loads(responses.get(timeout=5))
        if response.get('ok') is not True:
            raise ValueError('Native simulation request failed')
        return response['result']
    results = []
    try:
        if call(action='health').get('protocol') != 'warehouse-task-agent-v1':
            raise ValueError('Unexpected native simulation protocol')
        for case in CASES:
            task = call(action='create', request_id=case['name'] + '-create',
                        goal='Reach [4,0] in this explicitly synthetic experiment',
                        scenario=dict(width=5, height=4, start=[0, 0], goal=[4, 0], obstacles=case['obstacles']),
                        constraints=dict(max_steps=12, deadline_ms=5000))
            ident = task['task_id']
            task = call(action='start', task_id=ident, request_id=case['name'] + '-start')
            for _ in range(8):
                if task['status'] != 'running':
                    break
                task = call(action='wait', task_id=ident, timeout_ms=1000)
            summary = {'name': case['name'], 'status': task['status'],
                       'verified': task['verified'], 'position': task['world']['position'],
                       'steps': task['world']['metrics']['accepted_steps']}
            summary['passed'] = (summary['status'] == case['status'] and summary['steps'] == case['steps']
                                 and (summary['status'] != 'completed' or
                                      (summary['verified'] is True and summary['position'] == [4, 0])))
            results.append(summary)
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native', type=Path, required=True, help='Trusted installed aria-native selected by the host')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--replay', action='store_true')
    args = parser.parse_args()
    native = args.native.resolve(strict=True)
    fingerprint = hashlib.sha256(native.read_bytes()).hexdigest()
    if args.replay:
        audit = audit_bundle(str(args.out), 'manifest.json')
        if not audit['success'] or json.loads(_read_file(str(args.out), 'inputs.json')) != CASES:
            raise ValueError('Bundle integrity or experiment inputs changed')
        environment = json.loads(_read_file(str(args.out), 'environment.json'))
        if environment['native_sha256'] != fingerprint or _read_file(str(args.out), 'method.py') != (Path(__file__).read_text() + '\n').encode():
            raise ValueError('Simulator or experiment implementation changed')
        expected = json.loads(_read_file(str(args.out), 'results.json'))
        observed = experiment(native)
        passed = observed == expected and all(r['passed'] for r in observed)
        print(json.dumps({'replay_passed': passed, 'real_world_validation': 'not_established', 'results': observed}, indent=2))
        return 0 if passed else 1
    if args.out.exists() and any(args.out.iterdir()):
        parser.error('Output directory must be absent or empty')
    args.out.mkdir(parents=True, exist_ok=True)
    observed = experiment(native)
    files = {'inputs.json': json.dumps(CASES, indent=2), 'results.json': json.dumps(observed, indent=2),
             'environment.json': json.dumps({'native_sha256': fingerprint,
                                             'python': platform.python_version(), 'os': platform.system(),
                                             'machine': platform.machine()}, indent=2),
             'method.py': Path(__file__).read_text(),
             'report.md': '# Logical navigation experiment\n\n'
             'Three fixed synthetic fixtures: direct route, detour, unreachable goal.\n'
             'Expected steps and statuses are regression oracles, not physical measurements.\n'
             'No sensor noise, dynamics, hardware or learned policy is evaluated.\n'
             'Replay uses a host-selected binary; agreement is reproducibility within this simulator, not external validation.\n'
             + '\n| Case | Status | Steps | Oracle passed |\n|---|---|---|---|\n'
             + ''.join(f"| {r['name']} | {r['status']} | {r['steps']} | {r['passed']} |\n" for r in observed)}
    kinds = {'inputs.json': 'synthetic_data', 'results.json': 'simulation', 'method.py': 'code',
             'report.md': 'analysis', 'environment.json': 'design'}
    artifacts = []
    for name, text in files.items():
        data = (text + '\n').encode()
        (args.out / name).write_bytes(data)
        artifacts.append({'id': name, 'path': name, 'kind': kinds[name], 'sha256': hashlib.sha256(data).hexdigest()})
    manifest = {'version': 1,
                'question': 'Does the logical controller meet the three declared fixture oracles?',
                'hypothesis': 'The controller reaches the two reachable synthetic goals and reports needs_input for the blocked start.',
                'artifacts': artifacts,
                'claims': [{'id': 'scope', 'text': 'This package contains a bounded logical-navigation simulation experiment.',
                            'scope': 'simulation', 'evidence': ['results.json']}],
                'limitations': ['Only three fixed synthetic fixtures.', 'No physical or real-world validation.',
                                'Declared evidence kinds are not authenticated provenance.',
                                'A hash inventory does not prove correct reasoning or independent execution.']}
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    audit = audit_bundle(str(args.out), 'manifest.json')
    passed = all(r['passed'] for r in observed)
    print(json.dumps({'experiment_passed': passed, 'bundle_audit': audit, 'results': observed}, indent=2))
    return 0 if passed and audit['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
