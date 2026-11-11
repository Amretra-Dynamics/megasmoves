"""Shared dependency validation for operational tasks, independent of UI."""


def validate_task_dependencies(tasks, *, roots=None):
    by_id = {task.id: task for task in tasks}
    visited = set()
    for root in tasks if roots is None else roots:
        visiting = set()
        stack = [(root, False)]
        while stack:
            task, exiting = stack.pop()
            if exiting:
                visiting.remove(task.id)
                visited.add(task.id)
                continue
            if task.id in visiting:
                raise ValueError('todo dependencies contain a cycle')
            if task.id in visited:
                continue
            visiting.add(task.id)
            stack.append((task, True))
            for identifier in reversed(task.depends_on):
                dependency = by_id.get(identifier)
                if dependency is None or (dependency.project, dependency.project_authority_scope) != (task.project, task.project_authority_scope):
                    raise ValueError('dependency must be an existing todo in the same project and account scope')
                stack.append((dependency, False))
