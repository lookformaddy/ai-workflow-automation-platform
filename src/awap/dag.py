"""DAG ordering for workflow steps.

Steps declare `depends_on`; steps with no explicit dependency implicitly
depend on the previous step in declaration order, so a plain list behaves
like a sequence while still allowing explicit fan-in. `topological_order`
returns execution levels (lists of step ids that may run concurrently).
"""
from __future__ import annotations

from .models import WorkflowDefinition


def _effective_deps(defn: WorkflowDefinition) -> dict[str, list[str]]:
    deps: dict[str, list[str]] = {}
    prev: str | None = None
    for step in defn.steps:
        if step.depends_on is not None:
            deps[step.id] = list(step.depends_on)
        elif prev is not None:
            deps[step.id] = [prev]
        else:
            deps[step.id] = []
        prev = step.id
    return deps


def topological_order(defn: WorkflowDefinition) -> list[list[str]]:
    """Kahn's algorithm grouped into levels. Raises ValueError on cycles."""
    deps = _effective_deps(defn)
    indegree = {sid: len(ds) for sid, ds in deps.items()}
    dependents: dict[str, list[str]] = {sid: [] for sid in deps}
    for sid, ds in deps.items():
        for d in ds:
            if d in dependents:
                dependents[d].append(sid)
    levels: list[list[str]] = []
    ready = sorted(sid for sid, deg in indegree.items() if deg == 0)
    remaining = len(deps)
    while ready:
        levels.append(ready)
        nxt: list[str] = []
        for sid in ready:
            remaining -= 1
            for child in dependents[sid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    nxt.append(child)
        ready = sorted(nxt)
    if remaining:
        raise ValueError("dependency cycle detected among steps")
    return levels


def flat_order(defn: WorkflowDefinition) -> list[str]:
    """Topological order flattened to a single execution sequence."""
    return [sid for level in topological_order(defn) for sid in level]
