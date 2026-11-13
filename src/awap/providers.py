"""Pluggable providers — scripted, deterministic, no network, no keys.

`ScriptedLLMProvider` returns canned responses keyed by a prompt fragment
(or a per-step override), with deterministic token counts and a simulated
duration. `ScriptedToolProvider` registers named tools whose handlers are
plain callables; a tool can be scripted to fail N times before succeeding,
which is how retry behaviour is exercised deterministically.
`TransformProvider` implements the pure transform ops.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ProviderResult:
    output: Any
    tokens_in: int = 0
    tokens_out: int = 0
    duration_ms: int = 0


class ProviderError(Exception):
    """Raised by a provider when a call fails (transient or permanent)."""


def _token_estimate(text: str) -> int:
    """Deterministic token estimate: ~4 chars per token, min 1."""
    return max(1, len(text) // 4)


class ScriptedLLMProvider:
    """Deterministic stand-in for an LLM call.

    `responses` maps a prompt substring to the canned reply. `default`
    is used when nothing matches. `fail_times` makes the next N calls
    raise ProviderError (transient failure) before succeeding.
    """

    def __init__(
        self,
        responses: dict[str, str] | None = None,
        default: str = "Acknowledged.",
        fail_times: int = 0,
        fixed_duration_ms: int = 120,
    ) -> None:
        self.responses = responses or {}
        self.default = default
        self.fail_times = fail_times
        self.fixed_duration_ms = fixed_duration_ms
        self.calls: list[dict[str, Any]] = []

    def complete(self, prompt: str, model: str = "scripted") -> ProviderResult:
        self.calls.append({"prompt": prompt, "model": model})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ProviderError("simulated transient LLM failure")
        reply = self.default
        for fragment, canned in self.responses.items():
            if fragment in prompt:
                reply = canned
                break
        return ProviderResult(
            output=reply,
            tokens_in=_token_estimate(prompt),
            tokens_out=_token_estimate(reply),
            duration_ms=self.fixed_duration_ms,
        )


@dataclass
class ToolSpec:
    handler: Callable[[dict[str, Any]], Any]
    fail_times: int = 0
    duration_ms: int = 40


class ScriptedToolProvider:
    """Registry of named tools backed by plain callables."""

    def __init__(self) -> None:
        self.tools: dict[str, ToolSpec] = {}
        self.calls: list[dict[str, Any]] = []

    def register(self, name: str, handler: Callable[[dict[str, Any]], Any],
                 fail_times: int = 0, duration_ms: int = 40) -> None:
        self.tools[name] = ToolSpec(handler, fail_times, duration_ms)

    def call(self, name: str, args: dict[str, Any]) -> ProviderResult:
        self.calls.append({"tool": name, "args": dict(args)})
        spec = self.tools.get(name)
        if spec is None:
            raise ProviderError(f"unknown tool: {name}")
        if spec.fail_times > 0:
            spec.fail_times -= 1
            raise ProviderError(f"simulated transient failure for tool {name}")
        try:
            output = spec.handler(dict(args))
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"tool {name} failed: {exc}") from exc
        return ProviderResult(
            output=output,
            tokens_in=0,
            tokens_out=0,
            duration_ms=spec.duration_ms,
        )


class TransformProvider:
    """Pure, deterministic transforms over the run context."""

    @staticmethod
    def apply(op: str, context: dict[str, Any], config: dict[str, Any]) -> Any:
        if op == "uppercase":
            return str(context.get(config.get("field", ""), "")).upper()
        if op == "lowercase":
            return str(context.get(config.get("field", ""), "")).lower()
        if op == "template":
            template = config.get("template", "")
            try:
                return template.format(**{k: v for k, v in context.items()
                                          if isinstance(v, (str, int, float, bool))})
            except KeyError as exc:
                raise ProviderError(f"template references missing key: {exc}") from exc
        if op == "extract":
            source = context.get(config.get("source", ""), {})
            field = config.get("field", "")
            if isinstance(source, dict):
                return source.get(field)
            raise ProviderError(f"extract source {config.get('source')!r} is not a mapping")
        if op == "concat":
            parts = [str(context.get(k, "")) for k in config.get("fields", [])]
            return config.get("separator", " ").join(parts)
        raise ProviderError(f"unknown transform op: {op}")


def stable_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def demo_tool_registry() -> ScriptedToolProvider:
    """Synthetic tools used by the demo workflows (generic domains only)."""
    provider = ScriptedToolProvider()
    provider.register("crm_lookup", lambda a: {
        "account_id": a.get("account_id", "ACCT-0001"),
        "tier": "standard",
        "region": "north",
        "open_cases": 2,
    })
    provider.register("inventory_check", lambda a: {
        "sku": a.get("sku", "SKU-100"),
        "in_stock": True,
        "quantity": 42,
        "warehouse": "WH-NORTH-1",
    })
    provider.register("send_notification", lambda a: {
        "channel": a.get("channel", "email"),
        "recipient": a.get("recipient", "ops@example.test"),
        "sent": True,
    })
    provider.register("score_request", lambda a: {
        "score": 0.82 if a.get("priority", "low") == "high" else 0.41,
        "model": "scripted-scorer-v1",
    })
    return provider
