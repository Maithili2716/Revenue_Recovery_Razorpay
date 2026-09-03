"""Focused tests for multi-candidate adaptive selection.

Verifies:
1. No pending Payment Link → only payment_link_recovery candidate.
2. Pending Payment Link → both payment_link_recovery and payment_link_reminder.
3. Both candidates are passed to the bandit.
4. Bandit returns exactly one candidate.
5. Canonical context key remains unchanged through multi-candidate path.
6. Existing payment-link-reminder tests continue passing (via separate file).
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
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.recovery.agent.bandit import ContextualBandit
from app.recovery.agent.candidates import (
    generate_candidates,
    generate_candidates_with_context,
)
from app.recovery.agent.context import build_agent_context
from app.recovery.agent.diagnosis import diagnose
from app.recovery.agent.models import (
    ActionType,
    AgentContext,
    CandidateAction,
    DecisionSource,
    Diagnosis,
    DiagnosisCategory,
    EligibilityStatus,
    FailureStage,
)
from app.recovery.agent.service import AdaptiveRecoveryAgent
from app.recovery.learning.service import build_context_key
from app.recovery.learning.store import StrategyStore
from app.recovery.models import (
    RecoveryCase,
    Recoverability,
    RiskStatus,
    Urgency,
    build_case_id,
)
from app.signals.models import RevenueSignal, SignalStatus, SignalType

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 3, 9, 0, 0, tzinfo=timezone.utc)


def _make_signal(**overrides) -> RevenueSignal:
    defaults = dict(
        signal_id="sig_multi_test_001",
        merchant_id="acc_multi_test_merchant",
        customer_id=None,
        signal_type=SignalType.PAYMENT_FAILURE,
        status=SignalStatus.FAILED,
        amount_minor=50000,
        currency="INR",
        provider="razorpay",
        provider_event_id="evt_multi_test_001",
        provider_entity_id="pay_multi_test_001",
        reason="Payment failed",
        failure_source="bank",
        failure_step="payment_authorization",
        occurred_at=_NOW,
        raw_event_type="payment.failed",
        metadata={"method": "card"},
    )
    defaults.update(overrides)
    return RevenueSignal(**defaults)


def _make_case(signal: RevenueSignal | None = None, **overrides) -> RecoveryCase:
    sig = signal or _make_signal()
    defaults = dict(
        case_id=build_case_id(sig.signal_id),
        signal_id=sig.signal_id,
        merchant_id=sig.merchant_id,
        customer_id=sig.customer_id,
        amount_at_risk_minor=sig.amount_minor,
        currency=sig.currency,
        risk_status=RiskStatus.AT_RISK,
        recoverability=Recoverability.LIKELY,
        urgency=Urgency.LOW,
        reason_codes=["payment_failed", "failure_source:bank"],
        created_at=_NOW,
    )
    defaults.update(overrides)
    return RecoveryCase(**defaults)


def _make_agent_context(
    case_id: str = "case_multi_test_001",
    failure_source: str = "bank",
    urgency: str = "low",
) -> AgentContext:
    return AgentContext(
        case_id=case_id,
        signal_id="sig_multi_test_001",
        merchant_id="acc_multi_test_merchant",
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
        reason_codes=["payment_failed", "failure_source:bank"],
    )


def _make_diagnosis() -> Diagnosis:
    return Diagnosis(
        category=DiagnosisCategory.PAYMENT_FAILURE,
        primary_reason="bank_decline",
        failure_stage=FailureStage.PAYMENT_AUTHORIZATION,
        confidence=0.9,
        reason_codes=["payment_failed"],
    )


# ===========================================================================
# 1. No pending Payment Link → only payment_link_recovery
# ===========================================================================


class TestNoPendingLinkCandidates:
    def test_no_pending_link_generates_only_recovery(self):
        """Without a pending link, only payment_link_recovery is a candidate."""
        context = _make_agent_context()
        diagnosis = _make_diagnosis()

        candidates = generate_candidates_with_context(
            context, diagnosis, pending_payment_link_id=None
        )

        capability_ids = [c.capability_id for c in candidates]
        assert capability_ids == ["payment_link_recovery"]

    def test_no_pending_link_via_standard_generate(self):
        """The base generate_candidates also produces only recovery."""
        context = _make_agent_context()
        diagnosis = _make_diagnosis()

        candidates = generate_candidates(context, diagnosis)

        capability_ids = [c.capability_id for c in candidates]
        assert "payment_link_recovery" in capability_ids
        assert "payment_link_reminder" not in capability_ids

    def test_agent_decide_no_pending_link(self):
        """Agent.decide without pending link → only recovery candidate."""
        signal = _make_signal()
        case = _make_case(signal)
        agent = AdaptiveRecoveryAgent()

        decision = agent.decide(signal, case)

        assert decision is not None
        assert decision.selected_capability_id == "payment_link_recovery"
        assert decision.candidate_action_ids == ["payment_link_recovery"]


# ===========================================================================
# 2. Pending Payment Link → both candidates
# ===========================================================================


class TestPendingLinkCandidates:
    def test_pending_link_generates_both_candidates(self):
        """With a pending link, both recovery and reminder are candidates."""
        context = _make_agent_context()
        diagnosis = _make_diagnosis()

        candidates = generate_candidates_with_context(
            context, diagnosis, pending_payment_link_id="plink_pending_001"
        )

        capability_ids = {c.capability_id for c in candidates}
        assert capability_ids == {"payment_link_recovery", "payment_link_reminder"}

    def test_both_candidates_are_eligible(self):
        """Both candidates must have ELIGIBLE status."""
        context = _make_agent_context()
        diagnosis = _make_diagnosis()

        candidates = generate_candidates_with_context(
            context, diagnosis, pending_payment_link_id="plink_pending_001"
        )

        for c in candidates:
            assert c.eligibility == EligibilityStatus.ELIGIBLE, (
                f"{c.capability_id} is not ELIGIBLE"
            )

    def test_agent_decide_with_pending_link(self):
        """Agent.decide with pending link → both candidates considered."""
        signal = _make_signal()
        case = _make_case(signal)
        agent = AdaptiveRecoveryAgent()

        decision = agent.decide(
            signal, case, pending_payment_link_id="plink_pending_001"
        )

        assert decision is not None
        assert set(decision.candidate_action_ids) == {
            "payment_link_recovery",
            "payment_link_reminder",
        }


# ===========================================================================
# 3. Both candidates are passed to the bandit
# ===========================================================================


class TestBanditReceivesBothCandidates:
    def test_bandit_receives_two_eligible_candidates(self):
        """The bandit's select() is called with 2 eligible candidates."""
        context = _make_agent_context()
        diagnosis = _make_diagnosis()

        candidates = generate_candidates_with_context(
            context, diagnosis, pending_payment_link_id="plink_pending_001"
        )

        bandit = ContextualBandit()
        selection = bandit.select(candidates, context)

        assert selection is not None
        # With no learning store, bandit uses deterministic priority.
        # The selected action must be one of the two candidates.
        assert selection.selected.capability_id in {
            "payment_link_recovery",
            "payment_link_reminder",
        }

    def test_bandit_with_empty_learning_store_uses_deterministic(self):
        """With a learning store but NO verified outcomes, bandit uses deterministic."""
        context = _make_agent_context()
        diagnosis = _make_diagnosis()

        candidates = generate_candidates_with_context(
            context, diagnosis, pending_payment_link_id="plink_pending_001"
        )

        store = StrategyStore()
        bandit = ContextualBandit(learning_store=store)
        selection = bandit.select(candidates, context)

        assert selection is not None
        # No verified outcomes → deterministic fallback.
        assert "deterministic_priority" in selection.selection_reason
        assert selection.selected.capability_id in {
            "payment_link_recovery",
            "payment_link_reminder",
        }

    def test_bandit_with_verified_learning_uses_thompson(self):
        """With verified outcomes in the store, bandit uses Thompson Sampling."""
        context = _make_agent_context()
        diagnosis = _make_diagnosis()

        candidates = generate_candidates_with_context(
            context, diagnosis, pending_payment_link_id="plink_pending_001"
        )

        store = StrategyStore()
        context_key = build_context_key(
            signal_type="payment_failure", failure_source="bank", urgency="low",
        )
        # Record a verified success for payment_link_recovery.
        store.record_success("acc_multi_test_merchant", "payment_link_recovery", context_key)

        bandit = ContextualBandit(learning_store=store)
        selection = bandit.select(candidates, context)

        assert selection is not None
        assert "thompson_sampling" in selection.selection_reason
        assert selection.selected.capability_id in {
            "payment_link_recovery",
            "payment_link_reminder",
        }


