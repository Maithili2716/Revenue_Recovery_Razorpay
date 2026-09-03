"""Focused tests for the Capability Execution block.

Tests:
A. Registered capability can be resolved
B. Unknown capability is rejected
C. Policy blocks an invalid recovery case
D. Successful payment-link capability returns status=executed, provider_reference present
E. Razorpay/API failure returns status=failed
F. Execution result does NOT claim money recovered
G. AgentDecision routes through Policy → Registry → Capability
H. Webhook remains independent of execution latency
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.integrations.razorpay.client import PaymentLinkResponse
from app.policy.engine import PolicyEngine
from app.policy.models import PolicyVerdict
from app.recovery.agent.models import (
    ActionType,
    AgentDecision,
    DecisionSource,
)
from app.recovery.capabilities.executor import CapabilityExecutor
from app.recovery.capabilities.models import (
    ExecutionContext,
    ExecutionResult,
    ExecutionStatus,
    RecoveryCapability,
)
from app.recovery.capabilities.payment_link import PaymentLinkRecoveryCapability
from app.recovery.capabilities.registry import CapabilityRegistry
from app.recovery.models import (
    RecoveryCase,
    Recoverability,
    RiskStatus,
    Urgency,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_case(
    *,
    risk_status: RiskStatus = RiskStatus.AT_RISK,
    amount: int = 50000,
    currency: str = "INR",
    merchant_id: str = "acc_test_merchant",
) -> RecoveryCase:
    return RecoveryCase(
        case_id="case_test_abc123",
        signal_id="sig_test_xyz789",
        merchant_id=merchant_id,
        customer_id="cust_test_001",
        amount_at_risk_minor=amount,
        currency=currency,
        risk_status=risk_status,
        recoverability=Recoverability.LIKELY,
        urgency=Urgency.MEDIUM,
        reason_codes=["payment_failed"],
        created_at=datetime.now(timezone.utc),
    )


def _make_decision(
    *,
    capability_id: str = "payment_link_recovery",
    case_id: str = "case_test_abc123",
) -> AgentDecision:
    return AgentDecision(
        decision_id="dec_test_001",
        case_id=case_id,
        selected_capability_id=capability_id,
        selected_action_type=ActionType.CREATE_PAYMENT_LINK,
        reason="Test decision",
        candidate_action_ids=[capability_id],
        decision_context={"signal_type": "payment_failure"},
        decision_source=DecisionSource.CONTEXTUAL_BANDIT,
    )


def _make_mock_razorpay_client(*, success: bool = True) -> MagicMock:
    client = MagicMock()
    if success:
        client.create_payment_link.return_value = PaymentLinkResponse(
            success=True,
            payment_link_id="plink_test_abc123",
            short_url="https://rzp.io/i/test123",
            status="created",
            raw_response={"id": "plink_test_abc123"},
            http_status_code=200,
        )
    else:
        client.create_payment_link.return_value = PaymentLinkResponse(
            success=False,
            error_message="Bad Request: invalid amount",
            http_status_code=400,
        )
    return client


def _build_executor(
    mock_client: MagicMock | None = None,
) -> CapabilityExecutor:
    """Build a fully wired executor with the mock Razorpay client."""
    client = mock_client or _make_mock_razorpay_client()
    registry = CapabilityRegistry()
    registry.register(PaymentLinkRecoveryCapability(client))
    policy = PolicyEngine(registered_capability_ids=registry.registered_ids)
    return CapabilityExecutor(registry=registry, policy_engine=policy)


# ===========================================================================
# A. Registered capability can be resolved
# ===========================================================================


class TestCapabilityRegistry:
    def test_registered_capability_resolved(self):
        registry = CapabilityRegistry()
        client = _make_mock_razorpay_client()
        cap = PaymentLinkRecoveryCapability(client)
        registry.register(cap)

        resolved = registry.get("payment_link_recovery")
        assert resolved is not None
        assert resolved.capability_id == "payment_link_recovery"
        assert resolved.action_type == "create_payment_link"

    # B. Unknown capability is rejected
    def test_unknown_capability_returns_none(self):
        registry = CapabilityRegistry()
        assert registry.get("nonexistent_capability") is None

    def test_duplicate_registration_raises(self):
        registry = CapabilityRegistry()
        client = _make_mock_razorpay_client()
        cap = PaymentLinkRecoveryCapability(client)
        registry.register(cap)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(cap)


# ===========================================================================
# C. Policy blocks an invalid recovery case
# ===========================================================================


class TestPolicyEngine:
    def test_policy_allows_valid_case(self):
        policy = PolicyEngine(
            registered_capability_ids=frozenset({"payment_link_recovery"})
        )
        decision = _make_decision()
        case = _make_case()

        result = policy.evaluate(decision, case)
        assert result.verdict == PolicyVerdict.ALLOW

    def test_policy_blocks_not_at_risk(self):
        policy = PolicyEngine(
            registered_capability_ids=frozenset({"payment_link_recovery"})
        )
        decision = _make_decision()
        case = _make_case(risk_status=RiskStatus.NOT_AT_RISK)

        result = policy.evaluate(decision, case)
        assert result.verdict == PolicyVerdict.BLOCK
        assert any("at_risk" in r.lower() for r in result.reasons)

    def test_policy_blocks_zero_amount(self):
        policy = PolicyEngine(
            registered_capability_ids=frozenset({"payment_link_recovery"})
        )
        decision = _make_decision()
        case = _make_case(amount=0)

        result = policy.evaluate(decision, case)
        assert result.verdict == PolicyVerdict.BLOCK
        assert any("positive" in r.lower() for r in result.reasons)

    def test_policy_blocks_missing_merchant(self):
        policy = PolicyEngine(
            registered_capability_ids=frozenset({"payment_link_recovery"})
        )
        decision = _make_decision()
        case = _make_case(merchant_id="")

        result = policy.evaluate(decision, case)
        assert result.verdict == PolicyVerdict.BLOCK
        assert any("merchant" in r.lower() for r in result.reasons)

    def test_policy_blocks_missing_currency(self):
        policy = PolicyEngine(
            registered_capability_ids=frozenset({"payment_link_recovery"})
        )
        decision = _make_decision()
        case = _make_case(currency="")

        result = policy.evaluate(decision, case)
        assert result.verdict == PolicyVerdict.BLOCK
        assert any("currency" in r.lower() for r in result.reasons)

    def test_policy_blocks_unregistered_capability(self):
        policy = PolicyEngine(
            registered_capability_ids=frozenset({"payment_link_recovery"})
        )
        decision = _make_decision(capability_id="unknown_capability")
        case = _make_case()

        result = policy.evaluate(decision, case)
        assert result.verdict == PolicyVerdict.BLOCK
        assert any("not registered" in r.lower() for r in result.reasons)


# ===========================================================================
# D. Successful payment-link capability
# ===========================================================================


class TestPaymentLinkCapability:
    def test_successful_execution_returns_executed(self):
        mock_client = _make_mock_razorpay_client(success=True)
        cap = PaymentLinkRecoveryCapability(mock_client)

        context = ExecutionContext(
            case_id="case_test_abc123",
            decision_id="dec_test_001",
            merchant_id="acc_test_merchant",
            customer_id="cust_test_001",
            amount_minor=50000,
            currency="INR",
            capability_id="payment_link_recovery",
            action_type="create_payment_link",
            signal_id="sig_test_xyz789",
        )

        result = cap.execute(context)

        assert result.status == ExecutionStatus.EXECUTED
        assert result.provider_reference == "plink_test_abc123"
        assert result.provider == "razorpay"
        assert result.case_id == "case_test_abc123"
        assert result.capability_id == "payment_link_recovery"
        assert result.execution_id.startswith("exec_")

    # E. API failure returns status=failed
    def test_api_failure_returns_failed(self):
        mock_client = _make_mock_razorpay_client(success=False)
        cap = PaymentLinkRecoveryCapability(mock_client)

        context = ExecutionContext(
            case_id="case_test_abc123",
            decision_id="dec_test_001",
            merchant_id="acc_test_merchant",
            amount_minor=50000,
            currency="INR",
            capability_id="payment_link_recovery",
            action_type="create_payment_link",
            signal_id="sig_test_xyz789",
        )

        result = cap.execute(context)

        assert result.status == ExecutionStatus.FAILED
        assert result.provider_reference is None
        assert result.error_message is not None

    def test_exception_during_api_call_returns_failed(self):
        mock_client = MagicMock()
        mock_client.create_payment_link.side_effect = RuntimeError("Connection lost")
        cap = PaymentLinkRecoveryCapability(mock_client)

        context = ExecutionContext(
            case_id="case_test_abc123",
            decision_id="dec_test_001",
            merchant_id="acc_test_merchant",
            amount_minor=50000,
            currency="INR",
            capability_id="payment_link_recovery",
            action_type="create_payment_link",
            signal_id="sig_test_xyz789",
        )

        result = cap.execute(context)

        assert result.status == ExecutionStatus.FAILED
        assert "Connection lost" in result.error_message

    def test_validation_rejects_zero_amount(self):
        mock_client = _make_mock_razorpay_client(success=True)
        cap = PaymentLinkRecoveryCapability(mock_client)

        context = ExecutionContext(
            case_id="case_test_abc123",
            decision_id="dec_test_001",
            merchant_id="acc_test_merchant",
            amount_minor=0,
            currency="INR",
            capability_id="payment_link_recovery",
            action_type="create_payment_link",
            signal_id="sig_test_xyz789",
        )

        result = cap.execute(context)
        assert result.status == ExecutionStatus.FAILED
        # Razorpay API should NOT have been called.
        mock_client.create_payment_link.assert_not_called()


# ===========================================================================
# F. Execution result does NOT claim money recovered
# ===========================================================================


class TestExecutionResultSemantics:
    def test_executed_status_is_not_recovery_claim(self):
        """A successful execution result must NOT claim money was recovered."""
        result = ExecutionResult(
            case_id="case_test_abc123",
            decision_id="dec_test_001",
            capability_id="payment_link_recovery",
            action_type="create_payment_link",
            status=ExecutionStatus.EXECUTED,
            provider="razorpay",
            provider_reference="plink_test_abc123",
        )

        # The model should NOT have an 'amount_recovered' field.
        assert not hasattr(result, "amount_recovered")
        assert not hasattr(result, "recovered")

        # Status means "action executed", not "money recovered".
        assert result.status == ExecutionStatus.EXECUTED
        assert result.status.value == "executed"


# ===========================================================================
# G. AgentDecision routes through Policy → Registry → Capability
# ===========================================================================


class TestCapabilityExecutorIntegration:
    def test_full_pipeline_allow_and_execute(self):
        """Valid decision + case → ALLOW → resolve → execute → status=executed."""
        mock_client = _make_mock_razorpay_client(success=True)
        executor = _build_executor(mock_client)

        decision = _make_decision()
        case = _make_case()

        result = executor.execute(decision, case)

        assert result.status == ExecutionStatus.EXECUTED
        assert result.provider_reference == "plink_test_abc123"
        assert result.capability_id == "payment_link_recovery"
        mock_client.create_payment_link.assert_called_once()

    def test_full_pipeline_policy_block(self):
        """Invalid case → policy BLOCK → status=blocked, no API call."""
        mock_client = _make_mock_razorpay_client(success=True)
        executor = _build_executor(mock_client)

        decision = _make_decision()
        case = _make_case(risk_status=RiskStatus.NOT_AT_RISK)

        result = executor.execute(decision, case)

        assert result.status == ExecutionStatus.BLOCKED
        assert "Policy blocked" in result.error_message
        # Razorpay API should NOT have been called.
        mock_client.create_payment_link.assert_not_called()

    def test_unregistered_capability_returns_blocked(self):
        """Decision referencing unregistered capability → policy BLOCK."""
        mock_client = _make_mock_razorpay_client(success=True)
        executor = _build_executor(mock_client)

        decision = _make_decision(capability_id="totally_unknown_cap")
        case = _make_case()

        result = executor.execute(decision, case)

        assert result.status == ExecutionStatus.BLOCKED
        mock_client.create_payment_link.assert_not_called()

    def test_api_failure_through_executor(self):
        """API failure → executor returns status=failed."""
        mock_client = _make_mock_razorpay_client(success=False)
        executor = _build_executor(mock_client)

        decision = _make_decision()
        case = _make_case()

        result = executor.execute(decision, case)

        assert result.status == ExecutionStatus.FAILED
        assert result.error_message is not None


# ===========================================================================
# H. Webhook remains independent of execution latency
# ===========================================================================


class TestWebhookIndependence:
    """Verify that the webhook endpoint returns HTTP 200 immediately.

    The capability execution runs in a background task, so a slow
    Razorpay API must never block the webhook response.
    """

    def test_webhook_returns_200_immediately(self, client):
        """The webhook must return quickly even when the pipeline runs."""
        import hashlib
        import hmac
        import json

        payload = {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_test_exec_001",
                        "amount": 50000,
                        "currency": "INR",
                        "status": "failed",
                        "method": "card",
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "Card declined",
                        "error_source": "customer",
                        "error_step": "payment_authorization",
                        "created_at": 1693000000,
                    }
                }
            },
            "account_id": "acc_test_webhook_exec",
        }

        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        secret = "test_webhook_secret"
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        # Patch the executor so we don't make real API calls.
        with patch(
            "app.signals.service._executor"
        ) as mock_executor:
            mock_executor.execute.return_value = ExecutionResult(
                case_id="case_test",
                decision_id="dec_test",
                capability_id="payment_link_recovery",
                action_type="create_payment_link",
                status=ExecutionStatus.EXECUTED,
                provider_reference="plink_mock",
            )

            response = client.post(
                "/webhooks/razorpay",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": sig,
                },
            )

        # Webhook must return 200 regardless of execution.
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "accepted"
