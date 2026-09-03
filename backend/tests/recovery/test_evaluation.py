"""Focused tests for the isolated simulated held-out evaluation benchmark."""

import random
from unittest.mock import patch

from app.recovery.agent.bandit import ContextualBandit
from app.recovery.agent.models import EligibilityStatus
from app.recovery.evaluation.baseline import select_baseline_strategy
from app.recovery.evaluation.dataset import build_held_out_dataset
from app.recovery.evaluation.evaluator import _seed_simulated_calibration, evaluate
from app.recovery.evaluation.outcome_model import simulate_outcome
from app.recovery.evaluation.strategies import evaluation_candidates, select_evaluation_strategy
from app.recovery.learning.store import StrategyStore
from app.signals.service import get_learning_store


def _case(context: str):
    return next(item for item in build_held_out_dataset() if item.recovery_context == context)


def test_dataset_is_deterministic_held_out_and_has_three_contexts() -> None:
    first = build_held_out_dataset()
    second = build_held_out_dataset()

    assert len(first) == 50
    assert [(item.signal.model_dump(), item.recovery_case.model_dump(), item.recovery_context) for item in first] == [
        (item.signal.model_dump(), item.recovery_case.model_dump(), item.recovery_context) for item in second
    ]
    assert {item.recovery_context for item in first} == {
        "new_payment_failure", "existing_payment_link", "overdue_invoice",
    }
    assert all(item.signal.provider == "simulated_evaluation" for item in first)
    assert all(item.signal.metadata["simulated"] is True for item in first)


def test_evaluation_candidate_eligibility_varies_by_context() -> None:
    new = {candidate.capability_id: candidate.eligibility for candidate in evaluation_candidates(_case("new_payment_failure"))}
    existing = {candidate.capability_id: candidate.eligibility for candidate in evaluation_candidates(_case("existing_payment_link"))}
    invoice = {candidate.capability_id: candidate.eligibility for candidate in evaluation_candidates(_case("overdue_invoice"))}

    assert new == {
        "payment_link_recovery": EligibilityStatus.ELIGIBLE,
        "payment_link_reminder": EligibilityStatus.INELIGIBLE,
        "invoice_recovery": EligibilityStatus.INELIGIBLE,
    }
    assert existing["payment_link_recovery"] == EligibilityStatus.ELIGIBLE
    assert existing["payment_link_reminder"] == EligibilityStatus.ELIGIBLE
    assert existing["invoice_recovery"] == EligibilityStatus.INELIGIBLE
    assert invoice["invoice_recovery"] == EligibilityStatus.ELIGIBLE
    assert invoice["payment_link_recovery"] == EligibilityStatus.INELIGIBLE


def test_invoice_strategy_is_selected_for_an_invoice_context() -> None:
    selection, decision = select_evaluation_strategy(_case("overdue_invoice"), ContextualBandit(StrategyStore()))

    assert selection is not None
    assert decision is not None
    assert decision.selected_capability_id == "invoice_recovery"


def test_reminder_can_be_selected_for_existing_payment_link_context() -> None:
    store = StrategyStore()
    case = _case("existing_payment_link")
    context_key = f"payment_failure|{case.signal.failure_source}|{case.recovery_case.urgency.value}"
    for _ in range(100):
        store.record_failure(case.recovery_case.merchant_id, "payment_link_recovery", context_key)
        store.record_success(case.recovery_case.merchant_id, "payment_link_reminder", context_key)
    random.seed(7)
    _, decision = select_evaluation_strategy(case, ContextualBandit(store))

    assert decision is not None
    assert decision.selected_capability_id == "payment_link_reminder"


def test_payment_link_recovery_remains_available_and_baseline_is_fixed() -> None:
    new_candidates = {candidate.capability_id for candidate in evaluation_candidates(_case("new_payment_failure")) if candidate.eligibility == EligibilityStatus.ELIGIBLE}

    assert "payment_link_recovery" in new_candidates
    assert {select_baseline_strategy() for _ in build_held_out_dataset()} == {"payment_link_recovery"}


def test_outcome_model_is_deterministic_for_every_strategy() -> None:
    for item in (_case("new_payment_failure"), _case("existing_payment_link"), _case("overdue_invoice")):
        for strategy in item.strategy_effectiveness:
            assert simulate_outcome(item, strategy, seed=7) == simulate_outcome(item, strategy, seed=7)


def test_calibration_is_merchant_specific_and_evaluation_local() -> None:
    store = StrategyStore()
    _seed_simulated_calibration(store)
    context_key = "payment_failure|bank|medium"
    merchant_a = store.get_or_create("merchant_eval_a", "payment_link_recovery", context_key)
    merchant_b = store.get_or_create("merchant_eval_b", "payment_link_recovery", context_key)

    assert (merchant_a.successes, merchant_a.failures) != (merchant_b.successes, merchant_b.failures)


def test_each_arm_evaluates_same_batch_and_reports_invoice_performance() -> None:
    dataset = build_held_out_dataset()
    result = evaluate(dataset)

    assert result.batch_size == len(dataset)
    assert result.baseline_recovered_cases + result.baseline_not_recovered_cases == len(dataset)
    assert result.adaptive_recovered_cases + result.adaptive_not_recovered_cases + result.escalation_count == len(dataset)
    assert result.baseline_strategy_performance["payment_link_recovery"].selected_count == len(dataset)
    assert result.adaptive_strategy_performance["invoice_recovery"].selected_count > 0
    assert result.adaptive_strategy_performance["payment_link_reminder"].selected_count > 0
    assert result.adaptive_strategy_performance["payment_link_recovery"].selected_count > 0


def test_evaluation_does_not_mutate_live_learning_or_invoke_live_capabilities() -> None:
    live_store = get_learning_store()
    before = [item.model_dump() for item in live_store.get_all()]

    with patch("app.recovery.capabilities.executor.CapabilityExecutor.execute", side_effect=AssertionError("live execution is forbidden")):
        payload = evaluate(build_held_out_dataset()).as_dict()

    assert [item.model_dump() for item in live_store.get_all()] == before
    assert payload["simulation_label"] == "simulated_held_out_evaluation"
    assert "verification_status" not in payload
    assert "learning_updated" not in payload


def test_metrics_and_repeated_runs_are_consistent() -> None:
    first = evaluate(build_held_out_dataset())
    second = evaluate(build_held_out_dataset())

    assert first.absolute_improvement_minor == first.adaptive_amount_recovered_minor - first.baseline_amount_recovered_minor
    assert 0.0 <= first.baseline_recovery_rate <= 1.0
    assert 0.0 <= first.adaptive_recovery_rate <= 1.0
    assert first.as_dict() == second.as_dict()
