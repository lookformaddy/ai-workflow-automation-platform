import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from awap.audit import AuditLog
from awap.demo_data import budget_demo_workflow, onboarding_workflow, triage_workflow
from awap.engine import Engine, VirtualClock
from awap.models import Budget, Step, StepType, WorkflowDefinition
from awap.providers import ScriptedLLMProvider, ScriptedToolProvider, demo_tool_registry
from awap.store import WorkflowStore


@pytest.fixture()
def clock():
    return VirtualClock()


@pytest.fixture()
def llm():
    return ScriptedLLMProvider({
        "welcome": "Welcome aboard!",
        "escalation": "Escalation summary.",
        "auto-reply": "Thanks — we're on it.",
    })


@pytest.fixture()
def tools():
    return demo_tool_registry()


@pytest.fixture()
def engine(clock, llm, tools):
    return Engine(store=WorkflowStore(), audit=AuditLog(),
                  llm=llm, tools=tools, clock=clock)


@pytest.fixture()
def onboarding():
    return onboarding_workflow()


@pytest.fixture()
def triage():
    return triage_workflow()


@pytest.fixture()
def budget_demo():
    return budget_demo_workflow()


def simple_workflow(steps, budget=None, **kwargs):
    return WorkflowDefinition(
        id=kwargs.pop("id", "wf-simple"),
        name=kwargs.pop("name", "Simple"),
        steps=steps,
        budget=budget or Budget(),
        **kwargs,
    )


def llm_step(step_id="s1", prompt="hello", output_key="out", **overrides):
    cfg = {"prompt": prompt, "output_key": output_key}
    cfg.update(overrides.pop("config", {}))
    return Step(id=step_id, type=StepType.LLM, config=cfg, **overrides)


def tool_step(step_id="t1", tool="crm_lookup", output_key="record", **overrides):
    cfg = {"tool": tool, "args": {}, "output_key": output_key}
    cfg.update(overrides.pop("config", {}))
    return Step(id=step_id, type=StepType.TOOL, config=cfg, **overrides)
