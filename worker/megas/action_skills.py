"""Owner-scoped reusable local Actions with verified plugin reads and atomic task receipts."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import hashlib
import logging
import threading
import time

from jsonschema import ValidationError, validate

from .action_local_projection import (
    local_unscoped_action_tasks,
    task_requires_project_authorization,
)
from .actions import ID, MegasActions, append_local_action_task
from .agent_tasks import TASK_STATES, TaskRecord
from .policy import redact


def _object(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


KEY = {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,80}$"}
LABEL = {"type": "string", "minLength": 1, "maxLength": 128, "pattern": r"\S"}
PENDING = {"pending_delivery", "dispatching"}
MISSING = object()
TASK_ID = {"type": "string", "pattern": "^task_[a-f0-9]{32}$"}
TEXT = {"type": "string", "minLength": 1, "maxLength": 512, "pattern": r"\S"}
REVIEW_TEXT = {"type": "string", "minLength": 1, "maxLength": 1000, "pattern": r"\S"}
REVIEW_GOAL = _object(
    {
        "goal_id": KEY,
        "goal_revision": {"type": "integer", "minimum": 1},
        "phase": {"enum": ["day", "overnight"]},
        "objective": REVIEW_TEXT,
    },
    ["goal_id", "goal_revision", "phase", "objective"],
)
TRIGGER = {
    "oneOf": [
        _object({"kind": {"const": "manual"}}, ["kind"]),
        _object({"kind": {"const": "agent_turn_completed"}, "agent": LABEL}, ["kind", "agent"]),
        _object(
            {
                "kind": {"const": "schedule"},
                "at": {"type": "string", "maxLength": 40},
                "every_seconds": {"type": "integer", "minimum": 60, "maximum": 31536000},
            },
            ["kind", "at"],
        ),
        _object(
            {
                "kind": {"const": "task_state"},
                "task_id": TASK_ID,
                "state": {"enum": sorted(TASK_STATES)},
            },
            ["kind", "task_id", "state"],
        ),
    ]
}
STEP = {
    "oneOf": [
        _object({"tool": {"const": "chitraagni_health"}}, ["tool"]),
        _object(
            {
                "tool": {"const": "plugin_read"},
                "name": {"type": "string", "pattern": "^(chitraagni_[a-z0-9_]{1,96}|mcp_[0-9a-f]{32})$"},
                "arguments": {"type": "object", "maxProperties": 64},
            },
            ["tool", "name", "arguments"],
        ),
        _object(
            {
                "tool": {"const": "plugin_call"},
                "name": {"type": "string", "pattern": "^mcp_[0-9a-f]{32}$"},
                "arguments": {"type": "object", "maxProperties": 64},
            },
            ["tool", "name", "arguments"],
        ),
        _object(
            {
                "tool": {"const": "use_skill"},
                "skill_id": KEY,
                "revision": {"type": "integer", "minimum": 1},
            },
            ["tool", "skill_id", "revision"],
        ),
        _object({"tool": {"const": "megas_action_local_tasks"}}, ["tool"]),
        _object({"tool": {"const": "megas_action_task_create"}, "title": TEXT}, ["tool", "title"]),
        _object(
            {
                "tool": {"const": "local_agent_review"},
                "recipient_agent": LABEL,
                "workspace_project": LABEL,
            },
            ["tool", "recipient_agent", "workspace_project"],
        ),
    ]
}
CONDITION = _object(
    {
        "path": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "oneOf": [
                    {"type": "string", "maxLength": 80},
                    {"type": "integer", "minimum": 0, "maximum": 1023},
                ]
            },
        },
        "operator": {"enum": ["equals", "count_at_least"]},
        "value": {"type": ["string", "number", "boolean", "null"], "maxLength": 256},
    },
    ["path", "operator", "value"],
)
for variant in STEP["oneOf"]:
    if variant["properties"]["tool"]["const"] != "use_skill":
        variant["properties"]["when"] = CONDITION
SCHEMAS = {
    "list": _object({}),
    "capabilities": _object({}),
    "save": _object(
        {
            "skill_id": KEY,
            "name": TEXT,
            "action_id": ID,
            "steps": {"type": "array", "minItems": 1, "maxItems": 12, "items": STEP},
            "trigger": TRIGGER,
            "enabled": {"type": "boolean"},
            "expected_revision": {"type": "integer", "minimum": 0},
        },
        ["skill_id", "name", "action_id", "steps", "trigger", "enabled", "expected_revision"],
    ),
    "run": _object(
        {"skill_id": KEY, "request_id": KEY, "review_instructions": REVIEW_TEXT},
        ["skill_id", "request_id"],
    ),
    "history": _object({"skill_id": KEY}, ["skill_id"]),
}
HELP = {
    "capabilities": "Discover the current owner’s granted Chitraagni and custom MCP tools. Use plugin_read for read-only tools and plugin_call for a granted external action at any point in the sequence. Use when.path and equals or count_at_least to test the previous plugin result before a step. use_skill composes a saved revision for the same Action.",
    "list": "Read reusable skills saved by the signed-in owner on this installation.",
    "save": "Create or revise a sequence of granted plugin reads, conditional local Task operations, pinned saved skills, local agent reviews and granted custom MCP actions. Use megas_skill_capabilities for plugin inputs. Revision 0 creates a skill. Pause with enabled=false. Triggers support manual, scheduled, task state and local worker turn completion.",
    "run": "Run a saved skill manually. Optional review_instructions focus the named read-only reviewer on a question about the registered project. Retry with the same request_id to return the original run receipt without duplicating saved tasks.",
    "history": "Read the most recent 100 durable local run receipts for this owner's saved skill.",
}


def tools():
    return [
        {
            "name": "megas_skill_" + kind,
            "description": HELP[kind],
            "inputSchema": schema,
            "annotations": {
                "readOnlyHint": kind in {"list", "history", "capabilities"},
                "destructiveHint": kind in {"save", "run"},
                "openWorldHint": kind in {"save", "run"},
            },
        }
        for kind, schema in SCHEMAS.items()
    ]


def _task_matches(document, trigger):
    task = next((t for t in document["tasks"] if t["id"] == trigger["task_id"]), None)
    if task is None or task_requires_project_authorization(TaskRecord.from_dict(task)):
        raise PermissionError("Trigger requires an accessible local task")
    return task["state"] == trigger["state"]


def _rows(document, uid):
    return document.setdefault("action_skills", {}).setdefault(uid, {})


def _matches(value, condition):
    for part in condition["path"]:
        if isinstance(part, str) and isinstance(value, dict) and part in value:
            value = value[part]
        elif type(part) is int and isinstance(value, list) and part < len(value):
            value = value[part]
        else:
            return False
    expected = condition["value"]
    if condition["operator"] == "count_at_least":
        return isinstance(value, (list, dict)) and len(value) >= expected
    return type(value) is type(expected) and value == expected


class ReviewContextChanged(PermissionError):
    pass


def _review_digest(review):
    return hashlib.sha256(json.dumps(review, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _review_message(review):
    if not review:
        return "Review the current registered project changes and summarize the status of the existing tests. Use the configured read-only worker."
    if "objective" in review:
        label = f"Goal {review['goal_id']}, revision {review['goal_revision']}, {review['phase']} phase"
        topic = review["objective"]
        delivery = "the morning brief"
    else:
        label = "Owner's manual review topic"
        topic = review["instructions"]
        delivery = "your reply"
    return (
        "Use the configured read-only worker and its registered repository. "
        "The owner-authored topic below supplies context within that scope. "
        f"For {delivery}, report relevant findings within 1,500 characters, cite repository files or test evidence, "
        "and identify one concrete next opportunity with its supporting evidence. "
        "If the repository supplies insufficient evidence, state what remains unknown."
        f"\n\n{label}\n{topic}"
    )


def _existing_review(receipt, review, guarded):
    binding = receipt.get("review_context")
    has_review = any(s["tool"] == "local_agent_review" for s in receipt["steps"])
    if has_review and (review is not None or guarded and binding):
        if not review or not binding or binding["sha256"] != _review_digest(review):
            raise ReviewContextChanged("Request already belongs to a different review context")
    return receipt


class ActionSkills:
    def __init__(self, *, owner, plugin_call=None, plugins=None, home=None, clock=time.time,
                 last_agent_turn=None, review_mailbox=None):
        """The embedding host supplies live identity and any authorized integrations."""
        if not callable(owner):
            raise TypeError("A verified owner provider is required")
        self.last_agent_turn = last_agent_turn
        self.review_mailbox = review_mailbox
        self.actions = MegasActions(home)
        self.store = self.actions.store
        self.plugin_call = plugin_call
        self.plugins = plugins
        self._verified_owner = owner
        self.clock = clock
        self.home = home
        self._idle_stamp = None

    def owner(self):
        uid = self._verified_owner()
        if not isinstance(uid, str) or not uid.strip() or len(uid) > 128:
            raise PermissionError("Verified owner sign-in required")
        return uid

    def call(self, name, args):
        kind = name.removeprefix("megas_skill_")
        if kind not in SCHEMAS:
            raise KeyError(name)
        try:
            validate(args, SCHEMAS[kind])
        except ValidationError as exc:
            if kind == "run":
                raise ValueError("Invalid saved-skill run arguments") from None
            raise ValueError("Invalid saved-skill arguments: " + exc.message[:300]) from None
        uid = self.owner()
        if kind == "capabilities":
            value = {"plugins": list(self.plugins.discover().values()) if self.plugins else []}
        elif kind == "run":
            review = (
                {"instructions": redact(args["review_instructions"])[:1000]}
                if "review_instructions" in args
                else None
            )
            value = self._run(uid, args["skill_id"], "manual/" + args["request_id"], review=review)
            value = self._deliver(uid, args["skill_id"], value["run_id"])
        else:
            grants = self._plugin_grants(args) if kind == "save" and args["enabled"] else {}
            with self.store._lock():
                document = self.actions._document()
                rows = _rows(document, uid)
                if kind == "save":
                    value = self._save(document, rows, args, grants)
                elif kind == "list":
                    value = {"skills": [row["definition"] for row in rows.values()]}
                else:
                    row = rows[args["skill_id"]]
                    value = {
                        "runs": sorted(row["runs"].values(), key=lambda r: r["time"], reverse=True)[:100]
                    }
        return {
            "content": [{"type": "text", "text": json.dumps(value)}],
            "structuredContent": value,
            "isError": False,
        }

    def _plugin_grants(self, args):
        steps = args["steps"]
        refs = [s for s in steps if s["tool"] in {"plugin_read", "plugin_call"}]
        discovered = self.plugins.discover() if self.plugins and refs else {}
        grants = {}
        for step in refs:
            name = step["name"]
            if name not in discovered:
                raise PermissionError("Plugin tool is not currently granted: " + name)
            if step["tool"] == "plugin_read" and not discovered[name].get("read_only", True):
                raise PermissionError("A mutating custom MCP tool requires a plugin_call step")
            if len(json.dumps(step["arguments"]).encode()) > 16384:
                raise ValueError("Saved plugin input is too large")
            try:
                from .mcp.validation import validate_tool_arguments
                validate_tool_arguments(name, discovered[name]["inputSchema"], step["arguments"])
            except ValueError:
                raise ValueError("Plugin arguments do not match its granted input schema") from None
            grants[name] = discovered[name]["grant"]
        return grants

    def _expanded(self, definition, rows):
        expanded = []

        def extend(current, ancestors):
            if len(ancestors) > 8:
                raise ValueError("Included skill depth exceeds the supported limit")
            for step in current["steps"]:
                tool = step["tool"]
                if tool == "use_skill":
                    child = rows.get(step["skill_id"], {}).get("definition", {})
                    if (
                        step["skill_id"] in ancestors
                        or not child.get("enabled")
                        or child.get("revision") != step["revision"]
                        or child.get("action_id") != definition["action_id"]
                    ):
                        raise ValueError(
                            "Included skill revision is unavailable, recursive, or uses another Action"
                        )
                    extend(child, ancestors | {step["skill_id"]})
                else:
                    if len(expanded) >= 32:
                        raise ValueError("Expanded skill exceeds the supported limit")
                    if tool == "chitraagni_health" and self.plugin_call is None:
                        raise PermissionError("This host has no authorized project health connection")
                    if tool == "local_agent_review" and self.review_mailbox is None:
                        raise PermissionError("This host has no authorized review mailbox")
                    condition = step.get("when")
                    if condition:
                        if not expanded or expanded[-1][0]["tool"] not in {
                            "plugin_read",
                            "plugin_call",
                            "chitraagni_health",
                        }:
                            raise ValueError(
                                "A step condition requires an immediately preceding plugin result"
                            )
                        if condition["operator"] == "count_at_least" and (
                            type(condition["value"]) is not int or condition["value"] < 0
                        ):
                            raise ValueError("A count condition requires a nonnegative integer")
                    expanded.append(
                        (
                            step,
                            current.get("plugin_grants", {}).get(step.get("name")),
                            current["skill_id"],
                            current["revision"],
                        )
                    )

        extend(definition, {definition["skill_id"]})
        return expanded

    def _save(self, document, rows, args, grants):
        key = args["skill_id"]
        old = rows.get(key)
        revision = old["definition"]["revision"] if old else 0
        if revision != args["expected_revision"]:
            raise ValueError("Skill revision conflict")
        if not old and len(rows) >= 100:
            raise ValueError("Saved skill limit reached")
        if not any(a["id"] == args["action_id"] for a in document["actions"]):
            raise ValueError("Unknown action")
        trigger = args["trigger"]
        previous = old and old["definition"]["trigger"] == trigger
        if previous:
            cursor = old["cursor"]
        elif trigger["kind"] == "schedule":
            when = datetime.fromisoformat(trigger["at"])
            if when.utcoffset() is None:
                raise ValueError("A scheduled time requires an explicit UTC offset")
            cursor = when.timestamp()
        elif trigger["kind"] == "task_state":
            cursor = _task_matches(document, trigger)
        elif trigger["kind"] == "agent_turn_completed":
            if self.last_agent_turn is None:
                raise PermissionError("This host has no agent turn observer")
            cursor = self.last_agent_turn(trigger["agent"])
        else:
            cursor = None
        definition = {k: v for k, v in args.items() if k != "expected_revision"}
        definition["revision"] = revision + 1
        definition["plugin_grants"] = grants
        if definition["enabled"]:
            self._expanded(definition, rows)
        history = old["runs"] if old else {}
        for receipt in history.values():
            for output in receipt["steps"]:
                if output.get("state") in {"pending_delivery", "pending_sequence"}:
                    output.update(state="cancelled_before_delivery")
                elif output.get("state") == "dispatching":
                    output.update(state="delivery_unconfirmed")
        rows[key] = {"definition": definition, "cursor": cursor, "runs": history}
        self.store._atomic(self.store.path, document)
        return definition

    def _due(self, document, row, now):
        trigger = row["definition"]["trigger"]
        if not row["definition"]["enabled"]:
            return None
        if trigger["kind"] == "schedule" and row["cursor"] is not None and now >= row["cursor"]:
            return f"scheduled/{row['definition']['revision']}/{row['cursor']}"
        if trigger["kind"] == "agent_turn_completed":
            completed = self.last_agent_turn(trigger["agent"]) if self.last_agent_turn else None
            if completed and completed != row["cursor"]:
                return f"turn/{row['definition']['revision']}/{completed}"
        if trigger["kind"] == "task_state":
            matches = _task_matches(document, trigger)
            if matches and not row["cursor"]:
                return "event/" + str(row["definition"]["revision"]) + "/" + str(len(row["runs"]))
            if not matches and row["cursor"]:
                row["cursor"] = False
                self.store._atomic(self.store.path, document)
        return None

    def run_granted(self, uid, skill_id, expected_revision, request_id, *, guard=None, review_goal=None):
        """Execute a routine's exact grant without expanding it to later skill edits."""
        if self.owner() != uid:
            raise PermissionError("Routine and skill owners differ")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("A granted skill revision is required")
        try:
            validate({"skill_id": skill_id, "request_id": request_id}, SCHEMAS["run"])
            if review_goal is not None:
                validate(review_goal, REVIEW_GOAL)
                review_goal = {**review_goal, "objective": redact(review_goal["objective"])[:1000]}
        except ValidationError:
            raise ValueError("Invalid skill grant or goal review context") from None
        if guard:
            guard()
        receipt = self._run(
            uid,
            skill_id,
            "routine/" + request_id,
            expected_revision=expected_revision,
            guard=guard,
            review=review_goal,
        )
        return self._deliver(uid, skill_id, receipt["run_id"], guard=guard)

    def _run(self, uid, key, request=None, *, expected_revision=None, guard=None, review=None):
        if self.owner() != uid:
            raise PermissionError("Megas owner changed before execution")
        with self.store._lock():
            document = self.actions._document()
            row = _rows(document, uid)[key]
            if expected_revision is not None:
                if (
                    row["definition"]["revision"] != expected_revision
                    or not row["definition"]["enabled"]
                ):
                    raise PermissionError("Saved skill revision is no longer granted")
            if request is not None and request in row["runs"]:
                if (
                    expected_revision is not None
                    and row["runs"][request]["revision"] != expected_revision
                ):
                    raise PermissionError("Request already belongs to a different skill revision")
                return _existing_review(row["runs"][request], review, expected_revision is not None)
            definition = row["definition"]
            if not definition["enabled"]:
                if request is None:
                    return None
                raise PermissionError("Skill is paused")
            run_id = request or self._due(document, row, self.clock())
            if run_id is None:
                return None
            steps = self._expanded(definition, _rows(document, uid))
            has_review = any(s["tool"] == "local_agent_review" for s, *_ in steps)
            if review and "instructions" in review and not has_review:
                raise ValueError("Saved skill has no local agent review")
        # Only read-only plugin operations may precede the atomic local write.
        ordered_from = next(
            (i for i, (step, *_) in enumerate(steps) if step["tool"] == "plugin_call"), None
        )
        previous = MISSING
        prepared = []
        used_grants = {}
        for i, (step, grant, source, revision) in enumerate(steps):
            tool = step["tool"]
            output = {"tool": tool, "skill_id": source, "revision": revision}
            prepared.append(output)
            if ordered_from is not None and i >= ordered_from:
                output["state"] = "pending_sequence"
                if i == ordered_from and step.get("when"):
                    output["condition_matched"] = _matches(previous, step["when"])
                if tool in {"plugin_read", "plugin_call"}:
                    output["name"] = step["name"]
                    used_grants[step["name"]] = grant
                continue
            if step.get("when") and not _matches(previous, step["when"]):
                output["state"] = "condition_not_met"
                previous = MISSING
                continue
            previous = MISSING
            if tool == "plugin_read":
                if not self.plugins or not grant:
                    raise PermissionError("Saved plugin read has no authorized contract")
                name = step["name"]
                result = self.plugins.call(name, step["arguments"], grant)
                if result.get("isError") or "structuredContent" not in result:
                    raise RuntimeError("Plugin read did not return a structured result")
                previous = result["structuredContent"]
                used_grants[name] = grant
                output.update(name=name, status="ok")
                if isinstance(previous, (dict, list)):
                    output["count"] = len(previous)
            elif tool == "chitraagni_health":
                result = self.plugin_call("chitraagni_health", {})
                if (
                    result.get("isError")
                    or (result.get("structuredContent") or {}).get("status") != "ok"
                ):
                    raise RuntimeError("Chitraagni health did not verify")
                previous = result["structuredContent"]
                output["status"] = "ok"
        if self.owner() != uid:
            raise PermissionError("Megas owner changed during run")
        if used_grants:
            current = self.plugins.discover()
            if any(current.get(name, {}).get("grant") != grant for name, grant in used_grants.items()):
                raise PermissionError("A saved plugin grant changed during execution")
        with self.store._lock():
            document = self.actions._document()
            row = _rows(document, uid)[key]
            if (
                row["definition"] != definition
                or self._expanded(definition, _rows(document, uid)) != steps
            ):
                raise ValueError("Saved skill changed before execution")
            if run_id in row["runs"]:
                return _existing_review(row["runs"][run_id], review, expected_revision is not None)
            now = self.clock()
            if request is None and self._due(document, row, now) != run_id:
                return None
            action_id = definition["action_id"]
            if not any(a["id"] == action_id for a in document["actions"]):
                raise ValueError("Unknown action")
            if len(row["runs"]) >= 2000:
                raise ValueError("Saved skill run history limit reached")
            latest_task = None
            for (step, _, _, _), output in zip(steps, prepared):
                if output.get("state") in {"condition_not_met", "pending_sequence"}:
                    continue
                self._apply_local_step(document, action_id, step, output, latest_task)
                if output.get("task_id"):
                    latest_task = output["task_id"]
            receipt = {
                "skill_id": key,
                "revision": definition["revision"],
                "run_id": run_id,
                "time": datetime.fromtimestamp(now, UTC).isoformat(),
                "action_id": action_id,
                "steps": prepared,
            }
            if ordered_from is not None:
                receipt["ordered_from"] = ordered_from
            if review and has_review:
                receipt["review_context"] = {
                    k: v for k, v in review.items() if k not in {"objective", "instructions"}
                }
                receipt["review_context"]["sha256"] = _review_digest(review)
                if self._pending_reviews(prepared):
                    # Keep prose only in the pending outbox and the addressed mailbox.
                    row.setdefault("review_outbox", {})[run_id] = review
            row["runs"][run_id] = receipt
            if request is None:
                interval = definition["trigger"].get("every_seconds")
                if definition["trigger"]["kind"] == "task_state":
                    row["cursor"] = True
                elif definition["trigger"]["kind"] == "agent_turn_completed":
                    row["cursor"] = run_id.split("/", 2)[-1]
                elif interval:
                    start = row["cursor"]
                    row["cursor"] = start + (int((now - start) // interval) + 1) * interval
                else:
                    row["cursor"] = None
            if guard:
                guard()
            self.store._atomic(self.store.path, document)
            return receipt

    @staticmethod
    def _apply_local_step(document, action_id, step, output, latest_task):
        tool = step["tool"]
        if tool == "megas_action_task_create":
            task = append_local_action_task(
                document["tasks"], TaskRecord(title=step["title"], action_id=action_id)
            )
            output["task_id"] = task["id"]
        elif tool == "megas_action_local_tasks":
            output["local_task_count"] = len(local_unscoped_action_tasks(document["tasks"], action_id))
        elif tool == "local_agent_review":
            output.update(
                recipient_agent=step["recipient_agent"], workspace_project=step["workspace_project"],
                state="pending_delivery", task_id=latest_task,
            )

    @staticmethod
    def _pending_reviews(steps):
        return any(
            s["tool"] == "local_agent_review" and s.get("state") in PENDING | {"pending_sequence"}
            for s in steps
        )

    def _deliver(self, uid, key, run_id, *, guard=None):
        receipt = self._deliver_outbox(uid, key, run_id, guard=guard)
        start = receipt.get("ordered_from")
        if start is None or any(
            s["tool"] == "local_agent_review" and s.get("state") not in {"queued", "condition_not_met"}
            for s in receipt["steps"][:start]
        ):
            return receipt
        for index in range(start, len(receipt["steps"])):
            if receipt["steps"][index].get("state") == "pending_sequence":
                receipt = self._sequence_step(uid, key, run_id, index, guard)
            if receipt["steps"][index].get("state") in PENDING:
                receipt = self._deliver_outbox(uid, key, run_id, guard=guard)
            if receipt["steps"][index].get("state") not in {"completed", "queued", "condition_not_met"}:
                break
        if self.owner() != uid:
            raise PermissionError("Megas owner changed during ordered delivery")
        return receipt

    def _permitted_sequence(self, document, uid, key, receipt, index, guard):
        rows = _rows(document, uid)
        definition = rows[key]["definition"]
        if self.owner() != uid or not definition["enabled"] or definition["revision"] != receipt["revision"]:
            raise PermissionError("Saved skill owner or revision changed")
        if not any(action["id"] == receipt["action_id"] for action in document["actions"]):
            raise PermissionError("Saved skill Action is unavailable")
        steps = self._expanded(definition, rows)
        grants = {
            step["name"]: grant
            for (step, grant, _, _), output in zip(steps[:index + 1], receipt["steps"])
            if step["tool"] in {"plugin_read", "plugin_call"} and output.get("state") != "condition_not_met"
        }
        if grants:
            current = self.plugins.discover() if self.plugins else {}
            if any(not grant or current.get(name, {}).get("grant") != grant for name, grant in grants.items()):
                raise PermissionError("Saved plugin grant changed")
        if guard:
            guard()
        return steps

    @staticmethod
    def _sequence_result(receipt, index, state, steps=None, value=MISSING):
        outputs = receipt["steps"]
        outputs[index]["state"] = state
        success = state in {"completed", "condition_not_met"}
        for following in outputs[index + 1:]:
            if success and following.get("state") == "blocked_by_prior_action":
                following["state"] = "pending_sequence"
            elif not success and following.get("state") == "pending_sequence":
                following["state"] = "blocked_by_prior_action"
        if success and steps is not None and index + 1 < len(steps):
            condition = steps[index + 1][0].get("when")
            if condition:
                # Persist only the decision. External output can contain private upstream data.
                outputs[index + 1]["condition_matched"] = _matches(value, condition)

    def _sequence_step(self, uid, key, run_id, index, guard):
        with self.store._lock():
            document = self.actions._document()
            row = _rows(document, uid)[key]
            receipt = row["runs"][run_id]
            output = receipt["steps"][index]
            if output.get("state") != "pending_sequence":
                return receipt
            try:
                steps = self._permitted_sequence(document, uid, key, receipt, index, guard)
            except (KeyError, ValueError, PermissionError):
                self._sequence_result(receipt, index, "cancelled_before_delivery")
                row.get("review_outbox", {}).pop(run_id, None)
                self.store._atomic(self.store.path, document)
                return receipt
            step, grant, _, _ = steps[index]
            tool = step["tool"]
            if step.get("when") and not output.get("condition_matched"):
                self._sequence_result(receipt, index, "condition_not_met", steps)
                if not self._pending_reviews(receipt["steps"]):
                    row.get("review_outbox", {}).pop(run_id, None)
                self.store._atomic(self.store.path, document)
                return receipt
            if tool not in {"plugin_call", "plugin_read", "chitraagni_health"}:
                latest = next((s["task_id"] for s in reversed(receipt["steps"][:index])
                               if s["tool"] == "megas_action_task_create" and s.get("task_id")), None)
                self._apply_local_step(document, receipt["action_id"], step, output, latest)
                if tool != "local_agent_review":
                    self._sequence_result(receipt, index, "completed", steps)
                self.store._atomic(self.store.path, document)
                return receipt
            if tool == "plugin_call":
                # If this operator dies at any point after this durable claim, another
                # client must never spend the grant again or start a dependent step.
                self._sequence_result(receipt, index, "delivery_unconfirmed")
                self.store._atomic(self.store.path, document)

        value = MISSING
        try:
            if self.owner() != uid:
                raise PermissionError("Saved skill owner changed")
            if guard:
                guard()
            if tool == "chitraagni_health":
                result = self.plugin_call("chitraagni_health", {})
            else:
                result = self.plugins.call(step["name"], step["arguments"], grant)
            value = result.get("structuredContent", MISSING)
            state = "failed" if result.get("isError") else "completed"
            if tool == "plugin_read" and value is MISSING:
                state = "failed"
            if tool == "chitraagni_health" and (not isinstance(value, dict) or value.get("status") != "ok"):
                state = "failed"
        except (KeyError, OSError, RuntimeError, ValueError):
            state = "delivery_unconfirmed" if tool == "plugin_call" else "failed"

        with self.store._lock():
            document = self.actions._document()
            row = _rows(document, uid)[key]
            receipt = row["runs"][run_id]
            output = receipt["steps"][index]
            expected = "delivery_unconfirmed" if tool == "plugin_call" else "pending_sequence"
            if output.get("state") != expected:
                return receipt
            try:
                self._permitted_sequence(document, uid, key, receipt, index, guard)
            except (KeyError, ValueError, PermissionError):
                state = "delivery_unconfirmed" if tool == "plugin_call" else "cancelled_before_delivery"
            self._sequence_result(receipt, index, state, steps, value)
            if state == "completed" and tool in {"plugin_read", "chitraagni_health"}:
                output["status"] = "ok"
                if isinstance(value, (dict, list)):
                    output["count"] = len(value)
            if not self._pending_reviews(receipt["steps"]):
                row.get("review_outbox", {}).pop(run_id, None)
            self.store._atomic(self.store.path, document)
            return receipt

    def _deliver_outbox(self, uid, key, run_id, *, guard=None):
        """Replay a committed outbox using the local mailbox's own idempotency key."""

        with self.store._lock():
            row = _rows(self.actions._document(), uid)[key]
            receipt = row["runs"][run_id]
            pending = [(i, s) for i, s in enumerate(receipt["steps"]) if s.get("state") in PENDING]
            if not pending or not row["definition"]["enabled"]:
                return receipt
        for i, step in pending:
            if self.owner() != uid:
                raise PermissionError("Megas owner changed during delivery")
            digest = hashlib.sha256(json.dumps([uid, key, run_id, i]).encode()).hexdigest()
            # Reserve before releasing the task lock. The mailbox captures Git
            # provenance, which can take longer than the task store lock timeout.
            with self.store._lock():
                document = self.actions._document()
                row = _rows(document, uid)[key]
                receipt = row["runs"][run_id]
                current = receipt["steps"][i]
                if not row["definition"]["enabled"] or current.get("state") not in PENDING:
                    continue
                # The handoff may be retried after an included skill was edited
                # or paused. Cancel outstanding delivery under that old grant.
                try:
                    self._expanded(row["definition"], _rows(document, uid))
                except ValueError:
                    self._sequence_result(receipt, i, "cancelled_before_delivery")
                    if not self._pending_reviews(receipt["steps"]):
                        row.get("review_outbox", {}).pop(run_id, None)
                    self.store._atomic(self.store.path, document)
                    continue
                review = row.get("review_outbox", {}).get(run_id)
                if receipt.get("review_context") and (
                    not review or _review_digest(review) != receipt["review_context"]["sha256"]
                ):
                    raise PermissionError("Bound review context unavailable")
                dispatch = {
                    "recipient_agent": step["recipient_agent"],
                    "workspace_project": step["workspace_project"],
                    "message": _review_message(review),
                    "idempotency_key": "saved-skill-" + digest,
                }
                if review and "goal_revision" in review:
                    dispatch["context_revision"] = review["goal_revision"]
                if step.get("task_id"):
                    dispatch["task_id"] = step["task_id"]
                if current.get("retry_at", 0) > self.clock():
                    continue
                if guard:
                    guard()
                current.update(state="dispatching", retry_at=self.clock() + 60)
                self.store._atomic(self.store.path, document)
            if guard:
                guard()
            try:
                if self.review_mailbox is None:
                    raise RuntimeError("Review mailbox unavailable")
                mailbox = self.review_mailbox
                result = mailbox.send_review(uid, dispatch)["structuredContent"]
                outcome = {"state": "queued", "message_id": result["message_id"]}
            except (KeyError, OSError, RuntimeError, ValueError) as error:
                outcome = {
                    "state": "pending_delivery",
                    "last_error": type(error).__name__,
                    "retry_at": self.clock() + 60,
                }
            with self.store._lock():
                document = self.actions._document()
                row = _rows(document, uid)[key]
                receipt = row["runs"][run_id]
                current = receipt["steps"][i]
                if current.get("state") == "queued":
                    continue
                if receipt["revision"] != row["definition"]["revision"] and outcome["state"] != "queued":
                    outcome = {"state": "delivery_unconfirmed"}
                for field in ("last_error", "retry_at"):
                    current.pop(field, None)
                current.update(outcome)
                if not self._pending_reviews(receipt["steps"]):
                    row.get("review_outbox", {}).pop(run_id, None)
                self.store._atomic(self.store.path, document)
        return receipt

    def poll(self):
        if not self.store.path.exists():
            return []
        stat = self.store.path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._idle_stamp:
            return []
        with self.store._lock():
            skills = self.actions._document().get("action_skills", {})
            active = {}
            for uid, rows in skills.items():
                for key, row in rows.items():
                    if not row["definition"]["enabled"]:
                        continue
                    pending = [
                        run_id
                        for run_id, receipt in row["runs"].items()
                        if not run_id.startswith("routine/")
                        and any(s.get("state") in PENDING | {"pending_sequence"} for s in receipt["steps"])
                    ]
                    if pending or row["definition"]["trigger"]["kind"] != "manual":
                        active.setdefault(uid, {})[key] = pending
        if not active:
            self._idle_stamp = stamp
            return []
        uid = self.owner()
        results = []
        for key, pending in active.get(uid, {}).items():
            try:
                for run_id in pending:
                    self._deliver(uid, key, run_id)
                receipt = self._run(uid, key)
                if receipt:
                    results.append(self._deliver(uid, key, receipt["run_id"]))
            except (KeyError, OSError, PermissionError, RuntimeError, ValueError) as error:
                logging.getLogger(__name__).warning("Saved skill deferred (%s)", type(error).__name__)
        return results


def start_skill_worker(skills, *, interval=10, observe_recent_input=None):
    """Poll due work for the current verified owner until the host sets the returned event."""
    from .auto_routines import poll_routines

    if interval <= 0:
        raise ValueError("Poll interval must be positive")
    stop = threading.Event()

    def supervise():
        while not stop.wait(interval):
            for tick in (skills.poll, lambda: poll_routines(skills, observer=observe_recent_input)):
                try:
                    tick()
                except (KeyError, OSError, PermissionError, RuntimeError, ValueError) as error:
                    logging.getLogger(__name__).warning("Scheduled work deferred (%s)", type(error).__name__)

    threading.Thread(target=supervise, name="megas-saved-skills", daemon=True).start()
    return stop
