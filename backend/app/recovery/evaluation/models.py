"""Models for the deterministic held-out evaluation benchmark."""

from __future__ import annotations

from dataclasses import dataclass

from app.recovery.models import RecoveryCase
from app.signals.models import RevenueSignal


@dataclass(frozen=True)
class EvaluationCase:
    """A synthetic, held-out case with no real customer data."""

    signal: RevenueSignal
    recovery_case: RecoveryCase
    pending_payment_link_id: str | None
    recovery_context: str
    strategy_effectiveness: dict[str, int]


@dataclass(frozen=True)
class SimulatedOutcome:
    """An external-world simulation outcome, never a verified recovery."""

    recovered: bool
    amount_recovered_minor: int


@dataclass(frozen=True)
class StrategyPerformance:
    selected_count: int
    recovered_cases: int
    not_recovered_cases: int
    amount_recovered_minor: int

    def as_dict(self) -> dict[str, int]:
        return {
            "selected_count": self.selected_count,
            "recovered_cases": self.recovered_cases,
            "not_recovered_cases": self.not_recovered_cases,
            "amount_recovered_minor": self.amount_recovered_minor,
        }


@dataclass(frozen=True)
class EvaluationResult:
    """Aggregated result for a single simulated held-out batch."""

    batch_size: int
    total_amount_at_risk_minor: int
    baseline_amount_recovered_minor: int
    adaptive_amount_recovered_minor: int
    baseline_recovery_rate: float
    adaptive_recovery_rate: float
    absolute_improvement_minor: int
    relative_improvement: float
    baseline_recovered_cases: int
    baseline_not_recovered_cases: int
    adaptive_recovered_cases: int
    adaptive_not_recovered_cases: int
    baseline_strategy_performance: dict[str, StrategyPerformance]
    adaptive_strategy_performance: dict[str, StrategyPerformance]
    escalation_count: int
    policy_safety_violation_count: int
    simulation_label: str = "simulated_held_out_evaluation"

    def as_dict(self) -> dict[str, object]:
        return {
            "simulation_label": self.simulation_label,
            "batch_size": self.batch_size,
            "total_amount_at_risk_minor": self.total_amount_at_risk_minor,
            "baseline_amount_recovered_minor": self.baseline_amount_recovered_minor,
            "adaptive_amount_recovered_minor": self.adaptive_amount_recovered_minor,
            "baseline_recovery_rate": self.baseline_recovery_rate,
            "adaptive_recovery_rate": self.adaptive_recovery_rate,
            "absolute_improvement_minor": self.absolute_improvement_minor,
            "relative_improvement": self.relative_improvement,
            "baseline_recovered_cases": self.baseline_recovered_cases,
            "baseline_not_recovered_cases": self.baseline_not_recovered_cases,
            "adaptive_recovered_cases": self.adaptive_recovered_cases,
            "adaptive_not_recovered_cases": self.adaptive_not_recovered_cases,
            "baseline_strategy_performance": {
                key: value.as_dict()
                for key, value in self.baseline_strategy_performance.items()
            },
            "adaptive_strategy_performance": {
                key: value.as_dict()
                for key, value in self.adaptive_strategy_performance.items()
            },
            "escalation_count": self.escalation_count,
            "policy_safety_violation_count": self.policy_safety_violation_count,
        }
