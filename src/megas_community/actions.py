"""Megas action containers in the existing local task authority.

Paths are references, not executable commands. Editing never starts execution.
Chitraagni project identities and authorization are independent of action membership.
"""
from __future__ import annotations

import json
from pathlib import Path
import uuid
from jsonschema import validate
from .agent_tasks import AgentTaskStore, TASK_STORE_VERSION, TaskRecord, _now

ID = {"type": "string", "pattern": "^action_[a-f0-9]{32}$"}
DETAILS = {
    "name": {"type": "string", "minLength": 1, "maxLength": 160},
    "description": {"type": "string", "maxLength": 4000},
    "paths": {"type": "array", "maxItems": 64, "uniqueItems": True,
              "items": {"type": "string", "minLength": 1, "maxLength": 4096}},
}


def tools():
    specs = [
        ("list", {}, [], "List local Megas action containers, path references and task counts."),
        ("get", {"action_id": ID}, ["action_id"], "Read one action, its folder/file references and its authorized tasks."),
        ("create", DETAILS, ["name", "description", "paths"], "Create a local Megas action with existing folder/file references. Does not start work."),
        ("update", {**DETAILS, "action_id": ID, "expected_revision": {"type": "integer", "minimum": 1}},
         ["action_id", "expected_revision", "name", "description", "paths"], "Edit an action and its references. Stale revisions fail; tasks remain intact."),
        ("task_create", {"action_id": ID, "title": {"type": "string", "minLength": 1, "maxLength": 4000}},
         ["action_id", "title"], "Create an unassigned local task inside an action. Does not start execution."),
        ("task_move", {"action_id": ID, "task_id": {"type": "string", "pattern": "^task_[a-f0-9]{32}$"}},
         ["action_id", "task_id"], "Move an existing task into an action without changing assignment or Chitraagni authority."),
    ]
    return [{"name": "megas_action_" + suffix, "description": description,
             "inputSchema": {"type": "object", "additionalProperties": False,
                             "properties": properties, "required": required},
             "annotations": {"readOnlyHint": suffix in {"list", "get"}, "destructiveHint": False, "openWorldHint": False}}
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
    def _paths(values):
        paths = []
        seen = set()
        for value in values:
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

    def call(self, name, arguments):
        spec = next((s for s in tools() if s["name"] == name), None)
        if spec is None:
            raise ValueError("unknown action operation")
        validate(arguments, spec["inputSchema"])
        operation = name.removeprefix("megas_action_")
        paths = self._paths(arguments["paths"]) if "paths" in arguments else None
        with self.store._lock():
            document = self._document()
            actions = document["actions"]
            tasks = document["tasks"]
            if operation == "get":
                action = next((a for a in actions if a["id"] == arguments["action_id"]), None)
                if action is None:
                    raise KeyError("unknown action")
                members = [t for t in tasks if t.get("action_id") == action["id"]]
                for task in members:
                    record = TaskRecord.from_dict(task)
                    if record.project_authority_scope:
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
                    label = arguments["name"].strip()
                    if not label:
                        raise ValueError("action name must not be blank")
                    action.update(name=label, description=arguments["description"], paths=paths,
                                  revision=action["revision"] + 1, updated_at=_now())
                    result = dict(action)
                elif operation == "task_create":
                    if len(tasks) >= 2000:
                        raise ValueError("task capacity reached")
                    task = TaskRecord(title=arguments["title"], action_id=action["id"])
                    result = task.to_dict()
                    tasks.append(result)
                else:
                    task = next((t for t in tasks if t["id"] == arguments["task_id"]), None)
                    if task is None:
                        raise KeyError("unknown task")
                    record = TaskRecord.from_dict(task)
                    if record.project_authority_scope:
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
