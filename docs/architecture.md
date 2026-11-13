# Architecture — AI Workflow Automation Platform

## Components

| Component | File | Responsibility |
|---|---|---|
| Models | `src/awap/models.py` | Dataclasses: `WorkflowDefinition`, `Step`, `Run`, `StepRun`, `Approval`, `Budget`, `RetryPolicy`, `Trigger` |
| Validation | `src/awap/validation.py` | Fail-fast definition checks (refs, cycles, per-type config, budgets) |
| DAG | `src/awap/dag.py` | Topological ordering into execution levels (Kahn's algorithm) |
| Providers | `src/awap/providers.py` | Scripted LLM / tool / transform providers (deterministic, no network) |
| Budgets | `src/awap/budgets.py` | Fail-closed token / cost / latency / step caps, tenant + workflow scopes |
| Audit | `src/awap/audit.py` | Append-only, SHA-256 hash-chained JSONL log with verification |
| Store | `src/awap/store.py` | Versioned definitions, runs, approvals, idempotency keys; JSON snapshot |
| Triggers | `src/awap/triggers.py` | 5-field cron matcher and event-type + payload-filter matching |
| Engine | `src/awap/engine.py` | Durable run execution: checkpoints, resume, retry, timeout, approvals, budgets |
| Service | `src/awap/service.py` | FastAPI layer (thin; delegates to the engine) |
| CLI | `src/awap/cli.py` | `demo`, `list`, `run`, `approve`, `audit-verify`, `sweep` |

## Data flow

```
                         ┌──────────────────┐
                         │  WorkflowStore   │  versioned definitions
                         │  (immutable vN)  │  runs · approvals · idempotency
                         └────────┬─────────┘
                                  │
 trigger (cron/event/manual)     │  register / update / rollback
        │                        ▼
        │               ┌──────────────────┐
        └──────────────►│     Engine       │──► providers (LLM / tool / transform)
                        │  start / resume  │──► budgets (fail-closed)
                        │  approve / sweep │──► checkpoints (after each step)
                        └────────┬─────────┘──► AuditLog (hash-chained)
                                 │
                    ┌────────────┴────────────┐
                    │                         │
              FastAPI service              CLI
```

A run is a flat execution over the DAG order. Control-flow steps consume
their referenced steps: a branch marks the untaken arm SKIPPED; a parallel
step executes each branch and joins; a loop executes its body per item up
to `max_iterations`. Every step writes a checkpoint to the store before the
next step starts, so a crash or an approval pause loses at most the
in-flight step — and the in-flight step is itself retryable.

## Failure modes

| Failure | Behaviour |
|---|---|
| Invalid definition | Rejected at registration / run start with a list of errors; nothing runs |
| Provider transient failure | Retried per step policy with recorded exponential backoff |
| Step timeout | Duration exceeds `timeout_ms` → retryable failure, then run fails |
| Step failure, `on_error: fail` | Run fails immediately; later steps never execute |
| Step failure, `on_error: continue` | Step marked FAILED, run continues |
| Budget breach | Run halts fail-closed at the pre- or post-step check; no further steps execute |
| Approval expiry | Sweep escalates to `escalate_to` with a fresh window, or auto-denies per policy |
| Approver unavailable | Escalation reassigns the gate; the run stays paused, never auto-approved |
| Crash mid-run | Resume from the last checkpoint; completed steps are not re-executed |
| Duplicate start | Same idempotency key returns the original run; providers are not called twice |
| Audit tampering | `verify()` recomputes the hash chain; any edit, deletion or reorder is detected |
| Unknown tool / bad transform | Step fails with a clear error; `on_error` decides whether the run continues |

## Diagram description

Imagine three horizontal lanes. **Top lane — definition time:** an author
writes a workflow (steps + triggers + budget); validation rejects bad
definitions; the store appends an immutable version. **Middle lane — run
time:** a trigger starts a run; the engine walks the DAG, calling
providers, checking budgets before and after each step, writing a
checkpoint per step, and pausing at approval gates. **Bottom lane —
governance:** every transition appends a hash-chained audit entry; the
sweep job expires and escalates stale approvals; the audit endpoint
verifies the chain on demand.

## Trade-offs (recap)

- Simulated providers and durations keep the system deterministic and
  keyless; the provider boundary is the swap point for real services.
- Sequential execution of parallel branches preserves fan-out/join
  semantics without concurrency machinery; true parallelism is a
  deployment concern.
- In-memory store with a JSON snapshot is sufficient at demo scale;
  production would use a transactional database with the same versioning
  semantics.
- Branch conditions use a safe AST evaluator — expressive enough for
  routing, with no `eval()` surface.
