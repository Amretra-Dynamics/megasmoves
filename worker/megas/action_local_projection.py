"""Safe, read-only local-only task projection for mixed Megas Actions.

Project-owned task metadata is omitted even if the linked account is offline,
including older tasks lacking project_authority_scope. This function never checks
or circumvents authorization: it returns only tasks with no project binding.
"""
from __future__ import annotations

from typing import Any, Iterable


def local_unscoped_action_tasks(tasks: Iterable[dict[str, Any]], action_id: str | None) -> list[dict[str, Any]]:
    """Return copies of tasks bound to *action_id* with no project or account scope.

    Caller must first validate the entire durable store. This projection fails
    closed on any non-null project or project_authority_scope, including legacy
    project records with no account scope and cross-account tasks.
    """
    return [
        dict(task)
        for task in tasks
        if task.get("action_id") == action_id
        and task.get("project") is None
        and task.get("project_authority_scope") is None
    ]

def task_requires_project_authorization(task: Any) -> bool:
    """Both project identity and account scope independently require authorization.

    The older project-only records must not be treated as locally unscoped.
    Never check truthiness because empty or malformed non-null bindings must
    also remain protected.
    """
    return task.project is not None or task.project_authority_scope is not None
