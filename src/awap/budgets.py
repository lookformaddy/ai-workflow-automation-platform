"""Per-workflow and per-tenant budgets with fail-closed caps.

A `BudgetTracker` accumulates tokens, cost and latency for one scope
(tenant or workflow). Every check is fail-closed: if a cap is already
exceeded, or the next step's reservation would exceed it, the check raises
`BudgetExceeded` and the run halts without executing further steps.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Budget


class BudgetExceeded(Exception):
    def __init__(self, scope: str, dimension: str, limit: float, used: float):
        self.scope = scope
        self.dimension = dimension
        self.limit = limit
        self.used = used
        super().__init__(
            f"budget exceeded [{scope}] {dimension}: used {used} > limit {limit}"
        )


@dataclass
class BudgetTracker:
    scope: str  # e.g. "tenant:acme" or "workflow:onboarding"
    budget: Budget
    tokens_used: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    steps_used: int = 0

    def check_before_step(self, est_tokens: int = 0, est_cost_usd: float = 0.0) -> None:
        """Fail-closed pre-flight check before a step executes."""
        if self.tokens_used + est_tokens > self.budget.max_tokens:
            raise BudgetExceeded(self.scope, "tokens", self.budget.max_tokens,
                                 self.tokens_used + est_tokens)
        if self.cost_usd + est_cost_usd > self.budget.max_cost_usd:
            raise BudgetExceeded(self.scope, "cost_usd", self.budget.max_cost_usd,
                                 self.cost_usd + est_cost_usd)
        if self.latency_ms > self.budget.max_latency_ms:
            raise BudgetExceeded(self.scope, "latency_ms", self.budget.max_latency_ms,
                                 self.latency_ms)
        if self.steps_used + 1 > self.budget.max_steps:
            raise BudgetExceeded(self.scope, "steps", self.budget.max_steps,
                                 self.steps_used + 1)

    def record(self, tokens: int = 0, cost_usd: float = 0.0,
               latency_ms: int = 0) -> None:
        self.tokens_used += tokens
        self.cost_usd += cost_usd
        self.latency_ms += latency_ms
        self.steps_used += 1

    def check_after_step(self) -> None:
        """Post-step guard: a step that blew a cap halts the run."""
        if self.tokens_used > self.budget.max_tokens:
            raise BudgetExceeded(self.scope, "tokens", self.budget.max_tokens,
                                 self.tokens_used)
        if self.cost_usd > self.budget.max_cost_usd:
            raise BudgetExceeded(self.scope, "cost_usd", self.budget.max_cost_usd,
                                 self.cost_usd)
        if self.latency_ms > self.budget.max_latency_ms:
            raise BudgetExceeded(self.scope, "latency_ms", self.budget.max_latency_ms,
                                 self.latency_ms)


@dataclass
class BudgetManager:
    """Holds one tracker per tenant and per workflow; checks both scopes.

    Tenant trackers persist across runs (a tenant-wide cap), while each
    run gets a fresh workflow tracker for its own definition budget.
    """

    tenant_budgets: dict[str, Budget] = field(default_factory=dict)
    default_tenant_budget: Budget = field(default_factory=Budget)
    _tenant_trackers: dict[str, BudgetTracker] = field(default_factory=dict, repr=False)

    def tracker(self, tenant: str, workflow_id: str, workflow_budget: Budget) -> "ScopedBudgets":
        if tenant not in self._tenant_trackers:
            tenant_budget = self.tenant_budgets.get(tenant, self.default_tenant_budget)
            self._tenant_trackers[tenant] = BudgetTracker(f"tenant:{tenant}", tenant_budget)
        return ScopedBudgets(
            tenant=self._tenant_trackers[tenant],
            workflow=BudgetTracker(f"workflow:{workflow_id}", workflow_budget),
        )


@dataclass
class ScopedBudgets:
    tenant: BudgetTracker
    workflow: BudgetTracker

    def check_before_step(self, est_tokens: int = 0, est_cost_usd: float = 0.0) -> None:
        self.tenant.check_before_step(est_tokens, est_cost_usd)
        self.workflow.check_before_step(est_tokens, est_cost_usd)

    def record(self, tokens: int = 0, cost_usd: float = 0.0, latency_ms: int = 0) -> None:
        self.tenant.record(tokens, cost_usd, latency_ms)
        self.workflow.record(tokens, cost_usd, latency_ms)

    def check_after_step(self) -> None:
        self.tenant.check_after_step()
        self.workflow.check_after_step()
