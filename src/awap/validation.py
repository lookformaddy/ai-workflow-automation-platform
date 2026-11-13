"""Validation of workflow definitions — fail fast, before any run starts.

Checks: duplicate step ids, unknown references (depends_on, branch arms,
parallel branches, loop bodies), cycles, missing configuration per step
type, invalid budgets, and loop caps. Returns a list of human-readable
errors; an empty list means the definition is valid.
"""
from __future__ import annotations

from .models import StepType, WorkflowDefinition

_VALID_STEP_TYPES = {t.value for t in StepType}


def validate_definition(defn: WorkflowDefinition) -> list[str]:
    errors: list[str] = []
    if not defn.id:
        errors.append("workflow id must not be empty")
    if not defn.steps:
        errors.append("workflow must declare at least one step")

    ids = [s.id for s in defn.steps]
    seen: set[str] = set()
    for step_id in ids:
        if step_id in seen:
            errors.append(f"duplicate step id: {step_id}")
        seen.add(step_id)
    id_set = set(ids)

    for step in defn.steps:
        if step.type.value not in _VALID_STEP_TYPES:
            errors.append(f"step {step.id}: unknown type {step.type!r}")
        for dep in step.depends_on or []:
            if dep not in id_set:
                errors.append(f"step {step.id}: unknown depends_on {dep!r}")
            if dep == step.id:
                errors.append(f"step {step.id}: depends on itself")
        if step.timeout_ms <= 0:
            errors.append(f"step {step.id}: timeout_ms must be positive")
        if step.retry.max_attempts < 1:
            errors.append(f"step {step.id}: retry.max_attempts must be >= 1")
        if step.on_error not in ("fail", "continue"):
            errors.append(f"step {step.id}: on_error must be 'fail' or 'continue'")
        errors.extend(_validate_step_config(step, id_set))

    errors.extend(_find_cycles(defn))

    b = defn.budget
    if b.max_tokens <= 0:
        errors.append("budget.max_tokens must be positive")
    if b.max_cost_usd <= 0:
        errors.append("budget.max_cost_usd must be positive")
    if b.max_latency_ms <= 0:
        errors.append("budget.max_latency_ms must be positive")
    if b.max_steps <= 0:
        errors.append("budget.max_steps must be positive")
    if b.max_steps < len(defn.steps):
        errors.append(
            f"budget.max_steps ({b.max_steps}) is smaller than the step count "
            f"({len(defn.steps)}); the run could never complete"
        )
    return errors


def _validate_step_config(step, id_set: set[str]) -> list[str]:
    cfg = step.config
    errors: list[str] = []
    t = step.type
    if t is StepType.LLM:
        if not cfg.get("prompt"):
            errors.append(f"step {step.id}: llm step requires config.prompt")
    elif t is StepType.TOOL:
        if not cfg.get("tool"):
            errors.append(f"step {step.id}: tool step requires config.tool")
    elif t is StepType.APPROVAL:
        if not cfg.get("approvers"):
            errors.append(f"step {step.id}: approval step requires config.approvers")
        if cfg.get("on_expiry", "escalate") not in ("escalate", "deny"):
            errors.append(f"step {step.id}: on_expiry must be 'escalate' or 'deny'")
    elif t is StepType.TRANSFORM:
        if cfg.get("op") not in ("uppercase", "lowercase", "template", "extract", "concat"):
            errors.append(
                f"step {step.id}: transform op must be one of "
                "uppercase/lowercase/template/extract/concat"
            )
        if cfg.get("op") == "template" and not cfg.get("template"):
            errors.append(f"step {step.id}: template transform requires config.template")
        if cfg.get("op") == "extract" and not cfg.get("field"):
            errors.append(f"step {step.id}: extract transform requires config.field")
    elif t is StepType.BRANCH:
        if not cfg.get("condition"):
            errors.append(f"step {step.id}: branch step requires config.condition")
        for arm in ("true_steps", "false_steps"):
            for ref in cfg.get(arm, []):
                if ref not in id_set:
                    errors.append(f"step {step.id}: unknown {arm} reference {ref!r}")
    elif t is StepType.PARALLEL:
        branches = cfg.get("branches")
        if not branches or not isinstance(branches, list) or len(branches) < 2:
            errors.append(f"step {step.id}: parallel step requires >= 2 branches")
        else:
            for branch in branches:
                if not isinstance(branch, list) or not branch:
                    errors.append(f"step {step.id}: each parallel branch must be non-empty")
                    continue
                for ref in branch:
                    if ref not in id_set:
                        errors.append(f"step {step.id}: unknown parallel reference {ref!r}")
    elif t is StepType.LOOP:
        if not cfg.get("items_key"):
            errors.append(f"step {step.id}: loop step requires config.items_key")
        body = cfg.get("body", [])
        if not body:
            errors.append(f"step {step.id}: loop step requires a non-empty config.body")
        for ref in body:
            if ref not in id_set:
                errors.append(f"step {step.id}: unknown loop body reference {ref!r}")
        cap = cfg.get("max_iterations", 50)
        if not isinstance(cap, int) or cap < 1:
            errors.append(f"step {step.id}: max_iterations must be a positive integer")
    return errors


def _find_cycles(defn: WorkflowDefinition) -> list[str]:
    """Detect cycles in the depends_on graph (DFS)."""
    graph = {s.id: list(s.depends_on or []) for s in defn.steps}
    errors: list[str] = []
    state: dict[str, str] = {}  # white (absent) / gray / black

    def visit(node: str, path: list[str]) -> None:
        state[node] = "gray"
        for dep in graph.get(node, []):
            if dep not in graph:
                continue
            if state.get(dep) == "gray":
                cycle = " -> ".join(path + [node, dep])
                errors.append(f"dependency cycle detected: {cycle}")
            elif state.get(dep) != "black":
                visit(dep, path + [node])
        state[node] = "black"

    for step_id in graph:
        if state.get(step_id) != "black":
            visit(step_id, [])
    return errors


def assert_valid(defn: WorkflowDefinition) -> None:
    errors = validate_definition(defn)
    if errors:
        raise ValueError("invalid workflow definition:\n" + "\n".join(errors))
