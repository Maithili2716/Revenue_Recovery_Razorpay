"""Focused tests for the Verification Engine.

Tests:
1. created + amount_paid=0 → PENDING
2. paid + amount_paid>0 → RECOVERED
3. expired → NOT_RECOVERED
4. cancelled → NOT_RECOVERED
5. API failure → UNKNOWN
6. amount_paid cannot exceed amount_at_risk
7. captured payment ID extracted correctly
8. partially_paid with amount_paid>0 → RECOVERED
9. unexpected status → UNKNOWN
10. non-EXECUTED result → NOT_RECOVERED
11. no provider_reference → UNKNOWN
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.recovery.capabilities.models import ExecutionResult, ExecutionStatus
from app.recovery.verification.models import VerificationStatus, VerifiedOutcome
from app.recovery.verification.razorpay import (
    PaymentLinkVerificationResponse,
    VerificationProvider,
)
from app.recovery.verification.service import VerificationService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_execution_result(
    *,
    status: ExecutionStatus = ExecutionStatus.EXECUTED,
    provider_reference: str | None = "plink_test_123",
) -> ExecutionResult:
    return ExecutionResult(
        case_id="case_test_001",
        decision_id="dec_test_001",
        capability_id="payment_link_recovery",
        action_type="create_payment_link",
        status=status,
        provider="razorpay",
        provider_reference=provider_reference,
    )


def _make_mock_provider(
    response: PaymentLinkVerificationResponse,
) -> VerificationProvider:
    provider = MagicMock(spec=VerificationProvider)
    provider.fetch_payment_link.return_value = response
    return provider


# ===========================================================================
# 1. created + amount_paid=0 → PENDING  (THE BUG FIX)
# ===========================================================================


class TestCreatedPaymentLinkPending:
    def test_created_status_returns_pending(self):
        """A newly-created payment link must NOT be treated as failure."""
        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_test_123",
            status="created",
            amount=10000,
            amount_paid=0,
            currency="INR",
        )
        provider = _make_mock_provider(response)
        service = VerificationService(provider=provider)

        outcome = service.verify(
            execution_result=_make_execution_result(),
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.PENDING
        assert outcome.amount_recovered_minor == 0
        assert "awaiting customer" in outcome.reason.lower()


# ===========================================================================
# 2. paid + amount_paid>0 → RECOVERED
# ===========================================================================


class TestPaidPaymentLink:
    def test_paid_status_returns_recovered(self):
        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_test_123",
            status="paid",
            amount=10000,
            amount_paid=10000,
            currency="INR",
            payments=[{"payment_id": "pay_test_abc"}],
        )
        provider = _make_mock_provider(response)
        service = VerificationService(provider=provider)

        outcome = service.verify(
            execution_result=_make_execution_result(),
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.RECOVERED
        assert outcome.amount_recovered_minor == 10000
        assert outcome.provider_payment_id == "pay_test_abc"
        assert outcome.evidence["payment_link_status"] == "paid"


# ===========================================================================
# 3. expired → NOT_RECOVERED
# ===========================================================================


class TestExpiredPaymentLink:
    def test_expired_returns_not_recovered(self):
        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_test_123",
            status="expired",
            amount=10000,
            amount_paid=0,
            currency="INR",
        )
        provider = _make_mock_provider(response)
        service = VerificationService(provider=provider)

        outcome = service.verify(
            execution_result=_make_execution_result(),
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.NOT_RECOVERED
        assert outcome.amount_recovered_minor == 0


# ===========================================================================
# 4. cancelled → NOT_RECOVERED
# ===========================================================================


class TestCancelledPaymentLink:
    def test_cancelled_returns_not_recovered(self):
        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_test_123",
            status="cancelled",
            amount=10000,
            amount_paid=0,
            currency="INR",
        )
        provider = _make_mock_provider(response)
        service = VerificationService(provider=provider)

        outcome = service.verify(
            execution_result=_make_execution_result(),
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.NOT_RECOVERED
        assert outcome.amount_recovered_minor == 0


# ===========================================================================
# 5. API failure → UNKNOWN
# ===========================================================================


class TestApiFailure:
    def test_api_failure_returns_unknown(self):
        response = PaymentLinkVerificationResponse(
            success=False,
            error_message="Razorpay API error",
            http_status_code=500,
        )
        provider = _make_mock_provider(response)
        service = VerificationService(provider=provider)

        outcome = service.verify(
            execution_result=_make_execution_result(),
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.UNKNOWN
        assert outcome.amount_recovered_minor == 0


# ===========================================================================
# 6. amount_paid cannot exceed amount_at_risk
# ===========================================================================


class TestAmountCapping:
    def test_recovered_amount_capped_at_amount_at_risk(self):
        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_test_123",
            status="paid",
            amount=50000,
            amount_paid=50000,
            currency="INR",
            payments=[{"payment_id": "pay_test_big"}],
        )
        provider = _make_mock_provider(response)
        service = VerificationService(provider=provider)

        outcome = service.verify(
            execution_result=_make_execution_result(),
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.RECOVERED
        assert outcome.amount_recovered_minor == 10000  # capped


# ===========================================================================
# 7. Captured payment ID extracted correctly
# ===========================================================================


class TestPaymentIdExtraction:
    def test_payment_id_from_payments_array(self):
        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_test_123",
            status="paid",
            amount=10000,
            amount_paid=10000,
            currency="INR",
            payments=[{"payment_id": "pay_exact_123"}],
        )
        provider = _make_mock_provider(response)
        service = VerificationService(provider=provider)

        outcome = service.verify(
            execution_result=_make_execution_result(),
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.provider_payment_id == "pay_exact_123"

    def test_payment_id_from_nested_entity(self):
        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_test_123",
            status="paid",
            amount=10000,
            amount_paid=10000,
            currency="INR",
            payments=[{"entity": {"id": "pay_nested_456"}}],
        )
        provider = _make_mock_provider(response)
        service = VerificationService(provider=provider)

        outcome = service.verify(
            execution_result=_make_execution_result(),
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.provider_payment_id == "pay_nested_456"


# ===========================================================================
# 8. partially_paid with amount_paid > 0 → RECOVERED
# ===========================================================================


class TestPartiallyPaid:
    def test_partially_paid_returns_recovered(self):
        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_test_123",
            status="partially_paid",
            amount=10000,
            amount_paid=5000,
            currency="INR",
            payments=[{"payment_id": "pay_partial"}],
        )
        provider = _make_mock_provider(response)
        service = VerificationService(provider=provider)

        outcome = service.verify(
            execution_result=_make_execution_result(),
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.RECOVERED
        assert outcome.amount_recovered_minor == 5000


# ===========================================================================
# 9. Unexpected status → UNKNOWN
# ===========================================================================


class TestUnexpectedStatus:
    def test_unknown_status_returns_unknown(self):
        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_test_123",
            status="some_weird_status",
            amount=10000,
            amount_paid=0,
            currency="INR",
        )
        provider = _make_mock_provider(response)
        service = VerificationService(provider=provider)

        outcome = service.verify(
            execution_result=_make_execution_result(),
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.UNKNOWN
        assert outcome.amount_recovered_minor == 0


# ===========================================================================
# 10. PENDING → RECOVERED transition on later check
# ===========================================================================


class TestPendingToRecoveredTransition:
    def test_initial_pending_then_later_recovered(self):
        """Simulates: first check returns 'created', second returns 'paid'."""
        provider = MagicMock(spec=VerificationProvider)

        # First call: created → PENDING
        provider.fetch_payment_link.side_effect = [
            PaymentLinkVerificationResponse(
                success=True,
                payment_link_id="plink_test_123",
                status="created",
                amount=10000,
                amount_paid=0,
                currency="INR",
            ),
            PaymentLinkVerificationResponse(
                success=True,
                payment_link_id="plink_test_123",
                status="paid",
                amount=10000,
                amount_paid=10000,
                currency="INR",
                payments=[{"payment_id": "pay_later"}],
            ),
        ]

        service = VerificationService(provider=provider)
        exec_result = _make_execution_result()

        # First verification → PENDING
        outcome_1 = service.verify(
            execution_result=exec_result,
            amount_at_risk_minor=10000,
            currency="INR",
        )
        assert outcome_1.status == VerificationStatus.PENDING
        assert outcome_1.amount_recovered_minor == 0

        # Second verification → RECOVERED
        outcome_2 = service.verify(
            execution_result=exec_result,
            amount_at_risk_minor=10000,
            currency="INR",
        )
        assert outcome_2.status == VerificationStatus.RECOVERED
        assert outcome_2.amount_recovered_minor == 10000
        assert outcome_2.provider_payment_id == "pay_later"


# ===========================================================================
# Additional edge cases
# ===========================================================================


class TestNonExecutedResult:
    def test_failed_execution_returns_not_recovered(self):
        """A FAILED execution should produce NOT_RECOVERED."""
        provider = _make_mock_provider(
            PaymentLinkVerificationResponse(success=True)
        )
        service = VerificationService(provider=provider)

        outcome = service.verify(
            execution_result=_make_execution_result(
                status=ExecutionStatus.FAILED
            ),
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.NOT_RECOVERED
        provider.fetch_payment_link.assert_not_called()

    def test_no_provider_reference_returns_unknown(self):
        """Executed but no provider_reference → UNKNOWN."""
        provider = _make_mock_provider(
            PaymentLinkVerificationResponse(success=True)
        )
        service = VerificationService(provider=provider)

        outcome = service.verify(
            execution_result=_make_execution_result(provider_reference=None),
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.UNKNOWN
        provider.fetch_payment_link.assert_not_called()