# ===========================================================================
# 4. Bandit returns exactly one candidate
# ===========================================================================


class TestBanditSelectsExactlyOne:
    def test_bandit_returns_single_selection(self):
        """Bandit must return exactly one BanditSelection, not a list."""
        context = _make_agent_context()
        diagnosis = _make_diagnosis()

        candidates = generate_candidates_with_context(
            context, diagnosis, pending_payment_link_id="plink_pending_001"
        )

        bandit = ContextualBandit()
        selection = bandit.select(candidates, context)

        # selection is a single BanditSelection, not a list.
        assert selection is not None
        assert hasattr(selection, "selected")
        assert isinstance(selection.selected, CandidateAction)

    def test_agent_decision_has_single_selected_capability(self):
        """The AgentDecision selects exactly one capability_id."""
        signal = _make_signal()
        case = _make_case(signal)
        agent = AdaptiveRecoveryAgent()

        decision = agent.decide(
            signal, case, pending_payment_link_id="plink_pending_001"
        )

        assert decision is not None
        # selected_capability_id is a single string, not a list.
        assert isinstance(decision.selected_capability_id, str)
        assert decision.selected_capability_id in {
            "payment_link_recovery",
            "payment_link_reminder",
        }


# ===========================================================================
# 5. Canonical context key remains unchanged
# ===========================================================================


