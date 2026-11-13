"""Durable run engine.

Executes a validated workflow definition step by step with:
- checkpoints after every step (resume after a crash or an approval pause),
- retries with simulated exponential backoff,
- per-step timeouts (simulated durations from providers),
- human approval gates with expiry + escalation,
- fail-closed per-workflow and per-tenant budgets,
- idempotent run starts,
- a hash-chained audit trail of every transition.

Time is read through a `Clock` so tests can drive expiry deterministically.
Provider durations are simulated (no real sleeping); backoff delays are
recorded on the step run, not slept.
"""
from __future__ import annotations

import ast
import time
import uuid
from typing import Any, Callable

from .audit import AuditLog
from .budgets import BudgetExceeded, BudgetManager, ScopedBudgets
from .config import Settings
from .dag import flat_order
from .models import (
    Approval,
    ApprovalStatus,
    Budget,
    Run,
    RunStatus,
    Step,
    StepRun,
    StepStatus,
    StepType,
    WorkflowDefinition,
)
from .providers import ProviderError, ProviderResult, ScriptedLLMProvider, ScriptedToolProvider, TransformProvider
from .store import WorkflowStore
from .validation import assert_valid


class Clock:
    """Wall clock in milliseconds (real time by default)."""

    def now_ms(self) -> int:
        return int(time.time() * 1000)


class VirtualClock(Clock):
    """Deterministic clock for tests and demos."""

    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self._now = start_ms

    def now_ms(self) -> int:
        return self._now

    def advance(self, ms: int) -> None:
        self._now += ms

    def advance_s(self, seconds: float) -> None:
        self._now += int(seconds * 1000)


class StepFailure(Exception):
    def __init__(self, message: str, timed_out: bool = False):
        self.timed_out = timed_out
        super().__init__(message)


class _Paused(Exception):
    """Raised internally when a run pauses at an approval gate."""


# -- safe condition evaluation -------------------------------------------
_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare,
    ast.Name, ast.Constant, ast.Subscript, ast.Load,
    ast.And, ast.Or, ast.Not, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
    ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Add, ast.Sub, ast.Mult,
    ast.Div, ast.Mod, ast.USub,
)


