from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from megas.actions import MegasActions
from megas.action_skills import ActionSkills


def invoke(service, tool, **arguments):
    return service.call(tool, arguments)["structuredContent"]


@pytest.fixture
def setup(tmp_path):
    now = [1_800_000_000.0]
    owner = ["owner-a"]
    checked = []

    def plugin(name, args):
        checked.append(name)
        return {"isError": False, "structuredContent": {"status": "ok"}}

    def make():
        return ActionSkills(
            home=tmp_path, plugin_call=plugin, owner=lambda: owner[0], clock=lambda: now[0]
        )

    actions = MegasActions(tmp_path)
    action = invoke(actions, "megas_action_create", name="Delivery", description="", paths=[])
    args = dict(
        skill_id="review",
        name="Prepare review",
        action_id=action["id"],
        expected_revision=0,
        steps=[
            {"tool": "chitraagni_health"},
            {"tool": "megas_action_task_create", "title": "Review output"},
            {"tool": "megas_action_local_tasks"},
        ],
        trigger={"kind": "manual"},
        enabled=True,
    )
    return make, actions, args, now, owner, checked


def test_run_retry_restart_and_owner_isolation(setup):
    make, actions, args, now, owner, checked = setup
    invoke(make(), "megas_skill_save", **args)
    assert actions.store.list() == []

    def run(_):
        return invoke(make(), "megas_skill_run", skill_id="review", request_id="run-1")

    with ThreadPoolExecutor(max_workers=6) as pool:
        result = list(pool.map(run, range(6)))
    assert all(x == result[0] for x in result)
    assert checked and set(checked) == {"chitraagni_health"}
    assert result[0]["steps"][1]["task_id"] == actions.store.list()[0]["id"]
    assert result[0]["steps"][2]["local_task_count"] == 1
    assert invoke(make(), "megas_skill_history", skill_id="review")["runs"] == [result[0]]
    owner[0] = "owner-b"
    assert invoke(make(), "megas_skill_list") == {"skills": []}
    with pytest.raises(KeyError):
        run(1)


def test_schedule_restart_catchup_pause_and_idempotency(setup):
    make, actions, args, now, owner, checked = setup
    args["trigger"] = {
        "kind": "schedule",
        "at": datetime.fromtimestamp(now[0], UTC).isoformat(),
        "every_seconds": 60,
    }
    invoke(make(), "megas_skill_save", **args)
    assert len(make().poll()) == 1 and make().poll() == []
    now[0] += 180
    assert len(make().poll()) == 1 and make().poll() == []
    now[0] += 60
    invoke(make(), "megas_skill_save", **{**args, "expected_revision": 1, "enabled": False})
    assert make().poll() == [] and len(actions.store.list()) == 2
    with pytest.raises(ValueError, match="revision"):
        invoke(make(), "megas_skill_save", **args)


def test_run_granted_checks_owner_revision_and_cancellation_before_commit(setup):
    make, actions, args, _, owner, checked = setup
    invoke(make(), "megas_skill_save", **args)
    with pytest.raises(PermissionError):
        make().run_granted("another-owner", "review", 1, "x")
    with pytest.raises(PermissionError):
        make().run_granted(owner[0], "review", 2, "x")
    assert not checked

    def guard():
        if checked:
            raise PermissionError("Routine paused during plugin call")

    with pytest.raises(PermissionError, match="paused"):
        make().run_granted(owner[0], "review", 1, "x", guard=guard)
    assert not actions.store.list()
    first = make().run_granted(owner[0], "review", 1, "x")
    assert make().run_granted(owner[0], "review", 1, "x") == first
    assert len(actions.store.list()) == 1
    invoke(make(), "megas_skill_save", **{**args, "expected_revision": 1, "enabled": False})
    with pytest.raises(PermissionError):
        make().run_granted(owner[0], "review", 1, "x")


def test_actual_task_transition_and_atomic_failure(setup):
    make, actions, args, now, owner, checked = setup
    trigger = invoke(
        actions, "megas_action_task_create", action_id=args["action_id"], title="Coding turn"
    )
    args["trigger"] = {"kind": "task_state", "task_id": trigger["id"], "state": "completed"}
    invoke(make(), "megas_skill_save", **args)
    assert make().poll() == []
    invoke(
        actions,
        "megas_action_task_update",
        task_id=trigger["id"],
        expected_updated_at=trigger["updated_at"],
        state="completed",
        completion_note="Result inspected",
    )
    blocked = make()
    blocked.plugin_call = lambda *args: {"isError": True}
    assert blocked.poll() == [] and len(actions.store.list()) == 1
    assert len(make().poll()) == 1 and make().poll() == []
    assert len(actions.store.list()) == 2


def test_reject_unreviewed_steps_and_scoped_task_trigger(setup):
    make, actions, args, now, owner, checked = setup
    with pytest.raises(ValueError, match="Invalid saved-skill"):
        invoke(
            make(),
            "megas_skill_save",
            **{**args, "steps": [{"tool": "terminal_run", "command": "unused"}]},
        )
    protected = actions.store.create("Project task", project="owned-by-project")
    with pytest.raises(PermissionError):
        invoke(
            make(),
            "megas_skill_save",
            **{
                **args,
                "trigger": {"kind": "task_state", "task_id": protected["id"], "state": "open"},
            },
        )
