import hashlib
import json
from pathlib import Path

import pytest

from aria_code.runtime.research_bundle import audit_bundle


@pytest.fixture
def bundle(tmp_path):
    data = b'{"result":"synthetic"}'
    (tmp_path / 'result.json').write_bytes(data)
    manifest = {'version': 1, 'question': 'Can the example be replayed?',
                'hypothesis': 'The declared synthetic fixture is reproducible.',
                'limitations': ['No hardware or sensor validation.'],
                'artifacts': [{'id': 'result', 'path': 'result.json', 'kind': 'simulation',
                               'sha256': hashlib.sha256(data).hexdigest()}],
                'claims': [{'id': 'claim', 'text': 'An in-silico example exists.',
                            'scope': 'simulation', 'evidence': ['result']}]}
    return tmp_path, manifest


def check(bundle):
    root, manifest = bundle
    (root / 'manifest.json').write_text(json.dumps(manifest))
    return audit_bundle(str(root), 'manifest.json')


def test_integrity_is_not_scientific_validation(bundle):
    result = check(bundle)
    assert result['success'] is True
    assert result['integrity_verified'] is True
    assert result['scientifically_verified'] is None
    assert result['real_world_validation'] == 'not_established'


def test_modified_artifact_fails_inventory(bundle):
    (bundle[0] / 'result.json').write_text('changed')
    result = check(bundle)
    assert result['success'] is False
    assert result['integrity_verified'] is False


def test_simulation_does_not_support_real_world_claim(bundle):
    bundle[1]['claims'][0]['scope'] = 'real_world'
    bundle[1]['scientifically_verified'] = True
    bundle[1]['real_world_validation'] = 'verified'
    result = check(bundle)
    assert result['success'] is False
    assert result['claims'][0]['scope_supported'] is False
    assert result['scientifically_verified'] is None
    assert result['real_world_validation'] == 'not_established'


@pytest.mark.parametrize('kind', ['synthetic_data', 'analysis', 'code', 'public_data', 'design'])
def test_other_evidence_is_not_simulation_execution(bundle, kind):
    bundle[1]['artifacts'][0]['kind'] = kind
    assert check(bundle)['success'] is False


@pytest.mark.parametrize('kind', ['physical', 'measured_hardware', [], None])
def test_unsupported_evidence_kinds_fail(bundle, kind):
    bundle[1]['artifacts'][0]['kind'] = kind
    with pytest.raises(ValueError):
        check(bundle)


@pytest.mark.parametrize('change', ['missing_refs', 'duplicate_artifact', 'duplicate_claim', 'no_limitations', 'bad_digest'])
def test_invalid_contracts_fail(bundle, change):
    manifest = bundle[1]
    if change == 'missing_refs':
        manifest['claims'][0]['evidence'] = ['missing']
    elif change == 'duplicate_artifact':
        manifest['artifacts'] *= 2
    elif change == 'duplicate_claim':
        manifest['claims'] *= 2
    elif change == 'no_limitations':
        manifest['limitations'] = []
    else:
        manifest['artifacts'][0]['sha256'] = 'x' * 64
    with pytest.raises(ValueError):
        check(bundle)


def test_path_escape_is_rejected(bundle):
    bundle[1]['artifacts'][0]['path'] = '../private.json'
    with pytest.raises(ValueError):
        check(bundle)


def test_symlink_evidence_is_rejected(bundle):
    root, manifest = bundle
    (root / 'link.json').symlink_to(root / 'result.json')
    manifest['artifacts'][0]['path'] = 'link.json'
    with pytest.raises((OSError, ValueError)):
        check(bundle)


def test_audit_never_executes_evidence(bundle, monkeypatch):
    import subprocess
    def forbidden(*args, **kwargs):
        pytest.fail('Inventory must never execute a command')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    assert check(bundle)['success'] is True


@pytest.mark.parametrize('role', ['planner', 'researcher', 'implementer', 'data_analyst', 'reviewer', 'writer'])
def test_example_workflow_loads_through_existing_scoped_context(role):
    from aria_code.runtime.task_context import load_context
    root = Path(__file__).resolve().parents[1] / 'examples/research-lab'
    context = load_context(str(root), role, 'research-session', [])
    assert context.sources[0]['id'] == 'research_protocol'
    assert 'scientifically_verified remains null' in context.prompt_block
