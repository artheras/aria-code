from dataclasses import replace

import pytest

from aria_code.runtime.run_store import RunNotFoundError, RunStore
from aria_code.runtime.scoped_runs import RunGrant, ScopedRuns


@pytest.fixture
def bound(tmp_path):
    store = RunStore(tmp_path / 'runs.sqlite3')
    grant = RunGrant('org-a', 'project-a', 'user-a', 'session-a', str(tmp_path))
    return store, grant, ScopedRuns(store, grant)


def test_persisted_owner_can_read_after_reopen(bound):
    store, grant, api = bound
    run = api.create('Private project input')
    reopened = ScopedRuns(RunStore(store.database_path), grant)
    assert reopened.get(run['run_id']) == run
    assert reopened.list() == [run]
    assert run['execution_state'] == 'queued'
    assert run['verification_state'] == 'unverified'


@pytest.mark.parametrize('field', ['organization', 'project', 'subject', 'session'])
def test_other_scope_cannot_read_or_enumerate(bound, field):
    store, grant, api = bound
    run = api.create('Sensitive input')
    other = ScopedRuns(store, replace(grant, **{field: 'other'}))
    for read in [other.get, other.events]:
        with pytest.raises(RunNotFoundError) as denied:
            read(run['run_id'])
        with pytest.raises(RunNotFoundError) as missing:
            read('missing')
        assert str(denied.value) == str(missing.value)
    assert other.list() == []


def test_workspace_is_part_of_host_binding(bound, tmp_path):
    store, grant, api = bound
    run = api.create('input')
    folder = tmp_path / 'other'
    folder.mkdir()
    other = ScopedRuns(store, replace(grant, workspace=str(folder)))
    with pytest.raises(RunNotFoundError):
        other.get(run['run_id'])


def test_legacy_metadata_does_not_grant_access(bound):
    store, grant, api = bound
    legacy = store.create_run(session_id=grant.session, workspace=grant.workspace,
                              prompt='input', metadata=grant.__dict__)
    with pytest.raises(RunNotFoundError):
        api.get(legacy.run_id)
    assert api.list() == []


def test_scope_filter_applies_before_limit(bound):
    store, grant, api = bound
    own = api.create('own')
    other = ScopedRuns(store, replace(grant, organization='other'))
    for _ in range(3):
        other.create('unrelated')
    assert api.list(limit=1) == [own]


def test_events_are_cursor_ordered_and_omit_payload(bound):
    store, _, api = bound
    run = api.create('sensitive prompt')
    store.append_event(run['run_id'], 'tool_result', {'output': 'private content'})
    first = api.events(run['run_id'], limit=1)
    second = api.events(run['run_id'], after=first[0]['sequence'], limit=1)
    assert first[0]['sequence'] < second[0]['sequence']
    assert second[0]['event_type'] == 'tool_result'
    assert 'private content' not in str(api.events(run['run_id']))
    assert 'sensitive prompt' not in str(api.list())
    assert 'workspace' not in api.get(run['run_id'])


@pytest.mark.parametrize('limit', [0, 201, -1, True, '2'])
def test_invalid_limits_fail(bound, limit):
    _, _, api = bound
    with pytest.raises(ValueError):
        api.list(limit=limit)


@pytest.mark.parametrize('after', [-1, True, '0'])
def test_invalid_cursor_fails(bound, after):
    _, _, api = bound
    run = api.create('input')
    with pytest.raises(ValueError):
        api.events(run['run_id'], after=after)


def test_succeeded_is_not_verified_without_acceptance(bound):
    store, _, api = bound
    run = api.create('input')
    for state in ['running', 'verifying', 'succeeded']:
        store.transition(run['run_id'], state)
    assert api.get(run['run_id'])['verification_state'] == 'unverified'


def test_invalid_scope_and_prompt(bound):
    _, grant, api = bound
    with pytest.raises(ValueError):
        replace(grant, subject='')
    for prompt in ['', None, 'x' * 65537]:
        with pytest.raises(ValueError):
            api.create(prompt)
    assert api.list() == []


def test_binding_failure_leaves_record_inaccessible(bound, monkeypatch):
    store, _, api = bound
    original_create = store.create_run
    original_connect = store._connect
    def broken_connect():
        raise OSError('Simulated binding failure')
    def create_then_fail(**kwargs):
        record = original_create(**kwargs)
        monkeypatch.setattr(store, '_connect', broken_connect)
        return record
    monkeypatch.setattr(store, 'create_run', create_then_fail)
    with pytest.raises(OSError):
        api.create('input')
    monkeypatch.setattr(store, '_connect', original_connect)
    orphan = store.list_runs()[0]
    assert api.list() == []
    with pytest.raises(RunNotFoundError):
        api.get(orphan.run_id)