def evaluate_condition(expression: str, context: dict[str, Any]) -> bool:
    """Evaluate a small, safe boolean expression against the run context.

    Supports names from the context, comparisons, boolean ops and simple
    arithmetic. Anything else (calls, attribute access, imports) is
    rejected — conditions are workflow-author supplied, not code.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid branch condition {expression!r}: {exc}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"condition {expression!r} uses unsupported syntax: {type(node).__name__}")
    return bool(_eval_node(tree.body, context))


def _eval_node(node: ast.AST, context: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in context:
            raise ValueError(f"condition references unknown name: {node.id}")
        return context[node.id]
    if isinstance(node, ast.Subscript):
        container = _eval_node(node.value, context)
        key = _eval_node(node.slice, context)
        return container[key]
    if isinstance(node, ast.BoolOp):
        values = [_eval_node(v, context) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _eval_node(node.operand, context)
        if isinstance(node.op, ast.USub):
            return -_eval_node(node.operand, context)
        raise ValueError("unsupported unary operator")
    if isinstance(node, ast.BinOp):
        left, right = _eval_node(node.left, context), _eval_node(node.right, context)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.Mod):
            return left % right
        raise ValueError("unsupported binary operator")
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, context)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval_node(comparator, context)
            if isinstance(op, ast.Eq) and not left == right:
                return False
            if isinstance(op, ast.NotEq) and not left != right:
                return False
            if isinstance(op, ast.Lt) and not left < right:
                return False
            if isinstance(op, ast.LtE) and not left <= right:
                return False
            if isinstance(op, ast.Gt) and not left > right:
                return False
            if isinstance(op, ast.GtE) and not left >= right:
                return False
            if isinstance(op, ast.In) and not left in right:
                return False
            if isinstance(op, ast.NotIn) and not left not in right:
                return False
            left = right
        return True
    raise ValueError(f"unsupported condition node: {type(node).__name__}")


class Engine:
    def __init__(
        self,
        store: WorkflowStore | None = None,
        audit: AuditLog | None = None,
        llm: ScriptedLLMProvider | None = None,
        tools: ScriptedToolProvider | None = None,
        settings: Settings | None = None,
        clock: Clock | None = None,
        budget_manager: BudgetManager | None = None,
    ) -> None:
        self.store = store or WorkflowStore()
        self.audit = audit or AuditLog()
        self.llm = llm or ScriptedLLMProvider()
        self.tools = tools or ScriptedToolProvider()
        self.settings = settings or Settings.from_env()
        self.clock = clock or Clock()
        self.budget_manager = budget_manager or BudgetManager()

    # -- run lifecycle ----------------------------------------------------
    def start_run(
        self,
        defn: WorkflowDefinition,
        input_data: dict[str, Any] | None = None,
        idempotency_key: str = "",
        triggered_by: str = "manual",
    ) -> Run:
        assert_valid(defn)
        self.store.put(defn)  # runs must be resumable from the stored definition
        key = f"{defn.tenant}:{defn.id}:{defn.version}:{idempotency_key}" if idempotency_key else ""
        if key and key in self.store.idempotency:
            existing = self.store.get_run(self.store.idempotency[key])
            if existing is not None:
                self.audit.append(self.clock.now_ms(), existing.run_id,
                                  "run_duplicate_start",
                                  {"idempotency_key": idempotency_key})
                return existing
        run = Run(
            run_id=f"run_{uuid.uuid4().hex[:12]}",
            workflow_id=defn.id,
            workflow_version=defn.version,
            tenant=defn.tenant,
            status=RunStatus.PENDING,
            input_data=dict(input_data or {}),
            context=dict(input_data or {}),
            idempotency_key=idempotency_key,
            started_at_ms=self.clock.now_ms(),
        )
        if key:
            self.store.idempotency[key] = run.run_id
        self.store.save_run(run)
        self.audit.append(self.clock.now_ms(), run.run_id, "run_started",
                          {"workflow_id": defn.id, "version": defn.version,
                           "triggered_by": triggered_by})
        return self._execute(run, defn)

    def resume(self, run_id: str) -> Run:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        if run.status not in (RunStatus.WAITING_APPROVAL, RunStatus.PENDING, RunStatus.RUNNING):
            raise ValueError(f"run {run_id} is {run.status.value}; cannot resume")
        defn = self.store.get(run.workflow_id, run.workflow_version)
        if defn is None:
            raise KeyError(f"workflow {run.workflow_id} v{run.workflow_version} not found")
        self.audit.append(self.clock.now_ms(), run_id, "run_resumed",
                          {"checkpoint_step_index": run.checkpoint_step_index})
        return self._execute(run, defn)

    def approve(self, run_id: str, step_id: str, approver: str,
                decision: str, note: str = "") -> Run:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        approval = self.store.get_approval(run_id, step_id)
        if approval is None:
            raise KeyError(f"no approval pending for run {run_id} step {step_id}")
        if approval.status not in (ApprovalStatus.PENDING, ApprovalStatus.ESCALATED):
            raise ValueError(f"approval for {step_id} is already {approval.status.value}")
        if approver not in approval.approvers:
            raise ValueError(f"{approver} is not an approver for step {step_id}")
        now = self.clock.now_ms()
        if decision == "approve":
            approval.status = ApprovalStatus.APPROVED
            approval.decided_by = approver
            approval.decided_at_ms = now
            approval.note = note
            self.store.save_approval(approval)
            self.audit.append(now, run_id, "approval_decided",
                              {"step_id": step_id, "decision": "approved", "by": approver})
            sr = run.step_run(step_id)
            if sr is not None:
                sr.status = StepStatus.SUCCEEDED
                sr.output = {"approved_by": approver, "note": note}
                sr.ended_at_ms = now
            run.status = RunStatus.RUNNING
            # Advance past the approval step.
            run.checkpoint_step_index += 1
            self.store.save_run(run)
            defn = self.store.get(run.workflow_id, run.workflow_version)
            assert defn is not None
            return self._execute(run, defn)
        if decision == "deny":
            approval.status = ApprovalStatus.DENIED
            approval.decided_by = approver
            approval.decided_at_ms = now
            approval.note = note
            self.store.save_approval(approval)
            self.audit.append(now, run_id, "approval_decided",
                              {"step_id": step_id, "decision": "denied", "by": approver})
            sr = run.step_run(step_id)
            if sr is not None:
                sr.status = StepStatus.FAILED
                sr.error = f"denied by {approver}: {note}"
                sr.ended_at_ms = now
            run.status = RunStatus.FAILED
            run.error = f"approval denied at step {step_id} by {approver}"
            run.ended_at_ms = now
            self.store.save_run(run)
            self.audit.append(now, run_id, "run_failed", {"error": run.error})
            return run
        raise ValueError("decision must be 'approve' or 'deny'")

    def cancel(self, run_id: str) -> Run:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        if run.status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
            raise ValueError(f"run {run_id} is already {run.status.value}")
        run.status = RunStatus.CANCELLED
        run.ended_at_ms = self.clock.now_ms()
        self.store.save_run(run)
        self.audit.append(run.ended_at_ms, run_id, "run_cancelled", {})
        return run

    def sweep_expired(self) -> list[Approval]:
        """Expire overdue approvals; escalate or auto-deny per step policy."""
        now = self.clock.now_ms()
        expired: list[Approval] = []
        for approval in self.store.pending_approvals():
            if approval.expires_at_ms and now > approval.expires_at_ms:
                run = self.store.get_run(approval.run_id)
                defn = self.store.get(run.workflow_id, run.workflow_version) if run else None
                step = defn.step_by_id(approval.step_id) if defn else None
                policy = (step.config.get("on_expiry", self.settings.approval_on_expiry)
                          if step else self.settings.approval_on_expiry)
                if policy == "deny":
                    approval.status = ApprovalStatus.EXPIRED
                    self.store.save_approval(approval)
                    self.audit.append(now, approval.run_id, "approval_expired",
                                      {"step_id": approval.step_id, "policy": "deny"})
                    if run is not None:
                        sr = run.step_run(approval.step_id)
                        if sr is not None:
                            sr.status = StepStatus.FAILED
                            sr.error = "approval expired; policy=deny"
                            sr.ended_at_ms = now
                        run.status = RunStatus.FAILED
                        run.error = (f"approval expired at step {approval.step_id}; "
                                     "policy=deny")
                        run.ended_at_ms = now
                        self.store.save_run(run)
                        self.audit.append(now, run.run_id, "run_failed",
                                          {"error": run.error})
                    expired.append(approval)
                else:
                    escalate_to = step.config.get("escalate_to", []) if step else []
                    approval.status = ApprovalStatus.ESCALATED
                    approval.escalation_count += 1
                    approval.escalated_to = list(escalate_to)
                    approval.approvers = list(escalate_to) or approval.approvers
                    window = step.config.get("expires_in_s",
                                             self.settings.approval_default_expiry_s) if step else self.settings.approval_default_expiry_s
                    approval.expires_at_ms = now + int(window) * 1000
                    self.store.save_approval(approval)
                    self.audit.append(now, approval.run_id, "approval_escalated",
                                      {"step_id": approval.step_id,
                                       "escalated_to": escalate_to,
                                       "count": approval.escalation_count})
                    expired.append(approval)
        return expired

    # -- execution ----------------------------------------------------------
    def _execute(self, run: Run, defn: WorkflowDefinition) -> Run:
        run.status = RunStatus.RUNNING
        order = flat_order(defn)
        budgets = self.budget_manager.tracker(run.tenant, defn.id, defn.budget)
        # The tenant tracker persists across runs (tenant-wide cap) and is
        # never reset here. The workflow tracker is fresh per run, so on
        # resume restore this run's prior usage into it.
        budgets.workflow.tokens_used = run.total_tokens
        budgets.workflow.cost_usd = run.total_cost_usd
        budgets.workflow.latency_ms = run.total_latency_ms
        budgets.workflow.steps_used = len([s for s in run.step_runs
                                           if s.status == StepStatus.SUCCEEDED])
        try:
            for idx in range(run.checkpoint_step_index, len(order)):
                step_id = order[idx]
                step = defn.step_by_id(step_id)
                assert step is not None
                existing = run.step_run(step_id)
                if existing is not None and existing.status in (
                        StepStatus.SUCCEEDED, StepStatus.SKIPPED, StepStatus.FAILED):
                    run.checkpoint_step_index = idx + 1
                    continue
                run.current_step_index = idx
                self._execute_step(run, defn, step, budgets)
                run.checkpoint_step_index = idx + 1
                self.store.save_run(run)
        except _Paused:
            run.status = RunStatus.WAITING_APPROVAL
            self.store.save_run(run)
            return run
        except StepFailure as exc:
            run.status = RunStatus.FAILED
            run.error = str(exc)
            run.ended_at_ms = self.clock.now_ms()
            self.store.save_run(run)
            self.audit.append(run.ended_at_ms, run.run_id, "run_failed",
                              {"error": run.error})
            return run
        except BudgetExceeded as exc:
            run.status = RunStatus.FAILED
            run.error = str(exc)
            run.ended_at_ms = self.clock.now_ms()
            self.store.save_run(run)
            self.audit.append(run.ended_at_ms, run.run_id, "budget_exceeded",
                              {"error": str(exc)})
            self.audit.append(run.ended_at_ms, run.run_id, "run_failed",
                              {"error": run.error})
            return run
        run.status = RunStatus.SUCCEEDED
        run.ended_at_ms = self.clock.now_ms()
        self.store.save_run(run)
        self.audit.append(run.ended_at_ms, run.run_id, "run_succeeded",
                          {"steps": len(run.step_runs), "tokens": run.total_tokens,
                           "cost_usd": round(run.total_cost_usd, 6)})
        return run

    def _execute_step(self, run: Run, defn: WorkflowDefinition, step: Step,
                      budgets: ScopedBudgets, force: bool = False) -> StepRun:
        existing = run.step_run(step.id)
        if existing is not None and not force and existing.status in (
                StepStatus.SUCCEEDED, StepStatus.SKIPPED):
            return existing
        sr = existing if (existing is not None and force) else StepRun(step_id=step.id)
        if existing is None:
            run.step_runs.append(sr)
        if force and existing is not None:
            # Loop re-execution accumulates into the same record.
            if not isinstance(sr.output, list):
                sr.output = [sr.output] if sr.output is not None else []
        sr.status = StepStatus.RUNNING
        sr.started_at_ms = self.clock.now_ms()

        if step.type is StepType.APPROVAL:
            self._request_approval(run, defn, step, sr)
            raise _Paused

        if step.type is StepType.BRANCH:
            self._execute_branch(run, defn, step, sr)
            return sr
        if step.type is StepType.PARALLEL:
            self._execute_parallel(run, defn, step, budgets, sr)
            return sr
        if step.type is StepType.LOOP:
            self._execute_loop(run, defn, step, budgets, sr)
            return sr

        # Executable step: llm / tool / transform, with retry + timeout.
        try:
            self._execute_with_retry(run, step, budgets, sr)
        except StepFailure as exc:
            sr.status = StepStatus.FAILED
            sr.error = str(exc)
            sr.ended_at_ms = self.clock.now_ms()
            self.audit.append(sr.ended_at_ms, run.run_id, "step_failed",
                              {"step_id": step.id, "error": str(exc),
                               "attempts": sr.attempts})
            if step.on_error == "fail":
                run.error = f"step {step.id} failed: {exc}"
                raise
            return sr
        return sr

    def _execute_with_retry(self, run: Run, step: Step,
                            budgets: ScopedBudgets, sr: StepRun) -> None:
        policy = step.retry
        delays = policy.delays_ms()
        last_error: Exception | None = None
        try:
            return self._attempt_loop(run, step, budgets, sr, policy, delays)
        except BudgetExceeded as exc:
            sr.status = StepStatus.FAILED
            sr.error = str(exc)
            sr.ended_at_ms = self.clock.now_ms()
            raise

    def _attempt_loop(self, run: Run, step: Step,
                      budgets: ScopedBudgets, sr: StepRun,
                      policy, delays: list[int]) -> None:
        last_error: Exception | None = None
        for attempt in range(1, policy.max_attempts + 1):
            sr.attempts += 1
            # Fail-closed budget check before the attempt.
            est_tokens = 0
            if step.type is StepType.LLM:
                est_tokens = max(1, len(str(step.config.get("prompt", ""))) // 4)
            budgets.check_before_step(est_tokens=est_tokens)
            try:
                result = self._run_step_attempt(step, run.context)
            except ProviderError as exc:
                last_error = exc
                if attempt < policy.max_attempts:
                    sr.backoff_delays_ms.append(delays[attempt - 1])
                    self.audit.append(self.clock.now_ms(), run.run_id, "step_retried",
                                      {"step_id": step.id, "attempt": attempt,
                                       "error": str(exc),
                                       "backoff_ms": delays[attempt - 1]})
                    continue
                raise StepFailure(f"provider failed after {attempt} attempts: {exc}") from exc
            if result.duration_ms > step.timeout_ms:
                last_error = StepFailure(
                    f"step exceeded timeout ({result.duration_ms}ms > {step.timeout_ms}ms)",
                    timed_out=True)
                if attempt < policy.max_attempts:
                    sr.backoff_delays_ms.append(delays[attempt - 1])
                    self.audit.append(self.clock.now_ms(), run.run_id, "step_retried",
                                      {"step_id": step.id, "attempt": attempt,
                                       "error": str(last_error),
                                       "backoff_ms": delays[attempt - 1]})
                    continue
                raise last_error
            # Success: record usage and output.
            cost = self._cost_of(step, result)
            budgets.record(tokens=result.tokens_in + result.tokens_out,
                           cost_usd=cost, latency_ms=result.duration_ms)
            budgets.check_after_step()
            run.total_tokens += result.tokens_in + result.tokens_out
            run.total_cost_usd += cost
            run.total_latency_ms += result.duration_ms
            sr.duration_ms += result.duration_ms
            sr.tokens += result.tokens_in + result.tokens_out
            sr.cost_usd += cost
            sr.status = StepStatus.SUCCEEDED
            sr.ended_at_ms = self.clock.now_ms()
            if isinstance(sr.output, list):
                sr.output.append(result.output)
            else:
                sr.output = result.output
            out_key = step.config.get("output_key") or step.output_key
            if out_key:
                run.context[out_key] = result.output
            self.audit.append(sr.ended_at_ms, run.run_id, "step_succeeded",
                              {"step_id": step.id, "attempts": sr.attempts,
                               "tokens": result.tokens_in + result.tokens_out})
            return
        raise StepFailure(str(last_error) if last_error else "step failed")

    def _run_step_attempt(self, step: Step, context: dict[str, Any]) -> ProviderResult:
        if step.type is StepType.LLM:
            prompt = str(step.config.get("prompt", ""))
            try:
                prompt = prompt.format(**{k: v for k, v in context.items()
                                          if isinstance(v, (str, int, float, bool))})
            except KeyError:
                pass  # unresolved placeholders stay literal
            return self.llm.complete(prompt, model=str(step.config.get("model", "scripted")))
        if step.type is StepType.TOOL:
            args = dict(step.config.get("args", {}))
            # Resolve "{key}" placeholders from context.
            resolved = {}
            for k, v in args.items():
                if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
                    resolved[k] = context.get(v[1:-1], v)
                else:
                    resolved[k] = v
            return self.tools.call(str(step.config["tool"]), resolved)
        if step.type is StepType.TRANSFORM:
            output = TransformProvider.apply(str(step.config["op"]), context, step.config)
            return ProviderResult(output=output, duration_ms=5)
        raise StepFailure(f"unsupported executable step type: {step.type}")

    def _cost_of(self, step: Step, result: ProviderResult) -> float:
        if step.type is StepType.LLM:
            s = self.settings
            return (result.tokens_in / 1000) * s.llm_input_cost_per_1k + \
                   (result.tokens_out / 1000) * s.llm_output_cost_per_1k
        return 0.0

    # -- control flow -------------------------------------------------------
    def _execute_branch(self, run: Run, defn: WorkflowDefinition, step: Step, sr: StepRun) -> None:
        taken = evaluate_condition(str(step.config["condition"]), run.context)
        arm = "true_steps" if taken else "false_steps"
        other = "false_steps" if taken else "true_steps"
        for ref in step.config.get(other, []):
            target = defn.step_by_id(ref)
            if target is None:
                continue
            skipped = run.step_run(ref)
            if skipped is None:
                skipped = StepRun(step_id=ref, status=StepStatus.SKIPPED)
                run.step_runs.append(skipped)
            else:
                skipped.status = StepStatus.SKIPPED
            skipped.output = f"skipped: branch {step.id} took {'false' if taken else 'true'} arm"
        sr.status = StepStatus.SUCCEEDED
        sr.output = {"condition": step.config["condition"], "result": taken, "arm": arm}
        sr.ended_at_ms = self.clock.now_ms()
        self.audit.append(sr.ended_at_ms, run.run_id, "step_succeeded",
                          {"step_id": step.id, "branch": arm})

    def _execute_parallel(self, run: Run, defn: WorkflowDefinition, step: Step,
                          budgets: ScopedBudgets, sr: StepRun) -> None:
        outputs: dict[str, Any] = {}
        for i, branch in enumerate(step.config.get("branches", [])):
            for ref in branch:
                target = defn.step_by_id(ref)
                if target is None:
                    continue
                sub = self._execute_step(run, defn, target, budgets, force=True)
                outputs[f"branch_{i}:{ref}"] = sub.output
                if sub.status == StepStatus.FAILED and target.on_error == "fail":
                    sr.status = StepStatus.FAILED
                    sr.error = f"parallel branch step {ref} failed"
                    sr.ended_at_ms = self.clock.now_ms()
                    raise StepFailure(sr.error)
        sr.status = StepStatus.SUCCEEDED
        sr.output = outputs
        sr.ended_at_ms = self.clock.now_ms()
        self.audit.append(sr.ended_at_ms, run.run_id, "step_succeeded",
                          {"step_id": step.id, "branches": len(step.config.get("branches", []))})

    def _execute_loop(self, run: Run, defn: WorkflowDefinition, step: Step,
                      budgets: ScopedBudgets, sr: StepRun) -> None:
        items = run.context.get(step.config["items_key"], [])
        if not isinstance(items, list):
            raise StepFailure(f"loop items_key {step.config['items_key']!r} is not a list")
        cap = min(int(step.config.get("max_iterations", self.settings.max_loop_iterations)),
                  self.settings.max_loop_iterations)
        iterations = items[:cap]
        outputs: list[Any] = []
        for item in iterations:
            run.context["item"] = item
            for ref in step.config.get("body", []):
                target = defn.step_by_id(ref)
                if target is None:
                    continue
                sub = self._execute_step(run, defn, target, budgets, force=True)
                if sub.status == StepStatus.FAILED and target.on_error == "fail":
                    sr.status = StepStatus.FAILED
                    sr.error = f"loop body step {ref} failed on item {item!r}"
                    sr.ended_at_ms = self.clock.now_ms()
                    raise StepFailure(sr.error)
            outputs.append(run.context.get("item"))
        run.context.pop("item", None)
        # Body steps are consumed by the loop: if there were no iterations
        # they must not run later as ordinary steps.
        for ref in step.config.get("body", []):
            if run.step_run(ref) is None:
                run.step_runs.append(StepRun(
                    step_id=ref, status=StepStatus.SKIPPED,
                    output=f"skipped: loop {step.id} had no iterations"))
        sr.status = StepStatus.SUCCEEDED
        sr.output = {"iterations": len(iterations), "items": outputs}
        sr.ended_at_ms = self.clock.now_ms()
        self.audit.append(sr.ended_at_ms, run.run_id, "step_succeeded",
                          {"step_id": step.id, "iterations": len(iterations)})

    def _request_approval(self, run: Run, defn: WorkflowDefinition, step: Step, sr: StepRun) -> None:
        now = self.clock.now_ms()
        window_s = int(step.config.get("expires_in_s", self.settings.approval_default_expiry_s))
        approval = Approval(
            run_id=run.run_id,
            step_id=step.id,
            approvers=list(step.config.get("approvers", [])),
            requested_at_ms=now,
            expires_at_ms=now + window_s * 1000,
        )
        self.store.save_approval(approval)
        sr.status = StepStatus.WAITING_APPROVAL
        sr.started_at_ms = now
        self.audit.append(now, run.run_id, "approval_requested",
                          {"step_id": step.id, "approvers": approval.approvers,
                           "expires_at_ms": approval.expires_at_ms})
