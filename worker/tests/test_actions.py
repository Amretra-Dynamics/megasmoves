import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from jsonschema.exceptions import ValidationError
from megas.actions import MegasActions, tools
from megas.agent_tasks import AgentTaskStore, TaskRecord


def call(service, operation, **args):
    return service.call('megas_action_' + operation, args)['structuredContent']


def test_containers_paths_tasks_and_legacy_data_survive_restart(tmp_path):
    folder = tmp_path / 'app'
    folder.mkdir()
    file = tmp_path / 'reference.md'
    file.write_text('reference')
    store = AgentTaskStore(tmp_path)
    legacy = store.create('Legacy task', project='existing-project')
    def allow_test_project(record):
        assert record.project == 'existing-project'
    service = MegasActions(tmp_path, authorize=allow_test_project)
    first = call(service, 'create', name='Build app', description='Work', paths=[str(folder), str(file)])
    second = call(service, 'create', name='Review', description='', paths=[])
    task = call(service, 'task_create', action_id=first['id'], title='Inspect')
    store.assign(task['id'], 'codex')
    store.unassign(task['id'])
    call(service, 'task_move', action_id=second['id'], task_id=legacy['id'])
    edited = call(service, 'update', action_id=first['id'], expected_revision=1, name='App review', description='Updated', paths=[str(file)])
    reopened = MegasActions(tmp_path)
    listing = call(reopened, 'list')['actions']
    assert len(listing) == 2 and all(a['task_count'] == 1 for a in listing)
    assert edited['paths'] == [{'path': str(file.resolve()), 'kind': 'file'}]
    assert store.get(task['id']).action_id == first['id']
    assert store.get(legacy['id']).project == 'existing-project'
    assert edited['execution_started'] is False


def test_invalid_paths_and_stale_edits_leave_data_untouched(tmp_path):
    service = MegasActions(tmp_path)
    action = call(service, 'create', name='A', description='', paths=[])
    before = service.store.path.read_bytes()
    for paths in [['relative'], [str(tmp_path / 'missing')]]:
        with pytest.raises((ValueError, FileNotFoundError)):
            call(service, 'update', action_id=action['id'], expected_revision=1, name='B', description='', paths=paths)
        assert service.store.path.read_bytes() == before
    with pytest.raises(RuntimeError, match='revision conflict'):
        call(service, 'update', action_id=action['id'], expected_revision=2, name='B', description='', paths=[])
    with pytest.raises(ValidationError):
        call(service, 'create', name='B', description='', paths=[], command='whoami')
    assert service.store.path.read_bytes() == before


def test_concurrent_task_creation_keeps_both_containers(tmp_path):
    service = MegasActions(tmp_path)
    actions = [call(service, 'create', name=n, description='', paths=[]) for n in ['A', 'B']]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: call(MegasActions(tmp_path), 'task_create', action_id=actions[i % 2]['id'], title=str(i)), range(20)))
    listing = call(service, 'list')['actions']
    assert [a['task_count'] for a in listing] == [10, 10]
    assert len(AgentTaskStore(tmp_path).list()) == 20


def test_moving_linked_task_does_not_bypass_account_authorization(tmp_path):
    service = MegasActions(tmp_path)
    action = call(service, 'create', name='A', description='', paths=[])
    store = service.store
    task = store.create('Protected', project='remote-project')
    with store._lock():
        rows = store._load_unlocked()
        rows[0].project_authority_scope = 'account-A'
        store._save_unlocked(rows)
    with pytest.raises(PermissionError):
        call(service, 'task_move', action_id=action['id'], task_id=task['id'])
    assert store.get(task['id']).action_id is None
    assert store.get(task['id']).project_authority_scope == 'account-A'


