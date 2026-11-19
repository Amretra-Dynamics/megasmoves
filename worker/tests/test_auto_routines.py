from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime

import pytest

from megas import auto_routines as routines
from megas.actions import MegasActions
from megas.action_skills import ActionSkills


def epoch(iso):
    return datetime.fromisoformat(iso + "+05:30").timestamp()


@pytest.fixture
def app(tmp_path):
    owner = ["owner-1"]
    clock = [epoch("2026-11-11T22:50")]
    calls = []

    def plugin(name, args):
        calls.append(name)
        return {"isError": False, "structuredContent": {"status": "ok"}}

    def make():
        return ActionSkills(
            home=tmp_path, owner=lambda: owner[0], clock=lambda: clock[0], plugin_call=plugin
        )

    skills = make()
    actions = MegasActions(tmp_path)
    action = actions.call("megas_action_create", {"name": "Ops", "description": "", "paths": []})[
        "structuredContent"
    ]
    for key in ["night", "day"]:
        skills.call(
            "megas_skill_save",
            {
                "skill_id": key,
                "name": key,
                "action_id": action["id"],
                "expected_revision": 0,
                "enabled": True,
                "trigger": {"kind": "manual"},
                "steps": [
                    {"tool": "chitraagni_health"},
                    {"tool": "megas_action_task_create", "title": key},
                ],
            },
        )

    def call(kind, **args):
        return routines.call(make(), "megas_auto_routine_" + kind, args)["structuredContent"]

    config = {
        "timezone": "Asia/Kolkata",
        "sleep_start": "23:00",
        "wake_time": "06:30",
        "adaptive": False,
    }
    goal = {
        "goal_id": "ops",
        "objective": "Maintain daily delivery reviews",
        "expected_revision": 0,
        "unattended_granted": True,
        "day": {"skill_id": "day", "revision": 1},
        "overnight": {"skill_id": "night", "revision": 1},
    }
    call("settings", **config)
    call("save", **goal)
    return make, call, config, goal, clock, owner, actions, calls


def test_real_days_nights_morning_report_dedup_pause_and_owner_isolation(app):
    make, call, _, goal, now, owner, actions, _ = app

    def do(_=None):
        routines.poll_routines(make(), observer=lambda: pytest.fail("Manual schedule must never sample"))

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(do, range(10)))
    now[0] = epoch("2026-11-11T23:01")
    do()
    now[0] = epoch("2026-11-12T05:50")
    do()
    assert sorted(r["title"] for r in actions.store.list()) == ["day", "night"]
    before = call("report")
    assert {r["local_date"] for r in before["runs"]} == {"2026-11-11"}
    assert {r["receipt"]["steps"][1]["task_id"] for r in before["runs"]} == {
        t["id"] for t in actions.store.list()
    }
    call("control", goal_id="ops", enabled=False, expected_revision=1)
    now[0] = epoch("2026-11-12T09:00")
    do()
    assert len(actions.store.list()) == 2
    call("control", goal_id="ops", enabled=True, expected_revision=2)
    do()
    assert len(actions.store.list()) == 3
    owner[0] = "other-owner"
    assert call("report") == {"goals": [], "runs": [], "settings": None}
    do()
    assert len(actions.store.list()) == 3


def test_adaptive_nights_learn_only_opted_in_buckets_measurable_fallback(app):
    make, call, config, _, now, _, actions, _ = app
    call("settings", **{**config, "adaptive": True})
    for day in [11, 12, 13]:
        now[0] = epoch(f"2026-11-{day}T23:02")
        routines.poll_routines(make(), observer=lambda: True)
        assert call("report")["runs"][0]["reason"] == "recent_input_hold"
    now[0] = epoch("2026-11-14T23:05")
    routines.poll_routines(make(), observer=lambda: None)
    assert call("report")["runs"][0]["reason"] == "learned_active_half_hour_hold"
    assert not actions.store.list()
    now[0] = epoch("2026-11-14T23:40")
    routines.poll_routines(make(), observer=lambda: None)
    run = call("report")["runs"][0]
    assert run["state"] == "completed" and run["reason"] == "manual_fallback_signal_unavailable"
    call("settings", **config)
    with closing(routines._connect(make())) as db:
        assert db.execute("SELECT count(*) FROM routine_activity").fetchone()[0] == 0


