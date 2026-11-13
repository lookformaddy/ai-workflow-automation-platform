"""Persistence: versioned workflow definitions, runs, approvals.

Definitions are immutable per version; `register` adds version 1, `update`
appends a new version, and `rollback` appends a new version whose content
copies an older one — so every rollback is itself auditable and reversible.
Runs are keyed by idempotency key for exactly-once start semantics.
State is kept in memory and optionally snapshotted to JSON.
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import Approval, Run, WorkflowDefinition


def _definition_to_dict(defn: WorkflowDefinition) -> dict[str, Any]:
    return asdict(defn)


def _definition_from_dict(data: dict[str, Any]) -> WorkflowDefinition:
    from .models import Budget, RetryPolicy, Step, StepType, Trigger

    steps = []
    for s in data.get("steps", []):
        steps.append(Step(
            id=s["id"],
            type=StepType(s["type"]),
            name=s.get("name", ""),
            config=s.get("config", {}),
            depends_on=s.get("depends_on", []),
            retry=RetryPolicy(**s.get("retry", {})),
            timeout_ms=s.get("timeout_ms", 30_000),
            on_error=s.get("on_error", "fail"),
            output_key=s.get("output_key", ""),
        ))
    return WorkflowDefinition(
        id=data["id"],
        name=data["name"],
        tenant=data.get("tenant", "default"),
        version=data.get("version", 1),
        description=data.get("description", ""),
        steps=steps,
        triggers=[Trigger(**t) for t in data.get("triggers", [])],
        budget=Budget(**data.get("budget", {})),
        tags=data.get("tags", []),
    )


class WorkflowStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.versions: dict[str, list[WorkflowDefinition]] = {}
        self.runs: dict[str, Run] = {}
        self.approvals: dict[str, Approval] = {}  # key: f"{run_id}:{step_id}"
        self.idempotency: dict[str, str] = {}  # key -> run_id
        if self.path and self.path.exists():
            self.load()

    # -- definitions -----------------------------------------------------
    def register(self, defn: WorkflowDefinition) -> WorkflowDefinition:
        stored = copy.deepcopy(defn)
        stored.version = 1
        self.versions.setdefault(defn.id, []).append(stored)
        self._persist()
        return copy.deepcopy(stored)

    def update(self, defn: WorkflowDefinition) -> WorkflowDefinition:
        history = self.versions.get(defn.id)
        if not history:
            return self.register(defn)
        stored = copy.deepcopy(defn)
        stored.version = history[-1].version + 1
        history.append(stored)
        self._persist()
        return copy.deepcopy(stored)

    def rollback(self, workflow_id: str, to_version: int) -> WorkflowDefinition:
        history = self.versions.get(workflow_id)
        if not history:
            raise KeyError(f"unknown workflow: {workflow_id}")
        target = next((v for v in history if v.version == to_version), None)
        if target is None:
            raise KeyError(f"workflow {workflow_id} has no version {to_version}")
        restored = copy.deepcopy(target)
        restored.version = history[-1].version + 1
        history.append(restored)
        self._persist()
        return copy.deepcopy(restored)

    def put(self, defn: WorkflowDefinition) -> WorkflowDefinition:
        """Insert a definition as-is (preserving its version) if that
        version is not already stored — used by the engine so runs started
        from a bare definition object can be resumed/approved later."""
        history = self.versions.setdefault(defn.id, [])
        if not any(v.version == defn.version for v in history):
            history.append(copy.deepcopy(defn))
            history.sort(key=lambda v: v.version)
            self._persist()
        return copy.deepcopy(defn)

    def get(self, workflow_id: str, version: int | None = None) -> WorkflowDefinition | None:
        history = self.versions.get(workflow_id)
        if not history:
            return None
        if version is None:
            return copy.deepcopy(history[-1])
        match = next((v for v in history if v.version == version), None)
        return copy.deepcopy(match) if match else None

    def list_workflows(self) -> list[WorkflowDefinition]:
        return [copy.deepcopy(v[-1]) for v in self.versions.values()]

    def version_count(self, workflow_id: str) -> int:
        return len(self.versions.get(workflow_id, []))

    # -- runs / approvals -------------------------------------------------
    def save_run(self, run: Run) -> None:
        self.runs[run.run_id] = copy.deepcopy(run)
        self._persist()

    def get_run(self, run_id: str) -> Run | None:
        run = self.runs.get(run_id)
        return copy.deepcopy(run) if run else None

    def save_approval(self, approval: Approval) -> None:
        self.approvals[f"{approval.run_id}:{approval.step_id}"] = copy.deepcopy(approval)
        self._persist()

    def get_approval(self, run_id: str, step_id: str) -> Approval | None:
        ap = self.approvals.get(f"{run_id}:{step_id}")
        return copy.deepcopy(ap) if ap else None

    def pending_approvals(self) -> list[Approval]:
        from .models import ApprovalStatus
        return [copy.deepcopy(a) for a in self.approvals.values()
                if a.status == ApprovalStatus.PENDING]

    # -- persistence -------------------------------------------------------
    def _persist(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "versions": {wid: [_definition_to_dict(v) for v in vs]
                         for wid, vs in self.versions.items()},
            "runs": {rid: asdict(r) for rid, r in self.runs.items()},
            "approvals": {k: asdict(a) for k, a in self.approvals.items()},
            "idempotency": self.idempotency,
        }
        self.path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def load(self) -> None:
        assert self.path is not None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.versions = {
            wid: [_definition_from_dict(v) for v in vs]
            for wid, vs in data.get("versions", {}).items()
        }
        # Runs/approvals are restored lazily by the engine when needed; the
        # JSON snapshot keeps them for inspection. Full run restore is
        # intentionally out of scope for the demo store.
        self.idempotency = data.get("idempotency", {})
