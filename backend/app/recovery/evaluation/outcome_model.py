"""Deterministic simulated external-world outcomes for held-out evaluation."""

from __future__ import annotations

import hashlib

from app.recovery.evaluation.models import EvaluationCase, SimulatedOutcome


def simulate_outcome(
    evaluation_case: EvaluationCase, selected_strategy: str, *, seed: int
) -> SimulatedOutcome:
    """Apply the same deterministic outcome rule to either evaluation arm.

    The threshold is part of the synthetic held-out context.  The stable hash
    supplies a reproducible pseudo-random draw without using global RNG state.
    """
    threshold = evaluation_case.strategy_effectiveness.get(selected_strategy, 0)
    digest = hashlib.sha256(
        f"{seed}|{evaluation_case.recovery_case.case_id}|{selected_strategy}".encode()
    ).digest()
    draw = int.from_bytes(digest[:4], "big") % 100
    recovered = draw < threshold
    return SimulatedOutcome(
        recovered=recovered,
        amount_recovered_minor=(
            evaluation_case.recovery_case.amount_at_risk_minor if recovered else 0
        ),
    )
