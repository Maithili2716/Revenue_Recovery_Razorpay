"""Focused tests for Block 4: Adaptive Recovery Agent decision loop.

Tests the core decision pipeline:
- AgentContext construction
- Diagnosis for payment_failure signals
- Candidate generation
- Bandit selection
- End-to-end AdaptiveRecoveryAgent.decide()
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:test@localhost:5432/revenue_recovery_test",
)
os.environ.setdefault("RAZORPAY_KEY_ID", "test_key_id")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_key_secret")
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "test_webhook_secret")

from app.recovery.agent.bandit import ContextualBandit
from app.recovery.agent.candidates import generate_candidates
from app.recovery.agent.context import build_agent_context
from app.recovery.agent.diagnosis import diagnose
from app.recovery.agent.models import (
    ActionType,
    AgentContext,
    AgentDecision,
    CandidateAction,
    DecisionSource,
    Diagnosis,
    DiagnosisCategory,
    EligibilityStatus,
    FailureStage,
    build_decision_id,
)
from app.recovery.agent.service import AdaptiveRecoveryAgent
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

_NOW = datetime(2026, 9, 2, 10, 0, 0, tzinfo=timezone.utc)


def _make_signal(**overrides) -> RevenueSignal:
    defaults = dict(
        signal_id="sig_test123",
        merchant_id="acc_TestMerchant",
        customer_id=None,
        signal_type=SignalType.PAYMENT_FAILURE,
        status=SignalStatus.FAILED,
        amount_minor=50000,
        currency="INR",
        provider="razorpay",
        provider_event_id="evt_test123",
        provider_entity_id="pay_test123",
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
        urgency=Urgency.MEDIUM,
        reason_codes=["payment_failed", "failure_source:bank"],
        created_at=_NOW,
    )
    defaults.update(overrides)
    return RecoveryCase(**defaults)


# ---------------------------------------------------------------------------
# 1. Context
# ---------------------------------------------------------------------------


def test_build_agent_context_from_signal_and_case() -> None:
    signal = _make_signal()
    case = _make_case(signal)
    ctx = build_agent_context(signal, case)

    assert ctx.case_id == case.case_id
    assert ctx.signal_id == signal.signal_id
    assert ctx.merchant_id == signal.merchant_id
    assert ctx.amount_at_risk_minor == signal.amount_minor
    assert ctx.currency == "INR"
    assert ctx.failure_source == "bank"
    assert ctx.failure_step == "payment_authorization"
    assert ctx.payment_method == "card"
    assert ctx.previous_attempts == []
    assert ctx.recoverability == "likely"
    assert ctx.urgency == "medium"


def test_context_customer_id_none_when_unavailable() -> None:
    signal = _make_signal(customer_id=None)
    case = _make_case(signal)
    ctx = build_agent_context(signal, case)
    assert ctx.customer_id is None


# ---------------------------------------------------------------------------
# 2. Diagnosis
# ---------------------------------------------------------------------------


def test_diagnosis_bank_payment_authorization() -> None:
    """The real Razorpay example: failure_source=bank, failure_step=payment_authorization."""
    signal = _make_signal(failure_source="bank", failure_step="payment_authorization")
    case = _make_case(signal)
    ctx = build_agent_context(signal, case)
    diag = diagnose(ctx)

    assert diag.category == DiagnosisCategory.PAYMENT_FAILURE
    assert diag.primary_reason == "bank_decline"
    assert diag.failure_stage == FailureStage.PAYMENT_AUTHORIZATION
    assert diag.confidence == 0.9


def test_diagnosis_unknown_source() -> None:
    signal = _make_signal(failure_source=None, failure_step=None)
    case = _make_case(signal)
    ctx = build_agent_context(signal, case)
    diag = diagnose(ctx)

    assert diag.category == DiagnosisCategory.PAYMENT_FAILURE
    assert diag.primary_reason == "unknown_failure"
    assert diag.confidence == 0.4


# ---------------------------------------------------------------------------
# 3. Candidates
# ---------------------------------------------------------------------------


def test_payment_link_candidate_generated_for_payment_failure() -> None:
    signal = _make_signal()
    case = _make_case(signal)
    ctx = build_agent_context(signal, case)
    diag = diagnose(ctx)
    candidates = generate_candidates(ctx, diag)

    assert len(candidates) >= 1
    plink = next(c for c in candidates if c.capability_id == "payment_link_recovery")
    assert plink.action_type == ActionType.CREATE_PAYMENT_LINK
    assert plink.eligibility == EligibilityStatus.ELIGIBLE


# ---------------------------------------------------------------------------
# 4. Bandit
# ---------------------------------------------------------------------------


def test_bandit_selects_highest_priority_eligible() -> None:
    signal = _make_signal()
    case = _make_case(signal)
    ctx = build_agent_context(signal, case)
    diag = diagnose(ctx)
    candidates = generate_candidates(ctx, diag)
    bandit = ContextualBandit()

    selection = bandit.select(candidates, ctx)
    assert selection is not None
    assert selection.selected.capability_id == "payment_link_recovery"


def test_bandit_returns_none_when_no_eligible() -> None:
    ctx = AgentContext(
        case_id="case_empty",
        signal_id="sig_empty",
        merchant_id="acc_test",
        amount_at_risk_minor=100,
        currency="INR",
        signal_type="payment_failure",
        signal_occurred_at=_NOW,
        recoverability="likely",
        urgency="medium",
    )
    bandit = ContextualBandit()
    selection = bandit.select([], ctx)
    assert selection is None


# ---------------------------------------------------------------------------
# 5. End-to-end decision
# ---------------------------------------------------------------------------


def test_agent_decide_produces_decision() -> None:
    signal = _make_signal()
    case = _make_case(signal)
    agent = AdaptiveRecoveryAgent()

    decision = agent.decide(signal, case)

    assert decision is not None
    assert isinstance(decision, AgentDecision)
    assert decision.case_id == case.case_id
    assert decision.selected_capability_id == "payment_link_recovery"
    assert decision.selected_action_type == ActionType.CREATE_PAYMENT_LINK
    assert decision.decision_source == DecisionSource.CONTEXTUAL_BANDIT
    assert "payment_link_recovery" in decision.candidate_action_ids
    assert decision.decision_id.startswith("dec_")
    assert decision.diagnosis is not None
    assert decision.diagnosis.category == DiagnosisCategory.PAYMENT_FAILURE
    assert decision.diagnosis.primary_reason == "bank_decline"


def test_decision_id_is_deterministic() -> None:
    assert build_decision_id("case_abc") == build_decision_id("case_abc")


def test_different_cases_produce_different_decision_ids() -> None:
    assert build_decision_id("case_aaa") != build_decision_id("case_bbb")
