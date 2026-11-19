"""Megas action containers in the existing local task authority.

Paths are references, not executable commands. Editing never starts execution.
External project identities and authorization are independent of action membership.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath
import uuid
from jsonschema import validate
from .agent_tasks import AgentTaskStore, MAX_TASKS, TASK_STORE_VERSION, TaskRecord, _now
from .action_local_projection import local_unscoped_action_tasks, task_requires_project_authorization

TASK_FIELDS = {
    "title": {"type": "string", "minLength": 1, "maxLength": 4000},
    "description": {"type": "string", "maxLength": 16000},
    "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
    "due_date": {"type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
    "depends_on": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 256}, "uniqueItems": True, "maxItems": 32},
}

ID = {"type": "string", "pattern": "^action_[a-f0-9]{32}$"}
DETAILS = {
    "name": {"type": "string", "minLength": 1, "maxLength": 160},
    "description": {"type": "string", "maxLength": 4000},
    "paths": {"type": "array", "maxItems": 64, "uniqueItems": True,
              "items": {"type": "string", "minLength": 1, "maxLength": 4096}},
}


def append_local_action_task(tasks: list, record: TaskRecord) -> dict:
    """Append in the caller's locked transaction. No assignment or execution side effects."""
    if len(tasks) >= MAX_TASKS:
        raise ValueError("task capacity reached")
    row = record.to_dict()
    tasks.append(row)
    return row


def tools():
    specs = [
        ("list", {}, [], "List local Megas action containers, path references and task counts."),
        ("local_unfiled_tasks", {}, [], "Read only local unscoped tasks without an action. Project-owned tasks remain omitted even when their account is offline."),
        ("get", {"action_id": ID}, ["action_id"], "Read one action, its folder/file references and its authorized tasks."),
        ("local_tasks", {"action_id": ID}, ["action_id"], "Read local unscoped tasks in an action even if the project service is offline; never return project-bound tasks."),
        ("create", DETAILS, ["name", "description", "paths"], "Create a local Megas action with existing folder/file references. Does not start work."),
        ("save_voice_plan", {
            "voice_entry_id": {"type": "string", "pattern": "^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$"},
            "name": DETAILS["name"], "description": DETAILS["description"],
            "task_titles": {"type": "array", "minItems": 1, "maxItems": 40,
                            "items": {"type": "string", "minLength": 1, "maxLength": 4000}}},
         ["voice_entry_id", "name", "description", "task_titles"],
         "Save the Action and Tasks derived from a user-selected Megas voice note together. "
         "The opaque voice entry ID makes retries return the existing plan. Does not start tasks."),
        ("update", {**DETAILS, "action_id": ID, "expected_revision": {"type": "integer", "minimum": 1}},
         ["action_id", "expected_revision", "name", "description", "paths"], "Edit an action and its references. Stale revisions fail; tasks remain intact."),
        ("task_create", {"action_id": ID, "title": {"type": "string", "minLength": 1, "maxLength": 4000}},
         ["action_id", "title"], "Create an unassigned local task inside an action. Does not start execution."),
        ("task_move", {"action_id": ID, "task_id": {"type": "string", "pattern": "^task_[a-f0-9]{32}$"}},
         ["action_id", "task_id"], "Move an existing task into an action without changing assignment or project authority."),
        ("task_update", {"task_id": {"type": "string", "pattern": "^task_[a-f0-9]{32}$"},
             "expected_updated_at": {"type": "string", "minLength": 1, "maxLength": 80},
             **TASK_FIELDS, "state": {"type": "string", "enum": ["open", "paused", "waiting_approval", "completed", "cancelled"]},
             "completion_note": {"type": "string", "maxLength": 4000}},
         ["task_id", "expected_updated_at"], "Edit saved task details after checking the last update timestamp. Preserves assignment, history and context. Completion requires evidence and completed dependencies; editing never executes work."),
    ]
    return [{"name": "megas_action_" + suffix, "description": description,
             "inputSchema": {"type": "object", "additionalProperties": False,
                             "properties": properties, "required": required},
             "annotations": {"readOnlyHint": suffix in {"list", "get", "local_tasks", "local_unfiled_tasks"}, "destructiveHint": False, "openWorldHint": False}}
            for suffix, properties, required, description in specs]


