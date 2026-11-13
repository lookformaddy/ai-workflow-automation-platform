"""Core domain models — plain dataclasses, no framework dependency.

A workflow is a flat list of steps. Control-flow steps (branch, parallel,
loop) reference other step ids; the engine consumes the referenced steps
when the control step executes, so referenced steps never run twice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StepType(str, Enum):
    LLM = "llm"
    TOOL = "tool"
    APPROVAL = "approval"
    TRANSFORM = "transform"
    BRANCH = "branch"
    PARALLEL = "parallel"
    LOOP = "loop"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING_APPROVAL = "waiting_approval"
    TIMED_OUT = "timed_out"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    ESCALATED = "escalated"


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    backoff_base_ms: int = 100
    backoff_multiplier: float = 2.0
    max_backoff_ms: int = 5_000

    def delays_ms(self) -> list[int]:
        """Simulated backoff delays between attempts (no real sleep)."""
        delays = []
        delay = self.backoff_base_ms
        for _ in range(max(0, self.max_attempts - 1)):
            delays.append(min(delay, self.max_backoff_ms))
            delay = int(delay * self.backoff_multiplier)
        return delays


@dataclass
class Budget:
    max_tokens: int = 20_000
    max_cost_usd: float = 2.00
    max_latency_ms: int = 120_000
    max_steps: int = 100


@dataclass
class Step:
    id: str
    type: StepType
    name: str = ""
    # Step-type specific configuration, e.g.:
    #  llm:      {"prompt": "...", "model": "scripted", "output_key": "draft"}
    #  tool:     {"tool": "crm_lookup", "args": {...}, "output_key": "record"}
    #  approval: {"approvers": ["ops-lead"], "expires_in_s": 3600,
    #             "on_expiry": "escalate", "escalate_to": ["duty-manager"]}
    #  transform:{"op": "uppercase"|"template"|"extract", ...}
    #  branch:   {"condition": "score >= 0.7", "true_steps": [...],
    #             "false_steps": [...]}
    #  parallel: {"branches": [[...], [...]]}
    #  loop:     {"items_key": "items", "body": [...], "max_iterations": 10}
    config: dict[str, Any] = field(default_factory=dict)
    # None = implicit dependency on the previous step in declaration order;
    # [] = no dependencies (runs in the first topological level).
    depends_on: list[str] | None = None
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    timeout_ms: int = 30_000
    # Fail the whole run when this step fails, or continue with the rest.
    on_error: str = "fail"  # fail | continue
    output_key: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.id
        if self.output_key:
            self.config.setdefault("output_key", self.output_key)


@dataclass
class Trigger:
    kind: str  # "cron" | "event" | "manual"
    expression: str = ""  # cron expr ("0 9 * * MON-FRI") or event type
    payload_filter: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowDefinition:
    id: str
    name: str
    tenant: str = "default"
    version: int = 1
    description: str = ""
    steps: list[Step] = field(default_factory=list)
    triggers: list[Trigger] = field(default_factory=list)
    budget: Budget = field(default_factory=Budget)
    tags: list[str] = field(default_factory=list)

    def step_by_id(self, step_id: str) -> Step | None:
        for step in self.steps:
            if step.id == step_id:
                return step
        return None

    def step_ids(self) -> list[str]:
        return [s.id for s in self.steps]


@dataclass
class StepRun:
    step_id: str
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    started_at_ms: int = 0
    ended_at_ms: int = 0
    duration_ms: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    output: Any = None
    error: str = ""
    backoff_delays_ms: list[int] = field(default_factory=list)

    @property
    def latency_ms(self) -> int:
        return self.duration_ms


@dataclass
class Approval:
    run_id: str
    step_id: str
    approvers: list[str]
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at_ms: int = 0
    expires_at_ms: int = 0
    decided_by: str = ""
    decided_at_ms: int = 0
    note: str = ""
    escalated_to: list[str] = field(default_factory=list)
    escalation_count: int = 0


@dataclass
class Run:
    run_id: str
    workflow_id: str
    workflow_version: int
    tenant: str
    status: RunStatus = RunStatus.PENDING
    input_data: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    step_runs: list[StepRun] = field(default_factory=list)
    current_step_index: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    total_latency_ms: int = 0
    started_at_ms: int = 0
    ended_at_ms: int = 0
    error: str = ""
    # Checkpoint: index of the next step to execute on resume.
    checkpoint_step_index: int = 0

    def step_run(self, step_id: str) -> StepRun | None:
        for sr in self.step_runs:
            if sr.step_id == step_id:
                return sr
        return None
