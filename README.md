# AI Workflow Automation Platform

An enterprise workflow automation platform: **declarative workflow
definitions** (LLM call, tool call, human approval, transform, branch,
parallel fan-out, loop-with-cap), **scheduled/cron + event triggers**, and a
**durable run engine** with checkpoints and resume, retries with exponential
backoff, per-step timeouts, approval gates with expiry + escalation,
fail-closed per-workflow and per-tenant budgets, idempotency keys, versioned
definitions with rollback, run history + trace log, and a hash-chained audit
log — exposed via a CLI and a FastAPI service.

> **Clean-room note:** all code is original and all demo data is synthetic
> (a fictional tenant, fictional accounts, fictional tickets). LLM and tool
> providers are scripted stand-ins — no network calls, no API keys, no
> employer code, data, or proprietary designs anywhere.

## The enterprise problem

Teams automate multi-step AI work with ad-hoc scripts: an LLM draft here, a
tool call there, a human check over chat. That breaks the moment a step
fails, a budget blows up, an approver is on leave, or an auditor asks what
happened. This platform makes the workflow itself the unit of governance:

- **Declarative definitions** — a workflow is data: steps, triggers, budget.
  Validated before any run starts, versioned, and rollbackable.
- **Durable runs** — every step writes a checkpoint, so a run survives a
  crash or an approval pause and resumes exactly where it stopped.
- **Fail-closed budgets** — per-workflow and per-tenant token / cost /
  latency / step caps. A cap breach halts the run; it never silently
  overspends.
- **Human gates that don't stall** — approvals expire on a timer and
  escalate to a duty manager (or auto-deny, per policy).
- **Exactly-once starts** — idempotency keys make retries safe.
- **Provable history** — a hash-chained audit log; any edit or reordering
  breaks verification.

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full design.

```
definition (YAML/JSON) ──► validate ──► versioned store
                                  │
trigger (cron/event/manual) ──► engine ──► steps: llm · tool · transform
                                  │            branch · parallel · loop
                                  ├─► budgets (fail-closed caps)
                                  ├─► checkpoints (resume)
                                  ├─► approvals (expiry + escalation)
                                  └─► hash-chained audit log
```

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/demo.py          # end-to-end demo on synthetic data
.venv/bin/python -m pytest -q             # full test suite
```

Run the service:

```bash
.venv/bin/python -m uvicorn awap.service:app --port 8000
# GET  /health · POST /workflows · GET /workflows · POST /workflows/{id}/run
# GET  /runs/{id} · GET /runs/{id}/trace · POST /runs/{id}/approve/{step}
# POST /runs/{id}/resume · POST /approvals/sweep · GET /audit/verify
```

Or via the CLI (from the repo root; `PYTHONPATH=src` is set by the Makefile):

```bash
PYTHONPATH=src .venv/bin/python -m awap.cli demo
PYTHONPATH=src .venv/bin/python -m awap.cli list
PYTHONPATH=src .venv/bin/python -m awap.cli run support-triage --input '{"priority": "high", "followups": ["call"]}'
PYTHONPATH=src .venv/bin/python -m awap.cli audit-verify
```

Docker:

```bash
docker build -t awap . && docker run -p 8000:8000 awap
```

## How it works

| Concern | Mechanism |
|---|---|
| Definition | Flat step list with `depends_on`; validated (duplicates, unknown refs, cycles, per-type config, budget sanity) before any run |
| Ordering | Topological levels (Kahn's algorithm); a plain list behaves as a sequence |
| Branch | Safe AST-evaluated condition over the run context; the untaken arm is marked SKIPPED |
| Parallel | Branches execute and join; a failing branch step fails the run unless `on_error: continue` |
| Loop | Iterates over a context list, capped by `max_iterations` (engine-wide cap 50) |
| Retry | Per-step policy (attempts, base, multiplier, cap); backoff delays are recorded, not slept, so runs stay deterministic |
| Timeout | Provider-reported duration vs `timeout_ms`; a timeout is a retryable failure |
| Approval | Run pauses with a checkpoint; expiry sweep escalates to `escalate_to` (or auto-denies); only listed approvers can decide |
| Budgets | Tenant tracker persists across runs; workflow tracker is per run; checked before and after every step — fail-closed |
| Idempotency | `tenant:workflow:version:key` maps to the original run; a repeat start returns it without re-executing |
| Versioning | Immutable versions; rollback appends a new version copying old content, so rollbacks are auditable |
| Audit | Append-only JSONL, SHA-256 hash-chained; `verify()` recomputes the chain |
| Triggers | 5-field cron (`*`, lists, ranges, steps, weekday/month names) and event-type + payload-filter matching |

## Design decisions and trade-offs

- **Scripted providers instead of a real LLM.** Deterministic, zero-key,
  fully reproducible runs and tests. The provider boundary (`providers.py`)
  is where a real LLM/tool client plugs in — the engine only depends on
  `complete()` / `call()` returning output, tokens and a duration.
- **Simulated time and durations.** Providers report durations and the
  engine reads time through an injectable clock, so retries, timeouts and
  approval expiry are testable without sleeping. A production deployment
  would use real durations and a durable timer service.
- **In-memory store + JSON snapshot.** Correct at demo scale with no
  database dependency; a production deployment would move the store to a
  transactional database while keeping the versioning semantics.
- **Branch conditions via a safe AST evaluator.** No `eval()` — only
  comparisons, boolean ops and arithmetic over context names. Expressive
  enough for routing; not a general scripting language.
- **Parallel branches execute sequentially in simulation.** The semantics
  (fan-out, join, per-branch failure) are identical; true concurrency is a
  deployment concern, not a workflow-semantics concern.
- **JSON-file audit log.** Human-readable and verifiable; a production
  deployment would append to an immutable store (e.g. WORM storage).

## What it demonstrates

- Declarative workflow definitions with validation, versioning and rollback
- Durable run engine: checkpoints, resume, retries, timeouts, idempotency
- Human approval gates with expiry and escalation
- Fail-closed per-workflow and per-tenant budgets
- Branch / parallel / loop control flow with a safe condition evaluator
- Cron + event triggers
- Hash-chained audit log with tamper verification
- FastAPI service + CLI + Docker + GitHub Actions CI

## Project layout

```
src/awap/         core package (config, models, validation, dag, providers,
                  budgets, audit, store, triggers, engine, service, cli,
                  demo_data)
scripts/demo.py   end-to-end demo (6 scenarios, verifiable numbers)
tests/            pytest suite (service tests auto-skip without FastAPI)
docs/             architecture.md, github-actions-ci.yml
data/             store/audit persistence (created at runtime)
```

## CI

The GitHub Actions definition lives at `docs/github-actions-ci.yml` — move it
to `.github/workflows/ci.yml` to activate (the file is kept out of
`.github/` because this repo's integration cannot write workflow files).
It runs the test suite on Python 3.11/3.12 and the demo script.
