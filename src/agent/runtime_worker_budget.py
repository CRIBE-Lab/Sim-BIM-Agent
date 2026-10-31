"""Expose the read-only worker's remaining allowance in its runtime context."""

from src.agent_runtime.context import StateEntry


def update_observer_budget(engine, event, raw):
    remaining = {"model_calls": max(0, engine.limits.model_calls - engine.counts["model_calls"]),
        "tool_calls": max(0, engine.limits.tool_calls - engine.counts["tool_calls"]),
        "tokens": engine.task_budget.available.tokens,
        # Unlimited time is null on the wire; infinity is only an internal
        # Runtime comparison value and cannot enter the JSON evidence store.
        "seconds": max(0.0, engine._remaining()) if engine.limits.seconds is not None else None,
        "scope": "remaining child allowance; root budget may be lower"}
    previous = next((entry for entry in engine.context.state
                     if entry.key == "observer-remaining-budget"), None)
    engine.context.set_state(StateEntry(key="observer-remaining-budget", category="constraint",
        revision=previous.revision + 1 if previous else 1,
        value=remaining, epistemic_status="computed",
        source_refs=(engine.store.source("observer-remaining-budget", remaining),)))
