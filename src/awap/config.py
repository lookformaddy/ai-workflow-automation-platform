"""Settings loaded from the environment (AWAP_ prefix).

Core logic reads plain values here so the engine stays dependency-free;
the FastAPI layer validates the same values through pydantic models.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for the platform."""

    # Default budgets applied when a workflow definition omits its own.
    default_max_tokens: int = 20_000
    default_max_cost_usd: float = 2.00
    default_max_latency_ms: int = 120_000
    default_max_steps: int = 100
    # Pricing used by the scripted LLM provider (per 1k tokens).
    llm_input_cost_per_1k: float = 0.002
    llm_output_cost_per_1k: float = 0.006
    # Where the store persists state.
    store_path: str = "data/store.json"
    audit_path: str = "data/audit.jsonl"
    # Approval gates: default expiry and escalation policy.
    approval_default_expiry_s: int = 3_600
    approval_on_expiry: str = "escalate"  # escalate | deny
    # Engine safety.
    max_loop_iterations: int = 50

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            default_max_tokens=_int("AWAP_DEFAULT_MAX_TOKENS", 20_000),
            default_max_cost_usd=_float("AWAP_DEFAULT_MAX_COST_USD", 2.00),
            default_max_latency_ms=_int("AWAP_DEFAULT_MAX_LATENCY_MS", 120_000),
            default_max_steps=_int("AWAP_DEFAULT_MAX_STEPS", 100),
            llm_input_cost_per_1k=_float("AWAP_LLM_INPUT_COST_PER_1K", 0.002),
            llm_output_cost_per_1k=_float("AWAP_LLM_OUTPUT_COST_PER_1K", 0.006),
            store_path=os.environ.get("AWAP_STORE_PATH", "data/store.json"),
            audit_path=os.environ.get("AWAP_AUDIT_PATH", "data/audit.jsonl"),
            approval_default_expiry_s=_int("AWAP_APPROVAL_EXPIRY_S", 3_600),
            approval_on_expiry=os.environ.get("AWAP_APPROVAL_ON_EXPIRY", "escalate"),
            max_loop_iterations=_int("AWAP_MAX_LOOP_ITERATIONS", 50),
        )
