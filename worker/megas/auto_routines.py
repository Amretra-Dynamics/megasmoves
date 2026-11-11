"""Owner-granted day and overnight goals for the installed saved-skill worker.

This local scheduler can only invoke an existing, enabled skill at the owner's
pinned revision. It cannot activate imported Auto goals or mint new capabilities.
"""

from contextlib import closing, contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import os
import stat
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import validate, ValidationError
from .action_skills import KEY, TASK_ID, REVIEW_TEXT, ReviewContextChanged, _object
from .action_local_projection import task_requires_project_authorization
from .auto_authorization import ActionRequest, TrustedScope, decide
from .policy import redact

CLOCK = {"type": "string", "pattern": r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"}
GRANT = _object(
    {"skill_id": KEY, "revision": {"type": "integer", "minimum": 1}}, ["skill_id", "revision"]
)
SCHEMAS = {
    "settings": _object(
        {
            "timezone": {"type": "string", "minLength": 1, "maxLength": 80},
            "sleep_start": CLOCK,
            "wake_time": CLOCK,
            "adaptive": {"type": "boolean"},
        },
        ["timezone", "sleep_start", "wake_time", "adaptive"],
    ),
    "save": _object(
        {
            "goal_id": KEY,
            "objective": REVIEW_TEXT,
            "day": {"oneOf": [GRANT, {"type": "null"}]},
            "overnight": {"oneOf": [GRANT, {"type": "null"}]},
            "day_every_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
            "overnight_every_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
            "until_task_id": TASK_ID,
            "unattended_granted": {"const": True},
            "expected_revision": {"type": "integer", "minimum": 0},
        },
        ["goal_id", "objective", "day", "overnight", "unattended_granted", "expected_revision"],
    ),
    "control": _object(
        {
            "goal_id": KEY,
            "enabled": {"type": "boolean"},
            "expected_revision": {"type": "integer", "minimum": 1},
        },
        ["goal_id", "enabled", "expected_revision"],
    ),
    "report": _object({}),
}
HELP = {
    "settings": "Save the owner's local timezone and manual sleep/wake times. Adaptive activity sampling is opt-in and remembers only recent-input half-hour buckets for 14 days. Changing settings clears previous samples.",
    "save": "Grant an enabled, exact saved-skill revision for day and/or overnight work toward a local goal. The owner-authored objective is delivered to each named local review worker in the pinned skill for its registered repository. A null phase grants nothing. Defaults to once per phase. Optionally repeat every requested number of minutes in a phase; missed cycles do not backfill. An explicit local until_task_id ends the goal when its completion is recorded. Runs while this operator is online.",
    "control": "Pause or resume this goal at its expected revision. Pause stops new effects; work already delivered has a separate receipt.",
    "report": "Read this owner's routine settings, goals, interruptions and recent day/overnight receipts. For the latest 12 goal-directed reviews, agent_brief appears when a matching reply is available and includes its bounded text and matching repository evidence.",
}


def tools():
    return [
        {
            "name": "megas_auto_routine_" + key,
            "description": HELP[key],
            "inputSchema": schema,
            "annotations": {
                "readOnlyHint": key == "report",
                "destructiveHint": False,
                "openWorldHint": False,
            },
        }
        for key, schema in SCHEMAS.items()
    ]


@contextmanager
def _write(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _connect(skills):
    path = skills.store.root / "auto-routines.sqlite3"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise PermissionError("Invalid routine store")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError("Invalid routine store")
        if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise PermissionError("Routine store is not private")
    finally:
        os.close(fd)
    db = sqlite3.connect(path, isolation_level=None, timeout=5)
    db.row_factory = sqlite3.Row
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS routine_preferences(
          owner TEXT PRIMARY KEY, tz TEXT NOT NULL, start TEXT NOT NULL, wake TEXT NOT NULL,
          adaptive INTEGER NOT NULL, next_sample REAL DEFAULT 0, last_input TEXT DEFAULT 'unobserved');
        CREATE TABLE IF NOT EXISTS routine_sample_claims(owner TEXT PRIMARY KEY, claim TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS routine_activity(
          owner TEXT, local_date TEXT, bucket INTEGER, PRIMARY KEY(owner,local_date,bucket));
        CREATE TABLE IF NOT EXISTS routine_goals(
          owner TEXT, id TEXT, objective TEXT, day TEXT, night TEXT, enabled INTEGER, revision INTEGER,
          PRIMARY KEY(owner,id));
        CREATE TABLE IF NOT EXISTS routine_options(
          owner TEXT, goal TEXT, day_minutes INTEGER, night_minutes INTEGER, until_task TEXT, completion TEXT,
          PRIMARY KEY(owner,goal));
        CREATE TABLE IF NOT EXISTS routine_runs(
          owner TEXT, goal TEXT, local_date TEXT, phase TEXT, skill TEXT, skill_revision INTEGER,
          state TEXT, reason TEXT, attempt INTEGER, next_try REAL, updated REAL, receipt TEXT,
          PRIMARY KEY(owner,goal,local_date,phase));
    """)
    return db


def _window(local, start, wake):
    clock = local.strftime("%H:%M")
    overnight = clock >= start or clock < wake if start > wake else start <= clock < wake
    phase = "overnight" if overnight else "day"
    boundary = start if overnight else wake
    anchor = local.date() if clock >= boundary else local.date() - timedelta(days=1)
    return phase, anchor.isoformat()


def _options(db, uid, goal):
    return db.execute("SELECT * FROM routine_options WHERE owner=? AND goal=?", (uid, goal)).fetchone()


def _slot(local, phase, anchor, prefs, options):
    every = options["night_minutes" if phase == "overnight" else "day_minutes"] if options else None
    if not every:
        return phase
    boundary = prefs["start" if phase == "overnight" else "wake"]
    hours, mins = map(int, boundary.split(":"))
    days = (local.date() - datetime.fromisoformat(anchor).date()).days
    elapsed = days * 1440 + local.hour * 60 + local.minute - hours * 60 - mins
    index = elapsed // every
    # Stable local clock slots intentionally coalesce a repeated DST hour.
    return phase if not index else f"{phase}/{index}"


def _target(skills, task_id):
    # The Task store writes via atomic replacement. One complete snapshot is
    # safe both inside a saved skill's write lock and from a scheduler tick.
    task = next((row for row in skills.store._load_unlocked() if row.id == task_id), None)
    if task is None or task_requires_project_authorization(task):
        raise PermissionError("Linked local Task unavailable")
    return task


def _finished(skills, db, uid, goal, options, now):
    if not options or not options["until_task"]:
        return False
    task = _target(skills, options["until_task"])
    if task.state != "completed":
        return False
    evidence = json.dumps(
        {"task_id": task.id, "state": task.state, "task_updated_at": task.updated_at, "observed_at": now}
    )
    db.execute(
        "UPDATE routine_options SET completion=COALESCE(completion,?) WHERE owner=? AND goal=?",
        (evidence, uid, goal),
    )
    db.execute(
        "UPDATE routine_goals SET enabled=0, revision=revision+1 WHERE owner=? AND id=? AND enabled=1",
        (uid, goal),
    )
    return True


class RoutineInterrupted(PermissionError):
    pass


def _sample(db, uid, prefs, local, now, observer, current_owner):
    if not prefs["adaptive"] or prefs["next_sample"] > now:
        return
    # Reserve the sample first: multiple operator transports share one cadence.
    claim = uuid4().hex
    with _write(db):
        result = db.execute(
            "UPDATE routine_preferences SET next_sample=?,last_input='sampling' "
            "WHERE owner=? AND adaptive=1 AND next_sample<=? AND tz=? AND start=? AND wake=?",
            (now + 60, uid, now, prefs["tz"], prefs["start"], prefs["wake"]),
        )
        if not result.rowcount:
            return
        db.execute("INSERT OR REPLACE INTO routine_sample_claims VALUES(?,?)", (uid, claim))
    try:
        if current_owner() != uid:
            raise RoutineInterrupted("Owner changed before activity sampling")
        signal = observer()
        if current_owner() != uid:
            raise RoutineInterrupted("Owner changed during activity sampling")
    except Exception:
        with _write(db):
            if db.execute(
                "DELETE FROM routine_sample_claims WHERE owner=? AND claim=?", (uid, claim)
            ).rowcount:
                db.execute(
                    "UPDATE routine_preferences SET last_input='unobserved',next_sample=0 WHERE owner=?",
                    (uid,),
                )
        raise
    with _write(db):
        # Settings edits, including an opt-out/opt-in, cancel an old sample.
        if not db.execute(
            "DELETE FROM routine_sample_claims WHERE owner=? AND claim=?", (uid, claim)
        ).rowcount:
            return
        db.execute(
            "UPDATE routine_preferences SET last_input=? WHERE owner=?",
            ("recent" if signal is True else "quiet" if signal is False else "unavailable", uid),
        )
        db.execute(
            "DELETE FROM routine_activity WHERE owner=? AND local_date<?",
            (uid, (local.date() - timedelta(days=13)).isoformat()),
        )
        if signal is True:
            db.execute(
                "INSERT OR IGNORE INTO routine_activity VALUES(?,?,?)",
                (uid, local.date().isoformat(), local.hour * 2 + local.minute // 30),
            )


def _reason(db, uid, prefs, local, phase):
    if not prefs["adaptive"]:
        return "manual_schedule"
    if phase == "overnight":
        if prefs["last_input"] == "sampling":
            return "activity_sample_pending_hold"
        if prefs["last_input"] == "recent":
            return "recent_input_hold"
        previous = db.execute(
            "SELECT count(*) FROM routine_activity WHERE owner=? AND bucket=? AND local_date<? AND local_date>=?",
            (
                uid,
                local.hour * 2 + local.minute // 30,
                local.date().isoformat(),
                (local.date() - timedelta(days=13)).isoformat(),
            ),
        ).fetchone()[0]
        if previous >= 3:
            return "learned_active_half_hour_hold"
    return (
        "manual_fallback_signal_unavailable"
        if prefs["last_input"] in {"unavailable", "unobserved"}
        else "manual_schedule_with_activity"
    )


def _report(db, uid):
    prefs = db.execute(
        "SELECT tz AS timezone,start AS sleep_start,wake AS wake_time,adaptive,last_input FROM routine_preferences WHERE owner=?",
        (uid,),
    ).fetchone()
    goals = []
    for row in db.execute(
        "SELECT id,objective,enabled,revision,day,night FROM routine_goals WHERE owner=? ORDER BY id",
        (uid,),
    ):
        options = _options(db, uid, row["id"])
        goals.append(
            {
                "goal_id": row["id"],
                "objective": row["objective"],
                "day_every_minutes": options["day_minutes"] if options else None,
                "overnight_every_minutes": options["night_minutes"] if options else None,
                "until_task_id": options["until_task"] if options else None,
                "completion": json.loads(options["completion"])
                if options and options["completion"]
                else None,
                "enabled": bool(row["enabled"]),
                "revision": row["revision"],
                "day": json.loads(row["day"]),
                "overnight": json.loads(row["night"]),
            }
        )
    runs = []
    for row in db.execute(
        "SELECT goal,local_date,phase,skill,skill_revision,state,reason,attempt,updated,receipt FROM routine_runs WHERE owner=? ORDER BY updated DESC,local_date DESC,goal LIMIT 100",
        (uid,),
    ):
        run = dict(row)
        phase, _, cycle = run["phase"].partition("/")
        run["phase"] = phase
        run["cycle"] = int(cycle or 0)
        run["receipt"] = json.loads(run["receipt"]) if run["receipt"] else None
        runs.append(run)
    settings = dict(prefs) if prefs else None
    if settings:
        settings["adaptive"] = bool(settings["adaptive"])
    return {"settings": settings, "goals": goals, "runs": runs}


def _morning_briefs(skills, report):

    recent = []
    for run in report["runs"]:
        receipt = run["receipt"] or {}
        context = receipt.get("review_context") or {}
        if context.get("goal_id") != run["goal"] or context.get("phase") != run["phase"]:
            continue
        for step in receipt.get("steps", []):
            if step.get("state") == "queued" and step.get("message_id"):
                recent.append(step)
                if len(recent) == 12:
                    break
        if len(recent) == 12:
            break
    if recent:
        requested = {s["message_id"]: s["recipient_agent"] for s in recent}
        mailbox = skills.review_mailbox
        try:
            if mailbox is None:
                raise RuntimeError("Review mailbox unavailable")
            replies = mailbox.review_replies(requested)
        except (OSError, ValueError, RuntimeError):
            for step in recent:
                step["agent_brief"] = {"status": "mailbox_unavailable"}
            return
        for step in recent:
            if step["message_id"] in replies:
                step["agent_brief"] = replies[step["message_id"]]


def call(skills, name, args):
    kind = name.removeprefix("megas_auto_routine_")
    try:
        validate(args, SCHEMAS[kind])
    except ValidationError:
        raise ValueError("Invalid Auto routine arguments") from None
    uid = skills.owner()
    with closing(_connect(skills)) as db:
        if kind == "settings":
            try:
                ZoneInfo(args["timezone"])
            except (ValueError, ZoneInfoNotFoundError):
                raise ValueError("Unknown IANA timezone") from None
            if args["sleep_start"] == args["wake_time"]:
                raise ValueError("Sleep and wake times must differ")
            with _write(db):
                db.execute(
                    "INSERT OR REPLACE INTO routine_preferences(owner,tz,start,wake,adaptive) VALUES(?,?,?,?,?)",
                    (uid, args["timezone"], args["sleep_start"], args["wake_time"], args["adaptive"]),
                )
                db.execute("DELETE FROM routine_sample_claims WHERE owner=?", (uid,))
                db.execute("DELETE FROM routine_activity WHERE owner=?", (uid,))
        elif kind in {"save", "control"}:
            key = args["goal_id"]
            if kind == "save":
                if args.get("until_task_id"):
                    _target(skills, args["until_task_id"])
                pins = [p for p in (args["day"], args["overnight"]) if p is not None]
                if not pins:
                    raise ValueError("At least one phase requires a skill grant")
                listing = skills.call("megas_skill_list", {})["structuredContent"]["skills"]
                if skills.owner() != uid:
                    raise PermissionError("Owner changed")
                allowed = {(s["skill_id"], s["revision"]) for s in listing if s["enabled"]}
                if any((p["skill_id"], p["revision"]) not in allowed for p in pins):
                    raise PermissionError("Skill revision is unavailable")
            with _write(db):
                row = db.execute(
                    "SELECT revision FROM routine_goals WHERE owner=? AND id=?", (uid, key)
                ).fetchone()
                revision = row[0] if row else 0
                if revision != args["expected_revision"]:
                    raise ValueError("Goal revision conflict")
                if kind == "save":
                    if not db.execute(
                        "SELECT 1 FROM routine_preferences WHERE owner=?", (uid,)
                    ).fetchone():
                        raise ValueError("Configure local sleep times first")
                    if (
                        not row
                        and db.execute(
                            "SELECT count(*) FROM routine_goals WHERE owner=?", (uid,)
                        ).fetchone()[0]
                        >= 64
                    ):
                        raise ValueError("Routine goal limit reached")
                    db.execute(
                        "INSERT OR REPLACE INTO routine_goals VALUES(?,?,?,?,?,?,?)",
                        (
                            uid,
                            key,
                            redact(args["objective"])[:1000],
                            json.dumps(args["day"]),
                            json.dumps(args["overnight"]),
                            1,
                            revision + 1,
                        ),
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO routine_options VALUES(?,?,?,?,?,NULL)",
                        (
                            uid,
                            key,
                            args.get("day_every_minutes"),
                            args.get("overnight_every_minutes"),
                            args.get("until_task_id"),
                        ),
                    )
                else:
                    options = _options(db, uid, key)
                    if args["enabled"] and options and options["completion"]:
                        raise ValueError("Completed goal requires a new grant")
                    db.execute(
                        "UPDATE routine_goals SET enabled=?,revision=revision+1 WHERE owner=? AND id=?",
                        (args["enabled"], uid, key),
                    )
                    if not args["enabled"]:
                        db.execute(
                            "UPDATE routine_runs SET reason='owner_paused' WHERE owner=? AND goal=? AND state IN ('held','retry','running')",
                            (uid, key),
                        )
        value = _report(db, uid)
    if kind == "report":
        _morning_briefs(skills, value)
    if skills.owner() != uid:
        raise PermissionError("Routine owner changed")
    return {
        "content": [{"type": "text", "text": json.dumps(value)}],
        "structuredContent": value,
        "isError": False,
    }


def _request_id(key):
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()


def _receipt_state(receipt, reason):
    states = {s.get("state") for s in receipt["steps"]}
    if "delivery_unconfirmed" in states:
        return "attention", "agent_delivery_unconfirmed"
    if states & {"pending_delivery", "dispatching"}:
        return "retry", "agent_delivery_pending"
    if "cancelled_before_delivery" in states:
        return "attention", "agent_delivery_cancelled"
    if "queued" in states:
        return "agent_queued", "agent_review_delivered"
    return "completed", reason


def _recover_closed_window(skills, db, uid, phase, anchor, local, prefs, now):
    # Inspect only expired claims and never replay an earlier slot.
    candidates = db.execute(
        "SELECT * FROM routine_runs WHERE owner=? AND state IN ('running','retry','held') AND next_try<=? ORDER BY updated LIMIT 65",
        (uid, now),
    )
    run = next(
        (
            r
            for r in candidates
            if r["local_date"] != anchor
            or r["phase"] != _slot(local, phase, anchor, prefs, _options(db, uid, r["goal"]))
        ),
        None,
    )
    if not run:
        return
    key = (uid, run["goal"], run["local_date"], run["phase"])
    receipt = None
    state, reason = (
        ("attention", "expired_window_outcome_unverified")
        if run["attempt"]
        else ("skipped", "window_closed_while_held")
    )
    if run["attempt"]:
        try:
            history = skills.call("megas_skill_history", {"skill_id": run["skill"]})[
                "structuredContent"
            ]["runs"]
            if skills.owner() != uid:
                return
            expected = "routine/" + _request_id(key)
            receipt = next(
                (
                    r
                    for r in history
                    if r["run_id"] == expected and r["revision"] == run["skill_revision"]
                ),
                None,
            )
            if receipt:
                state, reason = _receipt_state(receipt, "existing_skill_receipt_verified")
                if state == "retry":
                    state, reason = "attention", "expired_window_pending_delivery"
        except (KeyError, PermissionError):
            pass
    with _write(db):
        db.execute(
            "UPDATE routine_runs SET state=?,reason=?,updated=?,receipt=? WHERE owner=? AND goal=? AND local_date=? AND phase=? AND state=? AND updated=?",
            (
                state,
                reason,
                now,
                json.dumps(receipt) if receipt else None,
                *key,
                run["state"],
                run["updated"],
            ),
        )


def poll_routines(skills, observer=None):
    try:
        return _poll(skills, observer if observer is not None else lambda: None)
    except sqlite3.Error as exc:
        # The persistent supervisor retries store contention without terminating.
        raise RuntimeError("Routine store unavailable") from exc


def _poll(skills, observer):
    """Bounded to one effectful invocation per tick; retries reuse the same skill request ID."""
    if not (skills.store.root / "auto-routines.sqlite3").is_file():
        return
    uid = skills.owner()
    now = skills.clock()
    with closing(_connect(skills)) as db:
        prefs = db.execute("SELECT * FROM routine_preferences WHERE owner=?", (uid,)).fetchone()
        if not prefs:
            return
        local = datetime.fromtimestamp(now, ZoneInfo(prefs["tz"]))
        try:
            _sample(db, uid, prefs, local, now, observer, skills.owner)
        except RoutineInterrupted:
            return
        if skills.owner() != uid:
            return
        phase, anchor = _window(local, prefs["start"], prefs["wake"])
        _recover_closed_window(skills, db, uid, phase, anchor, local, prefs, now)
        with _write(db):
            prefs = db.execute("SELECT * FROM routine_preferences WHERE owner=?", (uid,)).fetchone()
            local = datetime.fromtimestamp(now, ZoneInfo(prefs["tz"]))
            phase, anchor = _window(local, prefs["start"], prefs["wake"])
            reason = _reason(db, uid, prefs, local, phase)
            for goal in db.execute(
                "SELECT * FROM routine_goals WHERE owner=? AND enabled=1 ORDER BY id", (uid,)
            ):
                options = _options(db, uid, goal["id"])
                try:
                    if _finished(skills, db, uid, goal["id"], options, now):
                        continue
                    reason = _reason(db, uid, prefs, local, phase)
                except PermissionError:
                    reason = "linked_task_unavailable_hold"
                pin = json.loads(goal["night" if phase == "overnight" else "day"])
                if not pin:
                    continue
                slot = _slot(local, phase, anchor, prefs, options)
                key = (uid, goal["id"], anchor, slot)
                run = db.execute(
                    "SELECT * FROM routine_runs WHERE owner=? AND goal=? AND local_date=? AND phase=?",
                    key,
                ).fetchone()
                if run and (
                    run["state"] in {"completed", "agent_queued", "attention"} or run["next_try"] > now
                ):
                    continue
                if (
                    run
                    and run["attempt"]
                    and (pin["skill_id"], pin["revision"]) != (run["skill"], run["skill_revision"])
                ):
                    db.execute(
                        "UPDATE routine_runs SET state='attention',reason='grant_changed_after_attempt',updated=? WHERE owner=? AND goal=? AND local_date=? AND phase=?",
                        (now, *key),
                    )
                    continue
                attempt = run["attempt"] if run else 0
                state = "held" if reason.endswith("_hold") else "running"
                attempt += int(state == "running")
                db.execute(
                    "INSERT OR REPLACE INTO routine_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)",
                    (*key, pin["skill_id"], pin["revision"], state, reason, attempt, now + 60, now),
                )
                if state == "running":
                    break
            else:
                return

        def guard():
            if skills.owner() != uid:
                raise RoutineInterrupted("Routine owner changed")
            # Revision/permission checks and completion updates are serialized.
            # An old target can never complete a newly edited goal.
            with _write(db):
                current = db.execute(
                    "SELECT enabled,revision,day,night FROM routine_goals WHERE owner=? AND id=?",
                    key[:2],
                ).fetchone()
                if not current or current["revision"] != goal["revision"]:
                    raise RoutineInterrupted("Routine grant changed")
                live_pin = json.loads(current["night" if phase == "overnight" else "day"])
                allowed = (
                    frozenset({f"{live_pin['skill_id']}:{live_pin['revision']}"})
                    if live_pin
                    else frozenset()
                )
                scope = TrustedScope(
                    frozenset({"local_actions"}),
                    allowed,
                    frozenset({"read", "workspace_write"}),
                    bool(current["enabled"]),
                )
                request = ActionRequest(
                    "local_actions", f"{pin['skill_id']}:{pin['revision']}", "workspace_write"
                )
                if (
                    decide(request, scope, profile="custom", custom_effects=("read", "workspace_write"))
                    != "allow"
                ):
                    raise RoutineInterrupted("Routine grant changed")
                finished = _finished(skills, db, uid, goal["id"], options, skills.clock())
            if finished:
                raise RoutineInterrupted("Linked Task complete")
            latest = db.execute("SELECT * FROM routine_preferences WHERE owner=?", (uid,)).fetchone()
            moment = datetime.fromtimestamp(skills.clock(), ZoneInfo(latest["tz"]))
            if (
                _slot(moment, phase, anchor, latest, options) != slot
                or _window(moment, latest["start"], latest["wake"]) != (phase, anchor)
                or _reason(db, uid, latest, moment, phase).endswith("_hold")
            ):
                raise RoutineInterrupted("Routine window changed")

        receipt = None
        try:
            guard()
            receipt = skills.run_granted(
                uid,
                pin["skill_id"],
                pin["revision"],
                _request_id(key),
                guard=guard,
                review_goal={
                    "goal_id": goal["id"],
                    "goal_revision": goal["revision"],
                    "phase": phase,
                    "objective": goal["objective"],
                },
            )
            state, reason = _receipt_state(receipt, reason)
        except RoutineInterrupted:
            state, reason = "retry", "routine_paused_or_window_changed"
        except ReviewContextChanged:
            state, reason = "attention", "review_context_changed_after_attempt"
        except PermissionError:
            state, reason = "attention", "owner_or_skill_grant_unavailable"
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            state = "attention" if attempt >= 5 else "retry"
            reason = "grant_or_dependency_unavailable:" + type(exc).__name__
        if state == "retry" and attempt >= 5:
            state = "attention"
        with _write(db):
            db.execute(
                "UPDATE routine_runs SET state=?,reason=?,next_try=?,updated=?,receipt=? WHERE owner=? AND goal=? AND local_date=? AND phase=? AND attempt=? AND state='running'",
                (
                    state,
                    reason,
                    now + min(900, 30 * 2 ** min(attempt, 5)),
                    skills.clock(),
                    json.dumps(receipt) if receipt else None,
                    *key,
                    attempt,
                ),
            )
            db.execute("DELETE FROM routine_runs WHERE owner=? AND updated<?", (uid, now - 90 * 86400))