class MegasActions:
    def __init__(self, home=None, *, authorize=None):
        self.store = AgentTaskStore(home)
        self.authorize = authorize

    def _document(self):
        # Validate legacy task data before any write; never discard it on errors.
        self.store._load_unlocked()
        value = json.loads(self.store.path.read_text(encoding="utf-8")) if self.store.path.exists() else {
            "version": TASK_STORE_VERSION, "tasks": [], "projects": []}
        actions = value.setdefault("actions", [])
        if not isinstance(actions, list) or len(actions) > 500 or any(not isinstance(a, dict) for a in actions):
            raise ValueError("malformed action registry")
        return value

    @staticmethod
    def _paths(values, *, existing=()):
        paths = []
        seen = set()
        # Imported references are historical data, not usable Linux paths.
        # Retain only exact references already attached to this same action;
        # adding or changing a reference still requires an existing local path.
        previous = {p['path']: p for p in existing
                    if isinstance(p, dict) and isinstance(p.get('path'), str)}
        for value in values:
            if os.name != 'nt' and PureWindowsPath(value).is_absolute() and value in previous:
                key = value.casefold()
                if key not in seen:
                    paths.append(dict(previous[value]))
                    seen.add(key)
                continue
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError("action references must be absolute local paths")
            path = path.resolve(strict=True)
            if not (path.is_file() or path.is_dir()):
                raise ValueError("action references must be files or folders")
            key = str(path).casefold()
            if key in seen:
                continue
            seen.add(key)
            paths.append({"path": str(path), "kind": "folder" if path.is_dir() else "file"})
        return paths

    def _save_voice_plan(self, document, arguments):
        actions = document["actions"]
        source = arguments["voice_entry_id"]
        action = next((a for a in actions if a.get("voice_entry_id") == source), None)
        if action is None:
            if len(actions) >= 500:
                raise ValueError("action capacity reached")
            label = arguments["name"].strip()
            if not label:
                raise ValueError("action name must not be blank")
            now = _now()
            action = {"id": "action_" + uuid.uuid4().hex, "voice_entry_id": source, "name": label,
                      "description": arguments["description"], "paths": [],
                      "created_at": now, "updated_at": now, "revision": 1}
            task_ids = []
            for title in arguments["task_titles"]:
                row = append_local_action_task(document["tasks"], TaskRecord(title=title, action_id=action["id"]))
                task_ids.append(row["id"])
            document.setdefault("voice_plan_tasks", {})[source] = task_ids
            actions.append(action)
            if len(json.dumps(actions, ensure_ascii=False).encode("utf-8")) > 128 * 1024:
                raise ValueError("action references exceed the supported registry byte limit")
            self.store._atomic(self.store.path, document)
        members = local_unscoped_action_tasks(document["tasks"], action["id"])
        original = set(document["voice_plan_tasks"][source])
        return {"action_id": action["id"], "voice_entry_id": source,
                "task_ids": [t["id"] for t in members if t["id"] in original]}

    def call(self, name, arguments):
        spec = next((s for s in tools() if s["name"] == name), None)
        if spec is None:
            raise ValueError("unknown action operation")
        validate(arguments, spec["inputSchema"])
        operation = name.removeprefix("megas_action_")
        original = None
        if operation == 'task_update':
            original = self.store.get(arguments['task_id'])
            if task_requires_project_authorization(original):
                if self.authorize is None:
                    raise PermissionError('linked task access must be verified')
                self.authorize(original)
        with self.store._lock():
            document = self._document()
            actions = document["actions"]
            tasks = document["tasks"]
            if operation == 'save_voice_plan':
                result = self._save_voice_plan(document, arguments)
            elif operation == 'local_unfiled_tasks':
                result = {'tasks': local_unscoped_action_tasks(tasks, None), 'view': 'local_unscoped_only'}
            elif operation == 'task_update':
                task = next((row for row in tasks if row['id'] == arguments['task_id']), None)
                if task is None:
                    raise KeyError('unknown task')
                if (task.get('project'), task.get('project_authority_scope')) != (original.project, original.project_authority_scope):
                    raise RuntimeError('task authority changed; refresh before editing')
                if task['updated_at'] != arguments['expected_updated_at']:
                    raise RuntimeError('task revision conflict; refresh before editing')
                changes = {k: v for k, v in arguments.items() if k in TASK_FIELDS or k in ('state', 'completion_note')}
                if not changes:
                    raise ValueError('provide at least one task detail to edit')
                candidate = TaskRecord.from_dict({**task, **changes})
                from .task_dependencies import validate_task_dependencies
                records = [candidate if row['id'] == candidate.id else TaskRecord.from_dict(row) for row in tasks]
                validate_task_dependencies(records, roots=[candidate])
                by_id = {row.id: row for row in records}
                if candidate.state == 'completed' and (not candidate.completion_note.strip() or
                        any(by_id[identifier].state != 'completed' for identifier in candidate.depends_on)):
                    raise ValueError('completion requires a note and completed dependencies')
                if 'title' in changes:
                    changes['title'] = candidate.title
                task.update(**changes, updated_at=_now())
                self.store._atomic(self.store.path, document)
                result = dict(task)
            elif operation in {"get", "local_tasks"}:
                action = next((a for a in actions if a["id"] == arguments["action_id"]), None)
                if action is None:
                    raise KeyError("unknown action")
                if operation == "local_tasks":
                    # Explicit local-only read during a project-service outage. Never call
                    # the remote authorizer or include any project-associated task.
                    # This is a projection of the canonical validated task store.
                    result = {"action": dict(action),
                              "tasks": local_unscoped_action_tasks(tasks, action["id"]),
                              "view": "local_unscoped_only"}
                else:
                    members = [t for t in tasks if t.get("action_id") == action["id"]]
                    for task in members:
                        record = TaskRecord.from_dict(task)
                        # Older project-bound tasks may lack an authority scope.
                        # Fail closed instead of exposing their details.
                        if task_requires_project_authorization(record):
                            if self.authorize is None:
                                raise PermissionError("linked task access must be verified")
                            self.authorize(record)
                    result = {"action": dict(action), "tasks": members}
            elif operation == "list":
                result = {"actions": [{**action,
                    "task_count": sum(t.get("action_id") == action["id"] for t in tasks),
                    "active_task_count": sum(t.get("action_id") == action["id"] and t["state"] not in {"completed", "cancelled"} for t in tasks)}
                    for action in actions], "unfiled_task_count": sum(not t.get("action_id") for t in tasks)}
            else:
                action = next((a for a in actions if a["id"] == arguments.get("action_id")), None)
                if operation == "create":
                    if len(actions) >= 500:
                        raise ValueError("action capacity reached")
                    action = {"id": "action_" + uuid.uuid4().hex, "created_at": _now(), "revision": 0}
                    actions.append(action)
                elif action is None:
                    raise KeyError("unknown action")
                if operation in {"create", "update"}:
                    if operation == "update" and action["revision"] != arguments["expected_revision"]:
                        raise RuntimeError("action revision conflict; refresh before editing")
                    paths = self._paths(arguments['paths'], existing=action.get('paths', ()))
                    label = arguments["name"].strip()
                    if not label:
                        raise ValueError("action name must not be blank")
                    action.update(name=label, description=arguments["description"], paths=paths,
                                  revision=action["revision"] + 1, updated_at=_now())
                    result = dict(action)
                elif operation == "task_create":
                    result = append_local_action_task(tasks, TaskRecord(title=arguments["title"], action_id=action["id"]))
                else:
                    task = next((t for t in tasks if t["id"] == arguments["task_id"]), None)
                    if task is None:
                        raise KeyError("unknown task")
                    record = TaskRecord.from_dict(task)
                    if task_requires_project_authorization(record):
                        if self.authorize is None:
                            raise PermissionError("linked task access must be verified")
                        self.authorize(record)
                    task["action_id"] = action["id"]
                    task["updated_at"] = _now()
                    result = dict(task)
                if len(json.dumps(actions, ensure_ascii=False).encode("utf-8")) > 128 * 1024:
                    raise ValueError("action references exceed the supported registry byte limit")
                self.store._atomic(self.store.path, document)
        value = {**result, "execution_started": False}
        return {"content": [{"type": "text", "text": json.dumps(value)}],
                "structuredContent": value, "isError": False}
