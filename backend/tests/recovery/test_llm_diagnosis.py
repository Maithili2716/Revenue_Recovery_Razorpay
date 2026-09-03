"""Focused tests for LLM-based diagnosis using Grok (xAI).

Tests:
A. Grok provider can be constructed from configuration.
B. Correct diagnosis output maps into the existing Diagnosis model.
C. LLM failure triggers deterministic fallback.
D. Malformed LLM output triggers deterministic fallback.
E. Timeout triggers deterministic fallback.
F. AdaptiveRecoveryAgent uses the LLM diagnosis when available.
G. Existing agent behavior still produces a valid AgentDecision.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, PropertyMock

# Set env vars BEFORE any app imports.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:test@localhost:5432/revenue_recovery_test",
)
os.environ.setdefault("RAZORPAY_KEY_ID", "test_key_id")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_key_secret")
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "test_webhook_secret")

from app.recovery.agent.llm_diagnosis import (
    GrokDiagnosisProvider,
    _parse_llm_response,
)
from app.recovery.agent.models import (
    ActionType,
    AgentContext,
    AgentDecision,
    DecisionSource,
    Diagnosis,
    DiagnosisCategory,
    FailureStage,
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


def _make_context(**overrides) -> AgentContext:
    defaults = dict(
        case_id="case_test123",
        signal_id="sig_test123",
        merchant_id="acc_TestMerchant",
        amount_at_risk_minor=50000,
        currency="INR",
        signal_type="payment_failure",
        failure_reason="Payment failed",
        failure_source="bank",
        failure_step="payment_authorization",
        payment_method="card",
        signal_occurred_at=_NOW,
        recoverability="likely",
        urgency="medium",
        reason_codes=["payment_failed", "failure_source:bank"],
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


# ---------------------------------------------------------------------------
# A. Grok provider can be constructed from configuration
# ---------------------------------------------------------------------------


def test_grok_provider_construction() -> None:
    """GrokDiagnosisProvider can be instantiated with api_key and model."""
    with patch("app.recovery.agent.llm_diagnosis.OpenAI") as mock_openai:
        provider = GrokDiagnosisProvider(api_key="test_key", model="grok-3-mini-fast")

    assert provider._model == "grok-3-mini-fast"
    mock_openai.assert_called_once()
    call_kwargs = mock_openai.call_args
    assert call_kwargs.kwargs["api_key"] == "test_key"
    assert "x.ai" in call_kwargs.kwargs["base_url"]


# ---------------------------------------------------------------------------
# B. Valid structured response → Diagnosis
# ---------------------------------------------------------------------------


def test_parse_valid_llm_json_response() -> None:
    """A valid JSON response from the LLM is correctly parsed into a Diagnosis."""
    raw_json = json.dumps({
        "category": "payment_failure",
        "primary_reason": "bank_decline",
        "failure_stage": "payment_authorization",
        "confidence": 0.85,
        "reason_codes": ["bank_decline", "payment_failed"],
        "details": "Payment was declined by the issuing bank at authorization stage.",
    })

    diagnosis = _parse_llm_response(raw_json)

    assert isinstance(diagnosis, Diagnosis)
    assert diagnosis.category == DiagnosisCategory.PAYMENT_FAILURE
    assert diagnosis.primary_reason == "bank_decline"
    assert diagnosis.failure_stage == FailureStage.PAYMENT_AUTHORIZATION
    assert diagnosis.confidence == 0.85
    assert diagnosis.diagnosis_source == "llm"


def test_parse_llm_response_with_code_fences() -> None:
    """LLM responses wrapped in markdown code fences are handled."""
    raw = '```json\n{"category": "payment_failure", "primary_reason": "gateway_error", "failure_stage": "payment_processing", "confidence": 0.7, "reason_codes": [], "details": "Gateway error."}\n```'

    diagnosis = _parse_llm_response(raw)

    assert diagnosis.category == DiagnosisCategory.PAYMENT_FAILURE
    assert diagnosis.primary_reason == "gateway_error"
    assert diagnosis.diagnosis_source == "llm"


def test_parse_llm_response_clamps_confidence() -> None:
    """Confidence values outside 0–1 are clamped."""
    raw_json = json.dumps({
        "category": "payment_failure",
        "primary_reason": "bank_decline",
        "failure_stage": "payment_authorization",
        "confidence": 1.5,
        "reason_codes": [],
        "details": None,
    })

    diagnosis = _parse_llm_response(raw_json)
    assert diagnosis.confidence == 1.0


def test_parse_llm_response_corrects_unknown_enum() -> None:
    """Unknown enum values are safely defaulted to 'unknown'."""
    raw_json = json.dumps({
        "category": "some_invented_category",
        "primary_reason": "something",
        "failure_stage": "invented_stage",
        "confidence": 0.5,
        "reason_codes": [],
        "details": None,
    })

    diagnosis = _parse_llm_response(raw_json)
    assert diagnosis.category == DiagnosisCategory.UNKNOWN
    assert diagnosis.failure_stage == FailureStage.UNKNOWN


# ---------------------------------------------------------------------------
# B2. Invalid LLM output is rejected
# ---------------------------------------------------------------------------


def test_parse_invalid_json_raises() -> None:
    """Non-JSON output from the LLM raises ValueError."""
    try:
        _parse_llm_response("This is not JSON at all")
        assert False, "Should have raised"
    except (ValueError, json.JSONDecodeError):
        pass


def test_parse_missing_required_fields_raises() -> None:
    """JSON missing required fields raises a validation error."""
    raw_json = json.dumps({"category": "payment_failure"})
    try:
        _parse_llm_response(raw_json)
        assert False, "Should have raised"
    except Exception:
        pass


# ---------------------------------------------------------------------------
# C. LLM failure → deterministic fallback
# ---------------------------------------------------------------------------


def _build_mock_grok_provider() -> GrokDiagnosisProvider:
    """Create a GrokDiagnosisProvider with a mocked OpenAI client."""
    with patch("app.recovery.agent.llm_diagnosis.OpenAI"):
        provider = GrokDiagnosisProvider(api_key="fake_key", model="grok-3-mini-fast")
    return provider


def test_grok_provider_falls_back_on_api_error() -> None:
    """When the Grok API call fails, the provider returns a deterministic diagnosis."""
    context = _make_context()

    provider = _build_mock_grok_provider()
    provider._client = MagicMock()
    provider._client.chat.completions.create.side_effect = RuntimeError("API unavailable")

    diagnosis = provider.diagnose(context)

    assert isinstance(diagnosis, Diagnosis)
    assert diagnosis.diagnosis_source == "deterministic_fallback"
    assert diagnosis.category == DiagnosisCategory.PAYMENT_FAILURE


def test_grok_provider_falls_back_on_empty_response() -> None:
    """When Grok returns an empty response, fallback is used."""
    context = _make_context()

    provider = _build_mock_grok_provider()
    mock_client = MagicMock()

    # Simulate empty content from Grok
    mock_message = MagicMock()
    mock_message.content = ""
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_client.chat.completions.create.return_value = mock_response
    provider._client = mock_client

    diagnosis = provider.diagnose(context)

    assert diagnosis.diagnosis_source == "deterministic_fallback"


# ---------------------------------------------------------------------------
# D. Malformed LLM output → deterministic fallback
# ---------------------------------------------------------------------------


def test_grok_provider_falls_back_on_invalid_json() -> None:
    """When Grok returns invalid JSON, fallback is used."""
    context = _make_context()

    provider = _build_mock_grok_provider()
    mock_client = MagicMock()

    mock_message = MagicMock()
    mock_message.content = "I'm sorry, I can't help with that."
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_client.chat.completions.create.return_value = mock_response
    provider._client = mock_client

    diagnosis = provider.diagnose(context)

    assert diagnosis.diagnosis_source == "deterministic_fallback"


# ---------------------------------------------------------------------------
# E. Timeout → deterministic fallback
# ---------------------------------------------------------------------------


def test_grok_provider_falls_back_on_timeout() -> None:
    """When the Grok API times out, fallback is used."""
    from openai import APITimeoutError

    context = _make_context()

    provider = _build_mock_grok_provider()
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = APITimeoutError(request=MagicMock())
    provider._client = mock_client

    diagnosis = provider.diagnose(context)

    assert isinstance(diagnosis, Diagnosis)
    assert diagnosis.diagnosis_source == "deterministic_fallback"


# ---------------------------------------------------------------------------
# F. Agent uses LLM diagnosis when available
# ---------------------------------------------------------------------------


def test_agent_uses_llm_diagnosis_when_provider_available() -> None:
    """AdaptiveRecoveryAgent uses LLM diagnosis when a provider is injected."""
    signal = _make_signal()
    case = _make_case(signal)

    mock_provider = MagicMock()
    mock_provider.diagnose.return_value = Diagnosis(
        category=DiagnosisCategory.PAYMENT_FAILURE,
        primary_reason="bank_decline",
        failure_stage=FailureStage.PAYMENT_AUTHORIZATION,
        confidence=0.9,
        reason_codes=["bank_decline"],
        details="LLM diagnosed bank decline.",
        diagnosis_source="llm",
    )

    with patch("app.recovery.agent.service._build_llm_provider", return_value=mock_provider):
        agent = AdaptiveRecoveryAgent()

    decision = agent.decide(signal, case)

    assert decision is not None
    assert isinstance(decision, AgentDecision)
    assert decision.decision_context["diagnosis_source"] == "llm"
    mock_provider.diagnose.assert_called_once()


# ---------------------------------------------------------------------------
# G. Existing behavior still works without LLM
# ---------------------------------------------------------------------------


def test_agent_works_without_llm_provider() -> None:
    """When no LLM provider is available, the agent uses deterministic diagnosis."""
    signal = _make_signal()
    case = _make_case(signal)

    with patch("app.recovery.agent.service._build_llm_provider", return_value=None):
        agent = AdaptiveRecoveryAgent()

    decision = agent.decide(signal, case)

    assert decision is not None
    assert isinstance(decision, AgentDecision)
    assert decision.case_id == case.case_id
    assert decision.selected_capability_id == "payment_link_recovery"
    assert decision.selected_action_type == ActionType.CREATE_PAYMENT_LINK
    assert decision.decision_source == DecisionSource.CONTEXTUAL_BANDIT
    assert decision.decision_context["diagnosis_source"] == "deterministic"


# ---------------------------------------------------------------------------
# H. Grok provider produces correct Diagnosis from valid API response
# ---------------------------------------------------------------------------


def test_grok_provider_returns_valid_diagnosis() -> None:
    """GrokDiagnosisProvider correctly maps a valid Grok API response to Diagnosis."""
    context = _make_context()

    provider = _build_mock_grok_provider()
    mock_client = MagicMock()

    valid_json = json.dumps({
        "category": "payment_failure",
        "primary_reason": "bank_decline",
        "failure_stage": "payment_authorization",
        "confidence": 0.92,
        "reason_codes": ["bank_decline"],
        "details": "Bank declined the transaction at authorization.",
    })

    mock_message = MagicMock()
    mock_message.content = valid_json
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_client.chat.completions.create.return_value = mock_response
    provider._client = mock_client

    diagnosis = provider.diagnose(context)

    assert isinstance(diagnosis, Diagnosis)
    assert diagnosis.diagnosis_source == "llm"
    assert diagnosis.category == DiagnosisCategory.PAYMENT_FAILURE
    assert diagnosis.primary_reason == "bank_decline"
    assert diagnosis.failure_stage == FailureStage.PAYMENT_AUTHORIZATION
    assert diagnosis.confidence == 0.92
