"""Synthetic demo workflow definitions (generic domains, seeded data).

Three workflows exercise every engine feature:
- `customer-onboarding`: LLM draft -> tool lookup -> approval gate -> notify.
- `support-triage`: score -> branch (high/low) -> parallel fan-out -> loop.
- `budget-demo`: a deliberately tiny budget so the demo can show a
  fail-closed budget halt.
"""
from __future__ import annotations

from .models import Budget, Step, StepType, Trigger, WorkflowDefinition


def onboarding_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        id="customer-onboarding",
        name="Customer Onboarding (demo)",
        tenant="acme",
        description="Draft a welcome message with the LLM, look the account up "
                    "in the CRM, require a human approval, then notify.",
        steps=[
            Step(id="draft", type=StepType.LLM, name="Draft welcome message",
                 config={"prompt": "Draft a short welcome message for account {account_id}.",
                         "output_key": "draft"}),
            Step(id="lookup", type=StepType.TOOL, name="CRM lookup",
                 config={"tool": "crm_lookup",
                         "args": {"account_id": "{account_id}"},
                         "output_key": "account"}),
            Step(id="review", type=StepType.APPROVAL, name="Manager review",
                 config={"approvers": ["ops-lead"], "expires_in_s": 3600,
                         "on_expiry": "escalate", "escalate_to": ["duty-manager"]}),
            Step(id="notify", type=StepType.TOOL, name="Send welcome",
                 config={"tool": "send_notification",
                         "args": {"channel": "email", "recipient": "customer@example.test"}}),
        ],
        triggers=[Trigger(kind="event", expression="account.created")],
        budget=Budget(max_tokens=5_000, max_cost_usd=1.00, max_steps=10),
        tags=["onboarding", "demo"],
    )


def triage_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        id="support-triage",
        name="Support Triage (demo)",
        tenant="acme",
        description="Score the request, branch on priority, fan out to two "
                    "tools in parallel, then loop over follow-up actions.",
        steps=[
            Step(id="score", type=StepType.TOOL, name="Score request",
                 config={"tool": "score_request", "args": {"priority": "{priority}"},
                         "output_key": "scored"}),
            Step(id="route", type=StepType.BRANCH, name="Route by score",
                 config={"condition": "scored['score'] >= 0.7",
                         "true_steps": ["escalate"], "false_steps": ["auto_reply"]}),
            Step(id="escalate", type=StepType.LLM, name="Draft escalation",
                 config={"prompt": "Draft an escalation summary for a high-priority case.",
                         "output_key": "escalation"}),
            Step(id="auto_reply", type=StepType.LLM, name="Draft auto-reply",
                 config={"prompt": "Draft a helpful auto-reply for a routine case.",
                         "output_key": "reply"}),
            Step(id="fanout", type=StepType.PARALLEL, name="Enrich in parallel",
                 config={"branches": [["inventory"], ["notify_ops"]]}),
            Step(id="inventory", type=StepType.TOOL, name="Check inventory",
                 config={"tool": "inventory_check", "args": {"sku": "SKU-100"}}),
            Step(id="notify_ops", type=StepType.TOOL, name="Notify ops",
                 config={"tool": "send_notification",
                         "args": {"channel": "email", "recipient": "ops@example.test"}}),
            Step(id="followups", type=StepType.LOOP, name="Follow-up actions",
                 config={"items_key": "followups", "body": ["followup_note"],
                         "max_iterations": 5}),
            Step(id="followup_note", type=StepType.TRANSFORM, name="Format follow-up",
                 config={"op": "template", "template": "Follow-up: {item}",
                         "output_key": "followup"}),
            Step(id="summary", type=StepType.TRANSFORM, name="Summarise",
                 config={"op": "template",
                         "template": "Triage complete for {priority} case.",
                         "output_key": "summary"}),
        ],
        triggers=[Trigger(kind="cron", expression="*/30 * * * *"),
                  Trigger(kind="event", expression="ticket.created")],
        budget=Budget(max_tokens=10_000, max_cost_usd=2.00, max_steps=20),
        tags=["triage", "demo"],
    )


def budget_demo_workflow() -> WorkflowDefinition:
    """A workflow whose token budget is deliberately too small for its LLM step."""
    return WorkflowDefinition(
        id="budget-demo",
        name="Budget Cap Demo",
        tenant="acme",
        description="Two LLM steps against a 10-token budget; the run must halt "
                    "fail-closed on the second step.",
        steps=[
            Step(id="first", type=StepType.LLM, name="First call",
                 config={"prompt": "hi", "output_key": "first"}),
            Step(id="second", type=StepType.LLM, name="Second call",
                 config={"prompt": "Write a long detailed essay about workflows.",
                         "output_key": "second"}),
        ],
        budget=Budget(max_tokens=10, max_cost_usd=1.00, max_steps=5),
        tags=["demo", "budget"],
    )


def demo_workflows() -> list[WorkflowDefinition]:
    return [onboarding_workflow(), triage_workflow(), budget_demo_workflow()]
