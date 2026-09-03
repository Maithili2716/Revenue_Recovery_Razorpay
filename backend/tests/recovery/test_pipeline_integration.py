"""Integration tests: Execution → Verification → Learning → Audit.

Tests:
1. execution → verification → learning (full pipeline)
2. recovered execution creates correct audit events
3. unknown verification does NOT update learning
4. final pipeline result contains execution + verification fields
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.recovery.audit.models import AuditEventType
from app.recovery.audit.service import AuditService
from app.recovery.audit.store import AuditStore
from app.recovery.capabilities.models import ExecutionResult, ExecutionStatus
from app.recovery.learning.service import LearningService, build_context_key
from app.recovery.learning.store import StrategyStore
from app.recovery.pipeline import RecoveryPipelineResult
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
    provider_reference: str = "plink_test_int_001",
    payment_link_url: str = "https://rzp.io/i/test_int",
) -> ExecutionResult:
    return ExecutionResult(
        case_id="case_int_001",
        decision_id="dec_int_001",
        capability_id="payment_link_recovery",
        action_type="create_payment_link",
        status=status,
        provider="razorpay",
        provider_reference=provider_reference,
        payment_link_url=payment_link_url,
    )


def _make_mock_provider(
    response: PaymentLinkVerificationResponse,
) -> VerificationProvider:
    provider = MagicMock(spec=VerificationProvider)
    provider.fetch_payment_link.return_value = response
    return provider


# ===========================================================================
# 1. Execution → Verification → Learning (full flow)
# ===========================================================================


class TestFullPipelineFlow:
    def test_recovered_flow(self):
        """Paid payment link → RECOVERED → learning updates."""
        # Setup
        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_test_int_001",
            status="paid",
            amount=10000,
            amount_paid=10000,
            currency="INR",
            payments=[{"payment_id": "pay_int_001"}],
        )
        provider = _make_mock_provider(response)
        verification_service = VerificationService(provider=provider)

        learning_store = StrategyStore()
        learning_service = LearningService(store=learning_store)

        exec_result = _make_execution_result()

        # Step 1: Verify
        outcome = verification_service.verify(
            execution_result=exec_result,
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.RECOVERED
        assert outcome.amount_recovered_minor == 10000

        # Step 2: Learn
        context_key = build_context_key("payment_failure", "bank", "medium")
        updated = learning_service.record_outcome(
            merchant_id="merchant_int",
            capability_id="payment_link_recovery",
            context_key=context_key,
            verified_outcome=outcome,
        )

        assert updated is True
        stats = learning_service.get_statistics(
            "merchant_int", "payment_link_recovery", context_key
        )
        assert stats.successes == 2  # 1 prior + 1

    def test_created_returns_pending_no_learning(self):
        """Created payment link → PENDING → learning NOT updated."""
        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_test_int_002",
            status="created",
            amount=10000,
            amount_paid=0,
            currency="INR",
        )
        provider = _make_mock_provider(response)
        verification_service = VerificationService(provider=provider)

        learning_store = StrategyStore()
        learning_service = LearningService(store=learning_store)

        exec_result = _make_execution_result(
            provider_reference="plink_test_int_002",
        )

        outcome = verification_service.verify(
            execution_result=exec_result,
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.PENDING

        context_key = build_context_key()
        updated = learning_service.record_outcome(
            merchant_id="merchant_int",
            capability_id="payment_link_recovery",
            context_key=context_key,
            verified_outcome=outcome,
        )

        assert updated is False
        stats = learning_service.get_statistics(
            "merchant_int", "payment_link_recovery", context_key
        )
        assert stats.successes == 1  # Only prior
        assert stats.failures == 1  # Only prior

    def test_expired_returns_not_recovered_with_learning(self):
        """Expired payment link → NOT_RECOVERED → learning records failure."""
        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_test_int_003",
            status="expired",
            amount=10000,
            amount_paid=0,
            currency="INR",
        )
        provider = _make_mock_provider(response)
        verification_service = VerificationService(provider=provider)

        learning_store = StrategyStore()
        learning_service = LearningService(store=learning_store)

        exec_result = _make_execution_result(
            provider_reference="plink_test_int_003",
        )

        outcome = verification_service.verify(
            execution_result=exec_result,
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.NOT_RECOVERED

        context_key = build_context_key()
        updated = learning_service.record_outcome(
            merchant_id="merchant_int",
            capability_id="payment_link_recovery",
            context_key=context_key,
            verified_outcome=outcome,
        )

        assert updated is True
        stats = learning_service.get_statistics(
            "merchant_int", "payment_link_recovery", context_key
        )
        assert stats.failures == 2  # 1 prior + 1


# ===========================================================================
# 2. Recovered execution creates correct audit events
# ===========================================================================


class TestAuditEventCreation:
    def test_recovered_audit_trail(self):
        """Simulate recovered flow and record audit events."""
        audit_store = AuditStore()
        audit_service = AuditService(store=audit_store)

        case_id = "case_audit_001"
        merchant_id = "merchant_audit"

        # Record lifecycle events.
        audit_service.record(
            event_type=AuditEventType.CASE_CREATED,
            case_id=case_id,
            merchant_id=merchant_id,
            actor="risk_detector",
        )
        audit_service.record(
            event_type=AuditEventType.DECISION_CREATED,
            case_id=case_id,
            merchant_id=merchant_id,
            actor="agent",
            decision_id="dec_001",
        )
        audit_service.record(
            event_type=AuditEventType.POLICY_DECISION,
            case_id=case_id,
            merchant_id=merchant_id,
            actor="policy_engine",
        )
        audit_service.record(
            event_type=AuditEventType.CAPABILITY_EXECUTED,
            case_id=case_id,
            merchant_id=merchant_id,
            actor="payment_link_recovery",
            execution_id="exec_001",
        )
        audit_service.record(
            event_type=AuditEventType.VERIFICATION_COMPLETED,
            case_id=case_id,
            merchant_id=merchant_id,
            actor="verification_service",
            data={"verification_status": "recovered", "amount_recovered_minor": 10000},
        )
        audit_service.record(
            event_type=AuditEventType.LEARNING_UPDATED,
            case_id=case_id,
            merchant_id=merchant_id,
            actor="learning_service",
            data={"successes": 2, "failures": 1},
        )

        events = audit_service.get_case_audit(case_id)
        event_types = [e.event_type for e in events]

        assert AuditEventType.CASE_CREATED in event_types
        assert AuditEventType.DECISION_CREATED in event_types
        assert AuditEventType.POLICY_DECISION in event_types
        assert AuditEventType.CAPABILITY_EXECUTED in event_types
        assert AuditEventType.VERIFICATION_COMPLETED in event_types
        assert AuditEventType.LEARNING_UPDATED in event_types
        assert len(events) == 6


# ===========================================================================
# 3. Unknown verification does NOT update learning
# ===========================================================================


class TestUnknownNoLearning:
    def test_unknown_verification_skips_learning(self):
        response = PaymentLinkVerificationResponse(
            success=False,
            error_message="Timeout",
        )
        provider = _make_mock_provider(response)
        verification_service = VerificationService(provider=provider)

        learning_store = StrategyStore()
        learning_service = LearningService(store=learning_store)

        exec_result = _make_execution_result()

        outcome = verification_service.verify(
            execution_result=exec_result,
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.UNKNOWN

        context_key = build_context_key()
        updated = learning_service.record_outcome(
            merchant_id="merchant_unknown",
            capability_id="payment_link_recovery",
            context_key=context_key,
            verified_outcome=outcome,
        )

        assert updated is False
        stats = learning_service.get_statistics(
            "merchant_unknown", "payment_link_recovery", context_key
        )
        assert stats.successes == 1  # Only prior
        assert stats.failures == 1  # Only prior


# ===========================================================================
# 4. Pipeline result contains execution + verification fields
# ===========================================================================


class TestPipelineResult:
    def test_pipeline_result_fields(self):
        result = RecoveryPipelineResult(
            case_id="case_pr_001",
            decision_id="dec_pr_001",
            execution_id="exec_pr_001",
            capability_id="payment_link_recovery",
            execution_status=ExecutionStatus.EXECUTED,
            verification_status=VerificationStatus.RECOVERED,
            amount_at_risk_minor=10000,
            amount_recovered_minor=10000,
            currency="INR",
            provider_reference="plink_pr_001",
            payment_link_url="https://rzp.io/i/pr",
            verification_reason="Payment link paid",
            learning_updated=True,
        )

        assert result.execution_status == ExecutionStatus.EXECUTED
        assert result.verification_status == VerificationStatus.RECOVERED
        assert result.amount_recovered_minor == 10000
        assert result.provider_reference == "plink_pr_001"
        assert result.payment_link_url == "https://rzp.io/i/pr"
        assert result.learning_updated is True

    def test_pipeline_result_unknown_no_learning(self):
        result = RecoveryPipelineResult(
            case_id="case_pr_002",
            decision_id="dec_pr_002",
            execution_id="exec_pr_002",
            capability_id="payment_link_recovery",
            execution_status=ExecutionStatus.EXECUTED,
            verification_status=VerificationStatus.UNKNOWN,
            amount_at_risk_minor=10000,
            amount_recovered_minor=0,
            currency="INR",
            verification_reason="API timeout",
            learning_updated=False,
        )

        assert result.verification_status == VerificationStatus.UNKNOWN
        assert result.amount_recovered_minor == 0
        assert result.learning_updated is False
