"""Focused tests for merchant-specific adaptive bandit learning.

Verifies:
1.  One candidate → selected directly.
2.  Two candidates + no historical learning → deterministic fallback.
3.  Two candidates + historical verified outcomes → Thompson Sampling path.
4.  Merchant-specific isolation.
5.  Context-specific isolation.
6.  Verified success/failure statistics influence the selection path.
7.  PENDING/UNKNOWN outcomes do not make learning available.
8.  Exactly one candidate is selected.
9.  Existing context key remains unchanged.
10. Existing multi-candidate and learning tests remain compatible.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:test@localhost:5432/revenue_recovery_test",
)
os.environ.setdefault("RAZORPAY_KEY_ID", "test_key_id")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_key_secret")
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "test_webhook_secret")

import logging
import random
from datetime import datetime, timezone

import pytest

from app.recovery.agent.bandit import ContextualBandit
from app.recovery.agent.candidates import generate_candidates_with_context
from app.recovery.agent.models import (
    ActionType,
    AgentContext,
    CandidateAction,
    Diagnosis,
    DiagnosisCategory,
    EligibilityStatus,
    FailureStage,
)
from app.recovery.learning.service import LearningService, build_context_key
from app.recovery.learning.store import StrategyStore
from app.recovery.verification.models import VerificationStatus, VerifiedOutcome


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)


def _make_context(
    case_id: str = "case_bandit_test_001",
    merchant_id: str = "merchant_bandit_A",
    failure_source: str = "bank",
    urgency: str = "low",
) -> AgentContext:
    return AgentContext(
        case_id=case_id,
        signal_id="sig_bandit_test_001",
        merchant_id=merchant_id,
        customer_id=None,
        amount_at_risk_minor=50000,
        currency="INR",
        signal_type="payment_failure",
        failure_reason="Payment failed",
        failure_source=failure_source,
        failure_step="payment_authorization",
        payment_method="card",
        signal_occurred_at=_NOW,
        recoverability="likely",
        urgency=urgency,
        reason_codes=["payment_failed"],
    )


def _make_diagnosis() -> Diagnosis:
    return Diagnosis(
        category=DiagnosisCategory.PAYMENT_FAILURE,
        primary_reason="bank_decline",
        failure_stage=FailureStage.PAYMENT_AUTHORIZATION,
        confidence=0.9,
        reason_codes=["payment_failed"],
    )


def _two_candidates(context: AgentContext | None = None) -> list[CandidateAction]:
    """Generate both recovery + reminder candidates."""
    ctx = context or _make_context()
    return generate_candidates_with_context(
        ctx, _make_diagnosis(), pending_payment_link_id="plink_test_001"
    )


def _one_candidate(context: AgentContext | None = None) -> list[CandidateAction]:
    """Generate only the recovery candidate."""
    ctx = context or _make_context()
    return generate_candidates_with_context(
        ctx, _make_diagnosis(), pending_payment_link_id=None
    )


def _context_key(failure_source: str = "bank", urgency: str = "low") -> str:
    return build_context_key(
        signal_type="payment_failure",
        failure_source=failure_source,
        urgency=urgency,
    )


def _make_verified_outcome(
    *,
    status: VerificationStatus,
    case_id: str = "case_bandit_test",
    execution_id: str = "exec_bandit_test",
    capability_id: str = "payment_link_recovery",
    amount_recovered: int = 0,
) -> VerifiedOutcome:
    return VerifiedOutcome(
        execution_id=execution_id,
        case_id=case_id,
        capability_id=capability_id,
        status=status,
        amount_recovered_minor=amount_recovered,
        amount_at_risk_minor=50000,
        currency="INR",
        reason="test outcome",
    )


# ===========================================================================
# 1. One candidate → selected directly
# ===========================================================================


class TestSingleCandidate:
    def test_single_candidate_selected_directly(self):
        """With 1 candidate, bandit selects it regardless of learning."""
        context = _make_context()
        candidates = _one_candidate(context)
        store = StrategyStore()

        bandit = ContextualBandit(learning_store=store)
        selection = bandit.select(candidates, context)

        assert selection is not None
        assert selection.selected.capability_id == "payment_link_recovery"
        assert "deterministic_priority" in selection.selection_reason

    def test_single_candidate_even_with_learning_data(self):
        """Learning data exists but only 1 candidate → still deterministic."""
        context = _make_context()
        candidates = _one_candidate(context)
        store = StrategyStore()
        store.record_success("merchant_bandit_A", "payment_link_recovery", _context_key())

        bandit = ContextualBandit(learning_store=store)
        selection = bandit.select(candidates, context)

        assert selection is not None
        assert "deterministic_priority" in selection.selection_reason


# ===========================================================================
# 2. Two candidates + no historical learning → deterministic fallback
# ===========================================================================


class TestNoPriorLearning:
    def test_no_learning_uses_deterministic(self):
        """Fresh store (only priors) → deterministic_priority."""
        context = _make_context()
        candidates = _two_candidates(context)
        store = StrategyStore()

        bandit = ContextualBandit(learning_store=store)
        selection = bandit.select(candidates, context)

        assert selection is not None
        assert "deterministic_priority" in selection.selection_reason

    def test_no_learning_store_uses_deterministic(self):
        """No learning store at all → deterministic_priority."""
        context = _make_context()
        candidates = _two_candidates(context)

        bandit = ContextualBandit(learning_store=None)
        selection = bandit.select(candidates, context)

        assert selection is not None
        assert "deterministic_priority" in selection.selection_reason

    def test_no_learning_log(self, caplog):
        """Log shows strategy=deterministic_priority, learning_available=False."""
        context = _make_context()
        candidates = _two_candidates(context)

        with caplog.at_level(logging.INFO):
            bandit = ContextualBandit(learning_store=StrategyStore())
            bandit.select(candidates, context)

        logs = [r for r in caplog.records if r.message == "agent_bandit_decision"]
        assert len(logs) == 1
        r = logs[0]
        assert r.strategy == "deterministic_priority"
        assert r.learning_available is False


# ===========================================================================
# 3. Two candidates + historical verified outcomes → Thompson Sampling
# ===========================================================================


class TestThompsonSamplingActivated:
    def test_verified_success_activates_thompson(self):
        """One verified success → thompson_sampling path is used."""
        context = _make_context()
        candidates = _two_candidates(context)
        store = StrategyStore()
        ck = _context_key()
        store.record_success("merchant_bandit_A", "payment_link_recovery", ck)

        bandit = ContextualBandit(learning_store=store)
        selection = bandit.select(candidates, context)

        assert selection is not None
        assert "thompson_sampling" in selection.selection_reason

    def test_verified_failure_activates_thompson(self):
        """One verified failure → thompson_sampling path is used."""
        context = _make_context()
        candidates = _two_candidates(context)
        store = StrategyStore()
        ck = _context_key()
        store.record_failure("merchant_bandit_A", "payment_link_reminder", ck)

        bandit = ContextualBandit(learning_store=store)
        selection = bandit.select(candidates, context)

        assert selection is not None
        assert "thompson_sampling" in selection.selection_reason

    def test_thompson_log(self, caplog):
        """Log shows strategy=thompson_sampling, learning_available=True."""
        context = _make_context()
        candidates = _two_candidates(context)
        store = StrategyStore()
        ck = _context_key()
        store.record_success("merchant_bandit_A", "payment_link_recovery", ck)

        with caplog.at_level(logging.INFO):
            bandit = ContextualBandit(learning_store=store)
            bandit.select(candidates, context)

        logs = [r for r in caplog.records if r.message == "agent_bandit_decision"]
        assert len(logs) == 1
        r = logs[0]
        assert r.strategy == "thompson_sampling"
        assert r.learning_available is True

    def test_thompson_log_includes_candidate_statistics(self, caplog):
        """Log includes per-candidate statistics when learning is available."""
        context = _make_context()
        candidates = _two_candidates(context)
        store = StrategyStore()
        ck = _context_key()
        store.record_success("merchant_bandit_A", "payment_link_recovery", ck)

        with caplog.at_level(logging.INFO):
            bandit = ContextualBandit(learning_store=store)
            bandit.select(candidates, context)

        logs = [r for r in caplog.records if r.message == "agent_bandit_decision"]
        assert len(logs) == 1
        assert hasattr(logs[0], "candidate_statistics")
        stats = logs[0].candidate_statistics
        assert "payment_link_recovery" in stats
        assert "payment_link_reminder" in stats
        # payment_link_recovery had 1 verified success → total_verified_trials >= 1
        assert stats["payment_link_recovery"]["total_verified_trials"] >= 1


# ===========================================================================
# 4. Merchant-specific isolation
# ===========================================================================


class TestMerchantIsolation:
    def test_merchant_a_learning_does_not_affect_merchant_b(self):
        """MerchantA stats must not influence MerchantB selection."""
        store = StrategyStore()
        ck = _context_key()

        # Record learning for merchant A only.
        for _ in range(10):
            store.record_success("merchant_bandit_A", "payment_link_recovery", ck)

        # Merchant B has no learning → should use deterministic.
        context_b = _make_context(merchant_id="merchant_bandit_B")
        candidates_b = _two_candidates(context_b)

        bandit = ContextualBandit(learning_store=store)
        selection_b = bandit.select(candidates_b, context_b)

        assert selection_b is not None
        assert "deterministic_priority" in selection_b.selection_reason

    def test_merchant_a_uses_thompson_while_b_uses_deterministic(self):
        """Merchant A with learning uses Thompson; B without uses deterministic."""
        store = StrategyStore()
        ck = _context_key()
        store.record_success("merchant_bandit_A", "payment_link_recovery", ck)

        bandit = ContextualBandit(learning_store=store)

        # Merchant A → Thompson
        ctx_a = _make_context(merchant_id="merchant_bandit_A")
        sel_a = bandit.select(_two_candidates(ctx_a), ctx_a)
        assert "thompson_sampling" in sel_a.selection_reason

        # Merchant B → Deterministic
        ctx_b = _make_context(merchant_id="merchant_bandit_B")
        sel_b = bandit.select(_two_candidates(ctx_b), ctx_b)
        assert "deterministic_priority" in sel_b.selection_reason


# ===========================================================================
# 5. Context-specific isolation
# ===========================================================================


class TestContextIsolation:
    def test_different_context_keys_do_not_share_statistics(self):
        """Learning for bank|low must not affect bank|high."""
        store = StrategyStore()
        ck_low = _context_key(urgency="low")
        # Record learning for the bank|low context.
        store.record_success("merchant_bandit_A", "payment_link_recovery", ck_low)

        bandit = ContextualBandit(learning_store=store)

        # bank|low → Thompson (has learning)
        ctx_low = _make_context(urgency="low")
        sel_low = bandit.select(_two_candidates(ctx_low), ctx_low)
        assert "thompson_sampling" in sel_low.selection_reason

        # bank|high → Deterministic (no learning for this context)
        ctx_high = _make_context(urgency="high")
        sel_high = bandit.select(_two_candidates(ctx_high), ctx_high)
        assert "deterministic_priority" in sel_high.selection_reason

    def test_different_failure_sources_isolated(self):
        """Learning for bank failures does not affect issuer failures."""
        store = StrategyStore()
        ck_bank = _context_key(failure_source="bank")
        store.record_success("merchant_bandit_A", "payment_link_recovery", ck_bank)

        bandit = ContextualBandit(learning_store=store)

        ctx_issuer = _make_context(failure_source="issuer")
        sel = bandit.select(_two_candidates(ctx_issuer), ctx_issuer)
        assert "deterministic_priority" in sel.selection_reason


# ===========================================================================
# 6. Verified success/failure statistics influence the selection path
# ===========================================================================


class TestStatisticsInfluenceSelection:
    def test_strong_success_rate_biases_selection(self):
        """Heavily successful arm should be selected more often with a seed."""
        store = StrategyStore()
        ck = _context_key()

        # Give payment_link_recovery strong success rate.
        for _ in range(20):
            store.record_success("merchant_bandit_A", "payment_link_recovery", ck)
        # Give payment_link_reminder strong failure rate.
        for _ in range(20):
            store.record_failure("merchant_bandit_A", "payment_link_reminder", ck)

        context = _make_context()
        bandit = ContextualBandit(learning_store=store)

        # Run 50 selections with controlled seed.
        recovery_selections = 0
        for i in range(50):
            random.seed(i)
            candidates = _two_candidates(context)
            selection = bandit.select(candidates, context)
            assert selection is not None
            assert "thompson_sampling" in selection.selection_reason
            if selection.selected.capability_id == "payment_link_recovery":
                recovery_selections += 1

        # recovery should be selected the vast majority of the time.
        assert recovery_selections > 35, (
            f"Expected recovery to dominate but got only {recovery_selections}/50"
        )

    def test_learning_service_updates_influence_bandit(self):
        """Recording outcomes through LearningService makes stats available."""
        store = StrategyStore()
        learning = LearningService(store=store)
        ck = _context_key()

        # Record a verified RECOVERED outcome.
        outcome = _make_verified_outcome(status=VerificationStatus.RECOVERED)
        learning.record_outcome(
            merchant_id="merchant_bandit_A",
            capability_id="payment_link_recovery",
            context_key=ck,
            verified_outcome=outcome,
        )

        context = _make_context()
        candidates = _two_candidates(context)
        bandit = ContextualBandit(learning_store=store)
        selection = bandit.select(candidates, context)

        assert "thompson_sampling" in selection.selection_reason


# ===========================================================================
# 7. PENDING/UNKNOWN outcomes do not make learning available
# ===========================================================================


class TestPendingUnknownDoNotActivateLearning:
    def test_pending_outcome_does_not_activate_thompson(self):
        """PENDING verified outcome does not count as learning data."""
        store = StrategyStore()
        learning = LearningService(store=store)
        ck = _context_key()

        outcome = _make_verified_outcome(status=VerificationStatus.PENDING)
        updated = learning.record_outcome(
            merchant_id="merchant_bandit_A",
            capability_id="payment_link_recovery",
            context_key=ck,
            verified_outcome=outcome,
        )

        # PENDING should not update learning.
        assert updated is False

        context = _make_context()
        candidates = _two_candidates(context)
        bandit = ContextualBandit(learning_store=store)
        selection = bandit.select(candidates, context)

        # No verified outcomes → deterministic.
        assert "deterministic_priority" in selection.selection_reason

    def test_unknown_outcome_does_not_activate_thompson(self):
        """UNKNOWN verified outcome does not count as learning data."""
        store = StrategyStore()
        learning = LearningService(store=store)
        ck = _context_key()

        outcome = _make_verified_outcome(status=VerificationStatus.UNKNOWN)
        updated = learning.record_outcome(
            merchant_id="merchant_bandit_A",
            capability_id="payment_link_recovery",
            context_key=ck,
            verified_outcome=outcome,
        )

        assert updated is False

        context = _make_context()
        candidates = _two_candidates(context)
        bandit = ContextualBandit(learning_store=store)
        selection = bandit.select(candidates, context)

        assert "deterministic_priority" in selection.selection_reason


# ===========================================================================
# 8. Exactly one candidate is selected
# ===========================================================================


class TestExactlyOneSelected:
    def test_one_candidate_from_single(self):
        context = _make_context()
        candidates = _one_candidate(context)
        bandit = ContextualBandit(learning_store=StrategyStore())
        selection = bandit.select(candidates, context)

        assert selection is not None
        assert isinstance(selection.selected, CandidateAction)

    def test_one_candidate_from_two_deterministic(self):
        context = _make_context()
        candidates = _two_candidates(context)
        bandit = ContextualBandit(learning_store=StrategyStore())
        selection = bandit.select(candidates, context)

        assert selection is not None
        assert isinstance(selection.selected, CandidateAction)

    def test_one_candidate_from_two_thompson(self):
        store = StrategyStore()
        store.record_success("merchant_bandit_A", "payment_link_recovery", _context_key())
        context = _make_context()
        candidates = _two_candidates(context)
        bandit = ContextualBandit(learning_store=store)
        selection = bandit.select(candidates, context)

        assert selection is not None
        assert isinstance(selection.selected, CandidateAction)


# ===========================================================================
# 9. Existing context key remains unchanged
# ===========================================================================


class TestContextKeyUnchanged:
    def test_context_key_format(self):
        """Canonical context key format is preserved."""
        key = _context_key()
        assert key == "payment_failure|bank|low"

    def test_bandit_uses_same_context_key(self, caplog):
        """Bandit's internal context key matches the learning service format."""
        context = _make_context()
        candidates = _two_candidates(context)
        store = StrategyStore()
        store.record_success("merchant_bandit_A", "payment_link_recovery", _context_key())

        with caplog.at_level(logging.INFO):
            bandit = ContextualBandit(learning_store=store)
            bandit.select(candidates, context)

        logs = [r for r in caplog.records if r.message == "agent_bandit_decision"]
        assert len(logs) == 1
        assert logs[0].context_key == "payment_failure|bank|low"


# ===========================================================================
# 10. Existing multi-candidate tests remain compatible
# ===========================================================================


class TestBackwardCompatibility:
    def test_no_store_single_candidate(self):
        """Original path: no store, 1 candidate → deterministic."""
        context = _make_context()
        candidates = _one_candidate(context)
        bandit = ContextualBandit()
        selection = bandit.select(candidates, context)
        assert selection is not None
        assert selection.selected.capability_id == "payment_link_recovery"

    def test_no_store_two_candidates(self):
        """Original path: no store, 2 candidates → deterministic (priority)."""
        context = _make_context()
        candidates = _two_candidates(context)
        bandit = ContextualBandit()
        selection = bandit.select(candidates, context)
        assert selection is not None
        assert "deterministic_priority" in selection.selection_reason

    def test_empty_candidates(self):
        """No candidates → None."""
        context = _make_context()
        bandit = ContextualBandit()
        selection = bandit.select([], context)
        assert selection is None