def test_pause_during_plugin_check_prevents_task_write(app):
    make, call, _, _, now, _, actions, _ = app
    service = make()

    def interruption(_name, _args):
        call("control", goal_id="ops", enabled=False, expected_revision=1)
        return {"structuredContent": {"status": "ok"}}

    service.plugin_call = interruption
    routines.poll_routines(service)
    assert not actions.store.list()
    assert call("report")["runs"][0]["reason"] == "routine_paused_or_window_changed"
    call("control", goal_id="ops", enabled=True, expected_revision=2)
    now[0] += 61
    routines.poll_routines(make())
    assert len(actions.store.list()) == 1


def test_restart_after_effect_replays_receipt_without_duplicating_task(app):
    make, call, _, _, now, _, actions, _ = app
    service = make()
    grant = service.run_granted

    def crash(*args, **kwargs):
        grant(*args, **kwargs)
        raise SystemExit("simulate core exit")

    service.run_granted = crash
    with pytest.raises(SystemExit):
        routines.poll_routines(service)
    assert len(actions.store.list()) == 1
    now[0] += 65
    routines.poll_routines(make())
    assert len(actions.store.list()) == 1
    assert call("report")["runs"][0]["attempt"] == 2
    assert call("report")["runs"][0]["state"] == "completed"


def test_stale_pinned_revision_and_missing_unattended_grant(app):
    make, call, _, goal, _, _, actions, _ = app
    with pytest.raises(ValueError):
        call("save", **{**goal, "unattended_granted": False, "expected_revision": 1})
    svc = make()
    skill = svc.call("megas_skill_list", {})["structuredContent"]["skills"][0]
    revision = skill["revision"]
    editable = {k: skill[k] for k in ("skill_id", "name", "action_id", "steps", "trigger", "enabled")}
    svc.call("megas_skill_save", {**editable, "name": "Changed revision", "expected_revision": revision})
    now_phase = "day" if skill["skill_id"] == "day" else "overnight"
    if now_phase == "overnight":
        app[4][0] = epoch("2026-11-11T23:01")
    routines.poll_routines(svc)
    assert not actions.store.list()
    assert call("report")["runs"][0]["reason"] == "owner_or_skill_grant_unavailable"


def test_crash_across_window_reports_existing_evidence_without_replaying(app):
    make, call, _, _, now, _, actions, _ = app
    service = make()
    run = service.run_granted

    def crash(*args, **kwargs):
        run(*args, **kwargs)
        raise SystemExit()

    service.run_granted = crash
    with pytest.raises(SystemExit):
        routines.poll_routines(service)
    now[0] = epoch("2026-11-11T23:01")
    routines.poll_routines(make())
    report = call("report")
    day = next(r for r in report["runs"] if r["phase"] == "day")
    assert day["state"] == "completed" and day["reason"] == "existing_skill_receipt_verified"
    assert sorted(t["title"] for t in actions.store.list()) == ["day", "night"]


def test_overnight_cadence_skips_missed_cycles_and_completed_local_task_stops_future_work(app):
    make, call, _, goal, now, _, actions, _ = app
    action = actions.call("megas_action_list", {})["structuredContent"]["actions"][0]
    target = actions.call(
        "megas_action_task_create", {"action_id": action["id"], "title": "Complete review"}
    )["structuredContent"]
    call(
        "save",
        **{**goal, "expected_revision": 1, "until_task_id": target["id"], "overnight_every_minutes": 30},
    )
    for timestamp in ("2026-11-11T23:02", "2026-11-11T23:05", "2026-11-11T23:34", "2026-11-12T01:05"):
        now[0] = epoch(timestamp)
        # Re-open the operator store on each tick, including on the other side of midnight.
        routines.poll_routines(make())
    before = call("report")
    assert sorted(r["cycle"] for r in before["runs"]) == [0, 1, 4]
    assert all(r["phase"] == "overnight" and r["local_date"] == "2026-11-11" for r in before["runs"])
    assert len(actions.store.list()) == 4
    actions.call(
        "megas_action_task_update",
        {
            "task_id": target["id"],
            "expected_updated_at": target["updated_at"],
            "state": "completed",
            "completion_note": "Results verified",
        },
    )
    now[0] = epoch("2026-11-12T01:36")
    routines.poll_routines(make())
    now[0] = epoch("2026-11-12T08:15")
    routines.poll_routines(make())
    morning = call("report")
    assert not morning["goals"][0]["enabled"]
    assert morning["goals"][0]["completion"]["task_id"] == target["id"]
    assert morning["goals"][0]["completion"]["state"] == "completed"
    assert len(morning["runs"]) == 3 and len(actions.store.list()) == 4
    with pytest.raises(ValueError, match="Completed goal"):
        call("control", goal_id="ops", enabled=True, expected_revision=morning["goals"][0]["revision"])


