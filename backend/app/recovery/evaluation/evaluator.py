"""Held-out evaluator using the real agent decision interface, in isolation."""

from __future__ import annotations

import logging
import random
from collections import defaultdict

from app.policy.engine import PolicyEngine
from app.policy.models import PolicyVerdict
from app.recovery.agent.bandit import ContextualBandit
from app.recovery.evaluation.baseline import select_baseline_strategy
from app.recovery.evaluation.dataset import evaluation_calibration_profiles
from app.recovery.evaluation.models import EvaluationCase, EvaluationResult, StrategyPerformance
from app.recovery.evaluation.outcome_model import simulate_outcome
from app.recovery.evaluation.strategies import select_evaluation_strategy
from app.recovery.learning.store import StrategyStore

logger = logging.getLogger(__name__)

_EVALUATION_SEED = 20260903
_CAPABILITIES = frozenset({"payment_link_recovery", "payment_link_reminder", "invoice_recovery"})


def evaluate(cases: list[EvaluationCase], *, seed: int = _EVALUATION_SEED) -> EvaluationResult:
    """Compare the fixed baseline with the existing agent on the same cases."""
    logger.info("evaluation_started", extra={"batch_size": len(cases)})
    evaluation_store = StrategyStore()
    _seed_simulated_calibration(evaluation_store)
    bandit = ContextualBandit(learning_store=evaluation_store)
    policy = PolicyEngine(registered_capability_ids=_CAPABILITIES)
    baseline = _PerformanceAccumulator()
    adaptive = _PerformanceAccumulator()
    escalations = 0
    policy_violations = 0

    # ContextualBandit uses random.betavariate. Preserve the application's RNG
    # state so this benchmark is deterministic and has no side effects.
    previous_random_state = random.getstate()
    random.seed(seed)
    try:
        for item in cases:
            baseline_strategy = select_baseline_strategy()
            baseline.record(
                baseline_strategy,
                simulate_outcome(item, baseline_strategy, seed=seed),
            )

            _, decision = select_evaluation_strategy(item, bandit)
            if decision is None:
                escalations += 1
                continue
            policy_decision = policy.evaluate(decision, item.recovery_case)
            if policy_decision.verdict != PolicyVerdict.ALLOW:
                policy_violations += 1
                escalations += 1
                continue
            adaptive.record(
                decision.selected_capability_id,
                simulate_outcome(item, decision.selected_capability_id, seed=seed),
            )
    finally:
        random.setstate(previous_random_state)

    result = _build_result(cases, baseline, adaptive, escalations, policy_violations)
    logger.info(
        "evaluation_completed",
        extra={
            "batch_size": result.batch_size,
            "baseline_recovered_amount": result.baseline_amount_recovered_minor,
            "adaptive_recovered_amount": result.adaptive_amount_recovered_minor,
            "baseline_recovery_rate": result.baseline_recovery_rate,
            "adaptive_recovery_rate": result.adaptive_recovery_rate,
        },
    )
    return result


def _seed_simulated_calibration(store: StrategyStore) -> None:
    """Seed an evaluation-local store with separate simulated history only."""
    for merchant_id, contexts in evaluation_calibration_profiles().items():
        for source, urgency, effectiveness in contexts:
            context_key = f"payment_failure|{source}|{urgency}"
            for capability_id, rate in effectiveness.items():
                for _ in range(20):
                    if _ < rate // 5:
                        store.record_success(merchant_id, capability_id, context_key)
                    else:
                        store.record_failure(merchant_id, capability_id, context_key)


class _PerformanceAccumulator:
    def __init__(self) -> None:
        self.amount_recovered_minor = 0
        self.recovered_cases = 0
        self.not_recovered_cases = 0
        self.by_strategy: dict[str, dict[str, int]] = defaultdict(
            lambda: {"selected_count": 0, "recovered_cases": 0, "not_recovered_cases": 0, "amount_recovered_minor": 0}
        )

    def record(self, strategy: str, outcome) -> None:
        values = self.by_strategy[strategy]
        values["selected_count"] += 1
        if outcome.recovered:
            self.recovered_cases += 1
            self.amount_recovered_minor += outcome.amount_recovered_minor
            values["recovered_cases"] += 1
            values["amount_recovered_minor"] += outcome.amount_recovered_minor
        else:
            self.not_recovered_cases += 1
            values["not_recovered_cases"] += 1

    def strategy_performance(self) -> dict[str, StrategyPerformance]:
        return {
            strategy: StrategyPerformance(**values)
            for strategy, values in sorted(self.by_strategy.items())
        }


def _build_result(cases, baseline, adaptive, escalations, policy_violations) -> EvaluationResult:
    total = sum(item.recovery_case.amount_at_risk_minor for item in cases)
    baseline_rate = baseline.amount_recovered_minor / total if total else 0.0
    adaptive_rate = adaptive.amount_recovered_minor / total if total else 0.0
    improvement = adaptive.amount_recovered_minor - baseline.amount_recovered_minor
    relative = improvement / baseline.amount_recovered_minor if baseline.amount_recovered_minor else 0.0
    return EvaluationResult(
        batch_size=len(cases), total_amount_at_risk_minor=total,
        baseline_amount_recovered_minor=baseline.amount_recovered_minor,
        adaptive_amount_recovered_minor=adaptive.amount_recovered_minor,
        baseline_recovery_rate=baseline_rate, adaptive_recovery_rate=adaptive_rate,
        absolute_improvement_minor=improvement, relative_improvement=relative,
        baseline_recovered_cases=baseline.recovered_cases,
        baseline_not_recovered_cases=baseline.not_recovered_cases,
        adaptive_recovered_cases=adaptive.recovered_cases,
        adaptive_not_recovered_cases=adaptive.not_recovered_cases,
        baseline_strategy_performance=baseline.strategy_performance(),
        adaptive_strategy_performance=adaptive.strategy_performance(),
        escalation_count=escalations, policy_safety_violation_count=policy_violations,
    )