def test_local_only_view_during_chitraagni_outage_excludes_protected_tasks(tmp_path):
    def unavailable(_task):
        raise RuntimeError('Chitraagni API unavailable')

    service = MegasActions(tmp_path, authorize=unavailable)
    action = call(service, 'create', name='Mixed work', description='', paths=[])
    local = call(service, 'task_create', action_id=action['id'], title='Safe local task')
    with service.store._lock():
        rows = service.store._load_unlocked()
        rows.extend([
            TaskRecord(title='Remote scoped secret', action_id=action['id'],
                       project='remote-project', project_authority_scope='account-A'),
            TaskRecord(title='Legacy project secret', action_id=action['id'],
                       project='legacy-project'),
            TaskRecord(title='Orphan scope secret', action_id=action['id'],
                       project_authority_scope='account-A'),
        ])
        service.store._save_unlocked(rows)
    before = service.store.path.read_bytes()

    with pytest.raises(RuntimeError, match='Chitraagni API unavailable'):
        call(service, 'get', action_id=action['id'])

    safe = call(service, 'local_tasks', action_id=action['id'])
    assert safe['view'] == 'local_unscoped_only'
    assert [t['id'] for t in safe['tasks']] == [local['id']]
    assert 'secret' not in json.dumps(safe)
    assert service.store.path.read_bytes() == before

    # Default read stays deny-by-default even without a callback.
    with pytest.raises(PermissionError, match='verified'):
        call(MegasActions(tmp_path), 'get', action_id=action['id'])


def test_legacy_project_only_task_move_requires_explicit_authorization(tmp_path):
    """A missing project_authority_scope does not grant local-only status."""
    service = MegasActions(tmp_path)
    action = call(service, 'create', name='A', description='', paths=[])
    legacy = service.store.create('Legacy project task', project='legacy-project')
    before = service.store.path.read_bytes()
    with pytest.raises(PermissionError, match='linked task'):
        call(service, 'task_move', action_id=action['id'], task_id=legacy['id'])
    assert service.store.path.read_bytes() == before
    assert service.store.get(legacy['id']).action_id is None

    def deny_project(record):
        raise PermissionError('revoked')
    with pytest.raises(PermissionError, match='revoked'):
        call(MegasActions(tmp_path, authorize=deny_project), 'task_move',
             action_id=action['id'], task_id=legacy['id'])
    assert service.store.path.read_bytes() == before


def test_legacy_project_only_task_move_accepts_verified_authorization(tmp_path):
    service = MegasActions(tmp_path)
    action = call(service, 'create', name='A', description='', paths=[])
    legacy = service.store.create('Authorized legacy task', project='legacy-project')
    observed = []
    def authorize(record):
        observed.append(record.project)
    moved = call(MegasActions(tmp_path, authorize=authorize), 'task_move',
                 action_id=action['id'], task_id=legacy['id'])
    assert moved['action_id'] == action['id']
    assert observed == ['legacy-project']


def test_voice_plan_is_atomic_idempotent_and_uses_the_operator_task_store(tmp_path):
    from uuid import uuid4

    service = MegasActions(tmp_path)
    args = dict(voice_entry_id=str(uuid4()), name="Publish", description="Plan the release",
                task_titles=["Check release notes", "Review screenshots"])
    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(pool.map(lambda _: call(MegasActions(tmp_path), 'save_voice_plan', **args), range(3)))
    assert all(item == receipts[0] for item in receipts)
    receipt = receipts[0]
    assert not receipt["execution_started"]
    assert len(receipt["task_ids"]) == 2
    members = call(service, "local_tasks", action_id=receipt["action_id"])["tasks"]
    assert [(t["title"], t["state"], t["assigned_client"]) for t in members] == [
        (title, "open", None) for title in args["task_titles"]
    ]
    call(service, "task_create", action_id=receipt["action_id"], title="Unrelated later task")
    assert call(service, "save_voice_plan", **args) == receipt
    before = service.store.path.read_bytes()
    with pytest.raises(ValueError):
        call(service, "save_voice_plan", **{**args, "voice_entry_id": str(uuid4()), "task_titles": ["OK", "  "]})
    assert service.store.path.read_bytes() == before