def test_cadence_coalesces_dst_fallback_and_day_anchor_before_dawn():
    from zoneinfo import ZoneInfo

    eastern = ZoneInfo("America/New_York")
    prefs = {"start": "23:00", "wake": "06:30"}
    options = {"night_minutes": 30, "day_minutes": None}
    for fold in (0, 1):
        local = datetime(2026, 11, 1, 1, 15, tzinfo=eastern, fold=fold)
        phase, anchor = routines._window(local, prefs["start"], prefs["wake"])
        assert (phase, anchor) == ("overnight", "2026-10-31")
        assert routines._slot(local, phase, anchor, prefs, options) == "overnight/4"
    assert routines._window(datetime(2026, 11, 12, 1, 0), "02:00", "10:00") == ("day", "2026-11-11")


def test_active_account_change_during_sampling_discards_signal_and_work(app):
    make, call, config, _, clock, owner, actions, _ = app
    call("settings", **{**config, "adaptive": True})
    clock[0] = epoch("2026-11-11T23:07")

    def changed_account():
        owner[0] = "owner-2"
        return True

    routines.poll_routines(make(), observer=changed_account)
    assert call("report") == {"settings": None, "goals": [], "runs": []}
    owner[0] = "owner-1"
    assert call("report")["settings"]["last_input"] == "unobserved"
    with closing(routines._connect(make())) as db:
        assert db.execute("SELECT count(*) FROM routine_activity").fetchone()[0] == 0
    assert not actions.store.list() and not call("report")["runs"]


def test_second_transport_holds_overnight_work_until_busy_sample_finishes(app):
    from threading import Event

    make, call, config, _, clock, _, actions, _ = app
    call("settings", **{**config, "adaptive": True})
    clock[0] = epoch("2026-11-11T23:07")
    sampling, release = Event(), Event()

    def delayed_sample():
        sampling.set()
        assert release.wait(5)
        return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(routines.poll_routines, make(), delayed_sample)
        assert sampling.wait(5)
        try:
            routines.poll_routines(make(), lambda: pytest.fail("Sample already reserved"))
            assert not actions.store.list()
            held = call("report")["runs"][0]
            assert held["state"] == "held" and held["reason"] == "activity_sample_pending_hold"
        finally:
            release.set()
        first.result()
    assert call("report")["settings"]["last_input"] == "recent"
    assert not actions.store.list()


def test_inflight_sample_cannot_cross_an_opt_out_and_new_consent(app):
    make, call, config, _, clock, _, actions, _ = app
    adaptive = {**config, "adaptive": True}
    call("settings", **adaptive)
    clock[0] = epoch("2026-11-11T23:07")

    def old_sample():
        call("settings", **config)
        call("settings", **adaptive)
        routines.poll_routines(make(), lambda: True)
        return False

    routines.poll_routines(make(), old_sample)
    assert call("report")["settings"]["last_input"] == "recent"
    assert not actions.store.list()


def test_old_completed_target_cannot_disable_revised_running_goal(app):
    make, call, _, goal, _, _, actions, _ = app
    action = actions.call("megas_action_list", {})["structuredContent"]["actions"][0]

    def create(title):
        return actions.call("megas_action_task_create", {"action_id": action["id"], "title": title})[
            "structuredContent"
        ]

    old, new = create("Old target"), create("New target")
    call("save", **{**goal, "expected_revision": 1, "until_task_id": old["id"]})
    service = make()

    def replace_target(_name, _args):
        actions.call(
            "megas_action_task_update",
            {
                "task_id": old["id"],
                "expected_updated_at": old["updated_at"],
                "state": "completed",
                "completion_note": "Original objective verified",
            },
        )
        call("save", **{**goal, "expected_revision": 2, "until_task_id": new["id"]})
        return {"structuredContent": {"status": "ok"}}

    service.plugin_call = replace_target
    routines.poll_routines(service)
    current = call("report")["goals"][0]
    assert current["enabled"] and current["until_task_id"] == new["id"] and current["completion"] is None
    assert len(actions.store.list()) == 2
