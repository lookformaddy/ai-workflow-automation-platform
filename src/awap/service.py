"""FastAPI service layer.

Kept thin: it validates requests with pydantic and delegates everything
to the stdlib engine. Importing this module requires FastAPI; the engine
and tests work without it (service tests auto-skip).
"""
from __future__ import annotations

from typing import Any

try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover - exercised only without FastAPI
    FastAPI = None  # type: ignore

from .audit import AuditLog
from .demo_data import demo_workflows
from .engine import Engine
from .models import Budget, Step, StepType, Trigger, WorkflowDefinition
from .providers import ScriptedLLMProvider, demo_tool_registry
from .store import WorkflowStore
from .validation import validate_definition

if FastAPI is not None:  # pragma: no cover - import guard

    class StepIn(BaseModel):
        id: str
        type: str
        name: str = ""
        config: dict[str, Any] = Field(default_factory=dict)
        depends_on: list[str] | None = None
        timeout_ms: int = 30_000
        on_error: str = "fail"

    class WorkflowIn(BaseModel):
        id: str
        name: str
        tenant: str = "default"
        description: str = ""
        steps: list[StepIn]
        triggers: list[dict[str, Any]] = Field(default_factory=list)
        budget: dict[str, Any] = Field(default_factory=dict)
        tags: list[str] = Field(default_factory=list)

    class RunIn(BaseModel):
        input_data: dict[str, Any] = Field(default_factory=dict)
        idempotency_key: str = ""

    class ApprovalIn(BaseModel):
        approver: str
        decision: str  # approve | deny
        note: str = ""

    def _to_definition(payload: WorkflowIn) -> WorkflowDefinition:
        steps = [
            Step(id=s.id, type=StepType(s.type), name=s.name or s.id,
                 config=s.config, depends_on=s.depends_on,
                 timeout_ms=s.timeout_ms, on_error=s.on_error)
            for s in payload.steps
        ]
        return WorkflowDefinition(
            id=payload.id, name=payload.name, tenant=payload.tenant,
            description=payload.description, steps=steps,
            triggers=[Trigger(kind=t.get("kind", "manual"),
                              expression=t.get("expression", ""),
                              payload_filter=t.get("payload_filter", {}))
                      for t in payload.triggers],
            budget=Budget(**payload.budget) if payload.budget else Budget(),
            tags=payload.tags,
        )

    def create_app(engine: Engine | None = None) -> FastAPI:
        eng = engine or Engine(store=WorkflowStore(),
                               audit=AuditLog(),
                               llm=ScriptedLLMProvider(
                                   {"welcome": "Welcome aboard! We're glad to have you.",
                                    "escalation": "Escalation summary: high-priority case.",
                                    "auto-reply": "Thanks for reaching out — we're on it."}),
                               tools=demo_tool_registry())
        app = FastAPI(title="AI Workflow Automation Platform", version="1.0.0")

        @app.get("/health")
        def health() -> dict[str, str]:
            return {"status": "ok", "service": "awap"}

        @app.post("/workflows", status_code=201)
        def create_workflow(payload: WorkflowIn) -> dict[str, Any]:
            defn = _to_definition(payload)
            errors = validate_definition(defn)
            if errors:
                raise HTTPException(status_code=422, detail=errors)
            stored = eng.store.register(defn)
            eng.audit.append(eng.clock.now_ms(), "-", "workflow_registered",
                             {"workflow_id": stored.id, "version": stored.version})
            return {"id": stored.id, "version": stored.version, "steps": len(stored.steps)}

        @app.get("/workflows")
        def list_workflows() -> list[dict[str, Any]]:
            return [{"id": w.id, "name": w.name, "version": w.version,
                     "tenant": w.tenant, "steps": len(w.steps)}
                    for w in eng.store.list_workflows()]

        @app.get("/workflows/{workflow_id}")
        def get_workflow(workflow_id: str) -> dict[str, Any]:
            defn = eng.store.get(workflow_id)
            if defn is None:
                raise HTTPException(status_code=404, detail="workflow not found")
            return {"id": defn.id, "name": defn.name, "version": defn.version,
                    "tenant": defn.tenant, "steps": [s.id for s in defn.steps],
                    "triggers": [{"kind": t.kind, "expression": t.expression}
                                 for t in defn.triggers]}

        @app.post("/workflows/{workflow_id}/rollback/{version}", status_code=201)
        def rollback_workflow(workflow_id: str, version: int) -> dict[str, Any]:
            try:
                restored = eng.store.rollback(workflow_id, version)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return {"id": restored.id, "version": restored.version,
                    "restored_from": version}

        @app.post("/workflows/{workflow_id}/run", status_code=201)
        def start_run(workflow_id: str, payload: RunIn) -> dict[str, Any]:
            defn = eng.store.get(workflow_id)
            if defn is None:
                raise HTTPException(status_code=404, detail="workflow not found")
            run = eng.start_run(defn, payload.input_data, payload.idempotency_key)
            return {"run_id": run.run_id, "status": run.status.value,
                    "workflow_id": run.workflow_id, "version": run.workflow_version}

        @app.get("/runs/{run_id}")
        def get_run(run_id: str) -> dict[str, Any]:
            run = eng.store.get_run(run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="run not found")
            return {"run_id": run.run_id, "status": run.status.value,
                    "workflow_id": run.workflow_id, "version": run.workflow_version,
                    "tokens": run.total_tokens, "cost_usd": round(run.total_cost_usd, 6),
                    "latency_ms": run.total_latency_ms, "error": run.error,
                    "steps": [{"step_id": s.step_id, "status": s.status.value,
                               "attempts": s.attempts, "tokens": s.tokens,
                               "error": s.error} for s in run.step_runs]}

        @app.get("/runs/{run_id}/trace")
        def get_trace(run_id: str) -> list[dict[str, Any]]:
            run = eng.store.get_run(run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="run not found")
            return [{"step_id": s.step_id, "status": s.status.value,
                     "attempts": s.attempts, "duration_ms": s.duration_ms,
                     "tokens": s.tokens, "cost_usd": round(s.cost_usd, 6),
                     "output": s.output, "error": s.error}
                    for s in run.step_runs]

        @app.post("/runs/{run_id}/approve/{step_id}")
        def approve_step(run_id: str, step_id: str, payload: ApprovalIn) -> dict[str, Any]:
            if payload.decision not in ("approve", "deny"):
                raise HTTPException(status_code=422, detail="decision must be approve|deny")
            try:
                run = eng.approve(run_id, step_id, payload.approver,
                                  payload.decision, payload.note)
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"run_id": run.run_id, "status": run.status.value}

        @app.post("/runs/{run_id}/resume")
        def resume_run(run_id: str) -> dict[str, Any]:
            try:
                run = eng.resume(run_id)
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"run_id": run.run_id, "status": run.status.value}

        @app.post("/approvals/sweep")
        def sweep_approvals() -> dict[str, Any]:
            expired = eng.sweep_expired()
            return {"expired": [{"run_id": a.run_id, "step_id": a.step_id,
                                 "status": a.status.value} for a in expired]}

        @app.get("/audit/verify")
        def verify_audit() -> dict[str, Any]:
            ok, message = eng.audit.verify()
            return {"ok": ok, "message": message, "entries": len(eng.audit.entries)}

        @app.get("/demo/workflows")
        def demo_list() -> list[dict[str, str]]:
            return [{"id": w.id, "name": w.name, "description": w.description}
                    for w in demo_workflows()]

        return app

    app = create_app()