class TestContextKeyPreservedMultiCandidate:
    def test_context_key_unchanged_with_pending_link(self):
        """The canonical context key must not change when a pending link exists."""
        signal = _make_signal(failure_source="bank")
        case = _make_case(signal, urgency=Urgency.LOW)
        agent = AdaptiveRecoveryAgent()

        # Without pending link.
        decision_no_pending = agent.decide(signal, case)
        # With pending link.
        decision_with_pending = agent.decide(
            signal, case, pending_payment_link_id="plink_pending_001"
        )

        assert decision_no_pending is not None
        assert decision_with_pending is not None

        # The decision_context must contain the same signal-derived fields.
        assert (
            decision_no_pending.decision_context["failure_source"]
            == decision_with_pending.decision_context["failure_source"]
            == "bank"
        )
        assert (
            decision_no_pending.decision_context["urgency"]
            == decision_with_pending.decision_context["urgency"]
        )

    def test_build_context_key_not_affected_by_pending_link(self):
        """build_context_key is purely signal-derived, unaffected by pending link."""
        key = build_context_key(
            signal_type="payment_failure",
            failure_source="bank",
            urgency="low",
        )
        assert key == "payment_failure|bank|low"


# ===========================================================================
# 6. Observability: logs show correct candidate counts
# ===========================================================================


class TestMultiCandidateObservability:
    def test_candidate_generation_log_shows_two_candidates(self, caplog):
        """agent_candidates_generated + agent_reminder_candidate_added logged."""
        with caplog.at_level(logging.INFO):
            context = _make_agent_context()
            diagnosis = _make_diagnosis()
            candidates = generate_candidates_with_context(
                context, diagnosis, pending_payment_link_id="plink_pending_001"
            )

        # The base generate_candidates log should show candidate_count >= 1.
        base_logs = [
            r for r in caplog.records
            if r.message == "agent_candidates_generated"
        ]
        assert len(base_logs) >= 1

        # The reminder-added log should appear.
        reminder_logs = [
            r for r in caplog.records
            if r.message == "agent_reminder_candidate_added"
        ]
        assert len(reminder_logs) == 1
        assert reminder_logs[0].total_candidates == 2

    def test_bandit_log_shows_two_eligible(self, caplog):
        """agent_bandit_decision log shows eligible_count=2."""
        with caplog.at_level(logging.INFO):
            context = _make_agent_context()
            diagnosis = _make_diagnosis()
            candidates = generate_candidates_with_context(
                context, diagnosis, pending_payment_link_id="plink_pending_001"
            )
            bandit = ContextualBandit()
            bandit.select(candidates, context)

        bandit_logs = [
            r for r in caplog.records
            if r.message == "agent_bandit_decision"
        ]
        assert len(bandit_logs) == 1
        r = bandit_logs[0]
        assert r.eligible_count == 2
        assert set(r.eligible_candidates) == {
            "payment_link_recovery",
            "payment_link_reminder",
        }
        assert r.selected_strategy in {
            "payment_link_recovery",
            "payment_link_reminder",
        }

    def test_single_candidate_log_shows_one_eligible(self, caplog):
        """Without pending link, bandit log shows eligible_count=1."""
        with caplog.at_level(logging.INFO):
            context = _make_agent_context()
            diagnosis = _make_diagnosis()
            candidates = generate_candidates_with_context(
                context, diagnosis, pending_payment_link_id=None
            )
            bandit = ContextualBandit()
            bandit.select(candidates, context)

        bandit_logs = [
            r for r in caplog.records
            if r.message == "agent_bandit_decision"
        ]
        assert len(bandit_logs) == 1
        assert bandit_logs[0].eligible_count == 1
        assert bandit_logs[0].selected_strategy == "payment_link_recovery"
