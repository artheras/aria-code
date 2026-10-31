import asyncio
import os
from pathlib import Path
import pytest

from aria_code.runtime.acceptance import AcceptanceGate


def gate_for(root, runner=None, path='app.py'):
    gate = AcceptanceGate(root=root, commands=['approved-check'],
                          runner=runner or (lambda _: {'exit_code': 0}))
    gate.record_tool('edit_file', {'success': True, 'path': path, 'applied': True})
    return gate


def test_changed_bytes_invalidate_previous_pass_without_running_commands(tmp_path):
    path = tmp_path / 'app.py'
    path.write_text('original')
    calls = []
    gate = gate_for(tmp_path, lambda command: calls.append(command) or {'exit_code': 0})
    report = asyncio.run(gate.run())
    assert report.passed and gate.summary()['verified'] is True
    path.write_text('external edit')
    view = gate.summary()
    assert view['verified'] is None and view['revision_freshness'] == 'stale'
    assert calls == ['approved-check']
    assert report.summary()['checks'][0]['passed'] is True  # historical result retained


def test_content_identity_not_modification_time(tmp_path):
    path = tmp_path / 'app.py'
    path.write_text('same')
    gate = gate_for(tmp_path)
    asyncio.run(gate.run())
    os.utime(path, (1, 1))
    assert gate.summary()['verified'] is True


def test_changes_during_checks_are_unverified(tmp_path):
    path = tmp_path / 'app.py'
    path.write_text('before')
    def runner(_):
        path.write_text('after')
        return {'exit_code': 0}
    gate = gate_for(tmp_path, runner)
    report = asyncio.run(gate.run())
    assert not report.passed
    assert report.revision_status == 'changed_during_checks'
    assert gate.summary()['verified'] is None
    assert report.failures == ()  # not represented as a code assertion failure
    assert '不要覆盖用户修改' in report.repair_directive()


def test_deletion_invalidates_previous_pass(tmp_path):
    path = tmp_path / 'app.py'
    path.write_text('before')
    gate = gate_for(tmp_path)
    asyncio.run(gate.run())
    path.unlink()
    assert gate.summary()['verified'] is None


def test_absence_can_be_bound_for_deleted_files(tmp_path):
    gate = gate_for(tmp_path)
    asyncio.run(gate.run())
    assert gate.summary()['verified'] is True
    (tmp_path / 'app.py').write_text('recreated')
    assert gate.summary()['verified'] is None


def test_symlink_and_outside_path_do_not_get_verified(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + '-outside.txt')
    outside.write_text('private bytes')
    try:
        (tmp_path / 'app.py').symlink_to(outside)
        for path in ['app.py', str(outside), '../outside.txt']:
            gate = gate_for(tmp_path, path=path)
            asyncio.run(gate.run())
            summary = gate.summary()
            assert summary['verified'] is None
            assert summary['revision_freshness'] == 'unavailable'
            assert 'private bytes' not in str(summary)
    finally:
        outside.unlink()


def test_unavailable_revision_does_not_hide_a_real_failed_check(tmp_path):
    gate = gate_for(tmp_path, lambda _: {'exit_code': 1, 'stderr': 'assertion failed'}, path='../outside.txt')
    report = asyncio.run(gate.run())
    assert report.failures
    assert gate.summary()['verified'] is False


def test_oversized_artifact_is_unverified(tmp_path):
    (tmp_path / 'app.py').write_bytes(b'x' * 1048577)
    gate = gate_for(tmp_path)
    asyncio.run(gate.run())
    assert gate.summary()['verified'] is None


def test_rechecking_current_content_restores_verification(tmp_path):
    path = tmp_path / 'app.py'
    path.write_text('one')
    gate = gate_for(tmp_path)
    asyncio.run(gate.run())
    path.write_text('two')
    gate.record_tool('edit_file', {'success': True, 'path': str(path), 'applied': True})
    asyncio.run(gate.run())
    assert gate.summary()['verified'] is True


def test_read_only_turn_does_not_read_revisions_or_run(tmp_path, monkeypatch):
    gate = AcceptanceGate(root=tmp_path, runner=lambda _: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(gate, '_revision', lambda _: (_ for _ in ()).throw(AssertionError()))
    assert asyncio.run(gate.run()) is None
    assert gate.summary()['verified'] is None


def test_host_can_bind_dependency_configuration(tmp_path):
    (tmp_path / 'app.py').write_text('code')
    config = tmp_path / 'settings.json'
    config.write_text('{}')
    gate = AcceptanceGate(root=tmp_path, commands=['check'], runner=lambda _: {'exit_code': 0},
                          revision_inputs=['settings.json'])
    gate.record_tool('edit_file', {'success': True, 'path': 'app.py', 'applied': True})
    asyncio.run(gate.run())
    assert gate.summary()['verified'] is True
    config.write_text('{"changed":true}')
    assert gate.summary()['verified'] is None


def test_actual_command_pass_then_file_change_and_failed_recheck(tmp_path):
    import subprocess
    import sys
    source = tmp_path / 'app.py'
    source.write_text('1')
    (tmp_path / 'check.py').write_text("from pathlib import Path\nassert Path('app.py').read_text() == '1'\n")
    def runner(_):
        result = subprocess.run([sys.executable, 'check.py'], cwd=tmp_path,
                                capture_output=True, text=True, timeout=10)
        return {'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
    gate = gate_for(tmp_path, runner)
    asyncio.run(gate.run())
    assert gate.summary()['verified'] is True
    source.write_text('2')
    assert gate.summary()['verified'] is None
    gate.record_tool('edit_file', {'success': True, 'path': 'app.py', 'applied': True})
    report = asyncio.run(gate.run())
    assert report.failures and gate.summary()['verified'] is False


@pytest.mark.parametrize('inputs', ['settings.json', [None], [''], ['x' * 513], ['a'] * 65])
def test_invalid_host_revision_inputs_are_rejected(tmp_path, inputs):
    with pytest.raises(ValueError):
        AcceptanceGate(root=tmp_path, runner=lambda _: {'exit_code': 0}, revision_inputs=inputs)


def test_runtime_factory_passes_host_dependency_inputs(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from aria_code.apps.cli.providers import runtime_bridge as bridge
    monkeypatch.setattr(bridge, '_build_baseline', lambda *args: None)
    executor = SimpleNamespace(config={}, local_tools={"run_command": None})
    gate = bridge.build_acceptance_gate(executor, {
        'acceptance_commands': ['check'], 'acceptance_revision_inputs': ['settings.json'],
        '_session_workspace_root': str(tmp_path),
    })
    assert gate.revision_inputs == ('settings.json',)


@pytest.mark.parametrize('role', ['explorer', 'planner', 'implementer', 'reviewer', 'writer'])
def test_engineering_workflow_uses_existing_scoped_context(role):
    from aria_code.runtime.task_context import load_context
    root = Path(__file__).resolve().parents[1] / 'examples/engineering-delivery'
    context = load_context(str(root), role, 'engineering-session', [])
    assert context.sources[0]['id'] == 'engineering_delivery'
    assert 'Missing checks remain unverified' in context.prompt_block


def test_oversized_changed_path_set_is_not_certified(tmp_path):
    gate = gate_for(tmp_path)
    for index in range(65):
        gate.record_tool('edit_file', {'success': True, 'path': f'file-{index}.py', 'applied': True})
    report = asyncio.run(gate.run())
    assert report.revision_status == 'unavailable'
    assert gate.summary()['verified'] is None
