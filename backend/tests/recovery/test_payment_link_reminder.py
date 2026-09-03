"""Focused tests for the Payment Link Reminder capability.

Tests:
1.  Capability is registered in the registry
2.  Valid payment_link_id + email → Razorpay notification API called
3.  Valid payment_link_id + sms → Razorpay notification API called
4.  Successful Razorpay response → EXECUTED
5.  Razorpay/API error → FAILED
6.  Missing payment_link_id → appropriate failure
7.  No existing pending payment link → candidate is ineligible
8.  Reminder execution does NOT mark recovery as successful
9.  Reminder remains compatible with later payment_link.paid verification
10. Audit event is created
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.integrations.razorpay.client import NotifyResponse
from app.policy.engine import PolicyEngine
from app.recovery.agent.candidates import (
    generate_candidates,
    generate_candidates_with_context,
)
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
)
from app.recovery.audit.models import AuditEvent, AuditEventType
from app.recovery.audit.service import AuditService
from app.recovery.audit.store import AuditStore
from app.recovery.capabilities.executor import CapabilityExecutor
from app.recovery.capabilities.models import (
    ExecutionContext,
    ExecutionResult,
    ExecutionStatus,
)
from app.recovery.capabilities.payment_link import PaymentLinkRecoveryCapability
from app.recovery.capabilities.payment_link_reminder import (
    PaymentLinkReminderCapability,
)
from app.recovery.capabilities.registry import CapabilityRegistry
from app.recovery.models import (
    RecoveryCase,
    Recoverability,
    RiskStatus,
    Urgency,
)
from app.recovery.pending_store import PendingRecovery, PendingRecoveryStore


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------


def _make_mock_razorpay_client(*, notify_success: bool = True) -> MagicMock:
    """Create a mock Razorpay client with notify_by configured."""
    client = MagicMock()
    if notify_success:
        client.notify_by.return_value = NotifyResponse(
            success=True,
            http_status_code=200,
        )
    else:
        client.notify_by.return_value = NotifyResponse(
            success=False,
            error_message="Bad Request: payment link expired",
            http_status_code=400,
        )
    # Also provide a create_payment_link mock for the registry tests.
    from app.integrations.razorpay.client import PaymentLinkResponse

    client.create_payment_link.return_value = PaymentLinkResponse(
        success=True,
        payment_link_id="plink_test_abc123",
        short_url="https://rzp.io/i/test123",
        status="created",
        raw_response={"id": "plink_test_abc123"},
        http_status_code=200,
    )
    return client


def _make_reminder_context(
    *,
    payment_link_id: str | None = "plink_test_existing_001",
    medium: str = "email",
    case_id: str = "case_reminder_test_001",
    merchant_id: str = "acc_test_merchant",
) -> ExecutionContext:
    """Build an ExecutionContext for reminder tests."""
    decision_context = {}
    if payment_link_id is not None:
        decision_context["payment_link_id"] = payment_link_id
    if medium:
        decision_context["medium"] = medium

    return ExecutionContext(
        case_id=case_id,
        decision_id="dec_reminder_test_001",
        merchant_id=merchant_id,
        customer_id="cust_test_001",
        amount_minor=50000,
        currency="INR",
        capability_id="payment_link_reminder",
        action_type="send_payment_link_reminder",
        signal_id="sig_test_reminder_001",
        decision_context=decision_context,
    )


def _make_case(
    *,
    risk_status: RiskStatus = RiskStatus.AT_RISK,
    amount: int = 50000,
    currency: str = "INR",
    merchant_id: str = "acc_test_merchant",
) -> RecoveryCase:
    return RecoveryCase(
        case_id="case_reminder_test_001",
        signal_id="sig_test_reminder_001",
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
    capability_id: str = "payment_link_reminder",
    case_id: str = "case_reminder_test_001",
    payment_link_id: str = "plink_test_existing_001",
    medium: str = "email",
) -> AgentDecision:
    return AgentDecision(
        decision_id="dec_reminder_test_001",
        case_id=case_id,
        selected_capability_id=capability_id,
        selected_action_type=ActionType.SEND_PAYMENT_LINK_REMINDER,
        reason="Reminder for existing payment link",
        candidate_action_ids=[capability_id],
        decision_context={
            "payment_link_id": payment_link_id,
            "medium": medium,
        },
        decision_source=DecisionSource.CONTEXTUAL_BANDIT,
    )


def _make_agent_context(case_id: str = "case_reminder_test_001") -> AgentContext:
    return AgentContext(
        case_id=case_id,
        signal_id="sig_test_reminder_001",
        merchant_id="acc_test_merchant",
        customer_id="cust_test_001",
        amount_at_risk_minor=50000,
        currency="INR",
        signal_type="payment_failure",
        failure_reason="card_declined",
        failure_source="customer",
        failure_step="payment_authorization",
        payment_method="card",
        signal_occurred_at=datetime.now(timezone.utc),
        recoverability="likely",
        urgency="medium",
        reason_codes=["payment_failed"],
    )


def _make_diagnosis() -> Diagnosis:
    return Diagnosis(
        category=DiagnosisCategory.PAYMENT_FAILURE,
        primary_reason="card_declined",
        failure_stage=FailureStage.PAYMENT_AUTHORIZATION,
        confidence=1.0,
        reason_codes=["payment_failed"],
    )


def _build_executor_with_reminder(
    mock_client: MagicMock | None = None,
) -> CapabilityExecutor:
    """Build a fully wired executor with both payment_link_recovery and payment_link_reminder."""
    client = mock_client or _make_mock_razorpay_client()
    registry = CapabilityRegistry()
    registry.register(PaymentLinkRecoveryCapability(client))
    registry.register(PaymentLinkReminderCapability(client))
    policy = PolicyEngine(registered_capability_ids=registry.registered_ids)
    return CapabilityExecutor(registry=registry, policy_engine=policy)


# ===========================================================================
# 1. Capability is registered
# ===========================================================================


class TestReminderCapabilityRegistration:
    def test_reminder_capability_is_registered(self):
        client = _make_mock_razorpay_client()
        registry = CapabilityRegistry()
        cap = PaymentLinkReminderCapability(client)
        registry.register(cap)

        resolved = registry.get("payment_link_reminder")
        assert resolved is not None
        assert resolved.capability_id == "payment_link_reminder"
        assert resolved.action_type == "send_payment_link_reminder"

    def test_reminder_registered_alongside_recovery(self):
        """Both capabilities can coexist in the same registry."""
        client = _make_mock_razorpay_client()
        registry = CapabilityRegistry()
        registry.register(PaymentLinkRecoveryCapability(client))
        registry.register(PaymentLinkReminderCapability(client))

        assert registry.get("payment_link_recovery") is not None
        assert registry.get("payment_link_reminder") is not None
        assert "payment_link_reminder" in registry.registered_ids
        assert "payment_link_recovery" in registry.registered_ids


# ===========================================================================
# 2. Valid payment_link_id + email → Razorpay notify API called
# ===========================================================================


class TestReminderEmail:
    def test_email_notification_calls_razorpay(self):
        mock_client = _make_mock_razorpay_client(notify_success=True)
        cap = PaymentLinkReminderCapability(mock_client)
        context = _make_reminder_context(medium="email")

        result = cap.execute(context)

        mock_client.notify_by.assert_called_once_with(
            payment_link_id="plink_test_existing_001",
            medium="email",
        )
        assert result.status == ExecutionStatus.EXECUTED


# ===========================================================================
# 3. Valid payment_link_id + sms → Razorpay notify API called
# ===========================================================================


class TestReminderSms:
    def test_sms_notification_calls_razorpay(self):
        mock_client = _make_mock_razorpay_client(notify_success=True)
        cap = PaymentLinkReminderCapability(mock_client)
        context = _make_reminder_context(medium="sms")

        result = cap.execute(context)

        mock_client.notify_by.assert_called_once_with(
            payment_link_id="plink_test_existing_001",
            medium="sms",
        )
        assert result.status == ExecutionStatus.EXECUTED


# ===========================================================================
# 4. Successful Razorpay response → EXECUTED
# ===========================================================================


class TestReminderSuccessExecution:
    def test_successful_razorpay_response_returns_executed(self):
        mock_client = _make_mock_razorpay_client(notify_success=True)
        cap = PaymentLinkReminderCapability(mock_client)
        context = _make_reminder_context()

        result = cap.execute(context)

        assert result.status == ExecutionStatus.EXECUTED
        assert result.provider == "razorpay"
        assert result.provider_reference == "plink_test_existing_001"
        assert result.case_id == "case_reminder_test_001"
        assert result.capability_id == "payment_link_reminder"
        assert result.execution_id.startswith("exec_")
        assert result.metadata["payment_link_id"] == "plink_test_existing_001"
        assert result.metadata["medium"] == "email"
        assert result.metadata["notification_accepted"] is True
        assert result.error_message is None


# ===========================================================================
# 5. Razorpay/API error → FAILED
# ===========================================================================


class TestReminderApiFailure:
    def test_razorpay_api_error_returns_failed(self):
        mock_client = _make_mock_razorpay_client(notify_success=False)
        cap = PaymentLinkReminderCapability(mock_client)
        context = _make_reminder_context()

        result = cap.execute(context)

        assert result.status == ExecutionStatus.FAILED
        assert result.error_message is not None
        assert "expired" in result.error_message.lower() or result.error_message

    def test_exception_during_api_call_returns_failed(self):
        mock_client = MagicMock()
        mock_client.notify_by.side_effect = RuntimeError("Connection lost")
        cap = PaymentLinkReminderCapability(mock_client)
        context = _make_reminder_context()

        result = cap.execute(context)

        assert result.status == ExecutionStatus.FAILED
        assert "Connection lost" in result.error_message


# ===========================================================================
# 6. Missing payment_link_id → appropriate failure/block
# ===========================================================================


class TestReminderMissingPaymentLinkId:
    def test_missing_payment_link_id_returns_failed(self):
        mock_client = _make_mock_razorpay_client(notify_success=True)
        cap = PaymentLinkReminderCapability(mock_client)

        # Create context without payment_link_id
        context = ExecutionContext(
            case_id="case_reminder_test_001",
            decision_id="dec_reminder_test_001",
            merchant_id="acc_test_merchant",
            amount_minor=50000,
            currency="INR",
            capability_id="payment_link_reminder",
            action_type="send_payment_link_reminder",
            signal_id="sig_test_001",
            decision_context={},  # No payment_link_id
        )

        result = cap.execute(context)

        assert result.status == ExecutionStatus.FAILED
        assert "payment_link_id" in result.error_message.lower()
        # Razorpay API should NOT have been called.
        mock_client.notify_by.assert_not_called()

    def test_empty_payment_link_id_returns_failed(self):
        mock_client = _make_mock_razorpay_client(notify_success=True)
        cap = PaymentLinkReminderCapability(mock_client)

        context = ExecutionContext(
            case_id="case_reminder_test_001",
            decision_id="dec_reminder_test_001",
            merchant_id="acc_test_merchant",
            amount_minor=50000,
            currency="INR",
            capability_id="payment_link_reminder",
            action_type="send_payment_link_reminder",
            signal_id="sig_test_001",
            decision_context={"payment_link_id": ""},
        )

        result = cap.execute(context)

        assert result.status == ExecutionStatus.FAILED
        mock_client.notify_by.assert_not_called()

    def test_unsupported_medium_returns_failed(self):
        mock_client = _make_mock_razorpay_client(notify_success=True)
        cap = PaymentLinkReminderCapability(mock_client)
        context = _make_reminder_context(medium="whatsapp")

        result = cap.execute(context)

        assert result.status == ExecutionStatus.FAILED
        assert "medium" in result.error_message.lower()
        mock_client.notify_by.assert_not_called()


# ===========================================================================
# 7. No existing pending payment link → candidate is ineligible
# ===========================================================================


class TestReminderCandidateEligibility:
    def test_no_pending_payment_link_means_no_reminder_candidate(self):
        """When no pending payment link exists, reminder must NOT be generated."""
        context = _make_agent_context()
        diagnosis = _make_diagnosis()

        # Without pending_payment_link_id → no reminder candidate.
        candidates = generate_candidates_with_context(
            context, diagnosis, pending_payment_link_id=None
        )

        reminder_candidates = [
            c for c in candidates if c.capability_id == "payment_link_reminder"
        ]
        assert len(reminder_candidates) == 0

    def test_with_pending_payment_link_generates_reminder_candidate(self):
        """When a pending payment link exists, reminder should be generated."""
        context = _make_agent_context()
        diagnosis = _make_diagnosis()

        candidates = generate_candidates_with_context(
            context, diagnosis, pending_payment_link_id="plink_test_001"
        )

        reminder_candidates = [
            c for c in candidates if c.capability_id == "payment_link_reminder"
        ]
        assert len(reminder_candidates) == 1
        assert reminder_candidates[0].action_type == ActionType.SEND_PAYMENT_LINK_REMINDER
        assert reminder_candidates[0].eligibility == EligibilityStatus.ELIGIBLE

    def test_brand_new_failure_has_no_reminder_candidate(self):
        """Brand-new payment failure with no existing payment link: no reminder."""
        context = _make_agent_context()
        diagnosis = _make_diagnosis()

        # Standard generator (no pending context) → only payment_link_recovery.
        candidates = generate_candidates(context, diagnosis)

        capability_ids = [c.capability_id for c in candidates]
        assert "payment_link_recovery" in capability_ids
        assert "payment_link_reminder" not in capability_ids

    def test_both_candidates_when_pending_link_exists(self):
        """When pending link exists, both recovery and reminder are candidates."""
        context = _make_agent_context()
        diagnosis = _make_diagnosis()

        candidates = generate_candidates_with_context(
            context, diagnosis, pending_payment_link_id="plink_test_001"
        )

        capability_ids = [c.capability_id for c in candidates]
        assert "payment_link_recovery" in capability_ids
        assert "payment_link_reminder" in capability_ids


# ===========================================================================
# 8. Reminder execution does NOT mark recovery as successful
# ===========================================================================


class TestReminderDoesNotClaimRecovery:
    def test_executed_status_does_not_claim_recovery(self):
        """A successful reminder execution must NOT claim money was recovered."""
        mock_client = _make_mock_razorpay_client(notify_success=True)
        cap = PaymentLinkReminderCapability(mock_client)
        context = _make_reminder_context()

        result = cap.execute(context)

        # The model should NOT have an 'amount_recovered' field.
        assert not hasattr(result, "amount_recovered")
        assert not hasattr(result, "recovered")

        # Status means "notification sent", NOT "money recovered".
        assert result.status == ExecutionStatus.EXECUTED
        assert result.status.value == "executed"

    def test_reminder_through_executor_does_not_claim_recovery(self):
        """Full executor pipeline for reminder — no recovery claim."""
        mock_client = _make_mock_razorpay_client(notify_success=True)
        executor = _build_executor_with_reminder(mock_client)

        decision = _make_decision()
        case = _make_case()

        result = executor.execute(decision, case)

        assert result.status == ExecutionStatus.EXECUTED
        assert result.capability_id == "payment_link_reminder"
        assert not hasattr(result, "amount_recovered")


# ===========================================================================
# 9. Reminder remains compatible with later payment_link.paid verification
# ===========================================================================


class TestReminderVerificationCompatibility:
    def test_pending_store_remains_intact_after_reminder(self):
        """A reminder execution should NOT resolve/modify the pending store entry.

        The existing Payment Link verification path (payment_link.paid webhook)
        remains responsible for determining whether the associated Payment Link
        eventually becomes paid.
        """
        store = PendingRecoveryStore()

        # Simulate an existing pending recovery entry.
        pending = PendingRecovery(
            payment_link_id="plink_test_existing_001",
            case_id="case_reminder_test_001",
            execution_id="exec_original_001",
            decision_id="dec_original_001",
            merchant_id="acc_test_merchant",
            capability_id="payment_link_recovery",
            signal_id="sig_test_001",
            amount_at_risk_minor=50000,
            currency="INR",
        )
        store.store(pending)

        # Execute a reminder — the reminder capability itself doesn't touch the store.
        mock_client = _make_mock_razorpay_client(notify_success=True)
        cap = PaymentLinkReminderCapability(mock_client)
        context = _make_reminder_context()
        result = cap.execute(context)
        assert result.status == ExecutionStatus.EXECUTED

        # Verify the pending store entry is unchanged.
        entry = store.get_by_payment_link_id("plink_test_existing_001")
        assert entry is not None
        assert entry.resolved is False
        assert entry.resolution_status is None

    def test_pending_store_can_still_be_resolved_after_reminder(self):
        """After a reminder, the payment_link.paid webhook can still resolve the entry."""
        store = PendingRecoveryStore()

        pending = PendingRecovery(
            payment_link_id="plink_test_existing_001",
            case_id="case_reminder_test_001",
            execution_id="exec_original_001",
            decision_id="dec_original_001",
            merchant_id="acc_test_merchant",
            capability_id="payment_link_recovery",
            signal_id="sig_test_001",
            amount_at_risk_minor=50000,
            currency="INR",
        )
        store.store(pending)

        # Simulate reminder execution (separate from store).
        mock_client = _make_mock_razorpay_client(notify_success=True)
        cap = PaymentLinkReminderCapability(mock_client)
        context = _make_reminder_context()
        cap.execute(context)

        # Now simulate the payment_link.paid webhook arriving later.
        resolved = store.mark_resolved(
            "plink_test_existing_001",
            status="recovered",
            source="webhook",
        )
        assert resolved is True

        entry = store.get_by_payment_link_id("plink_test_existing_001")
        assert entry.resolved is True
        assert entry.resolution_status == "recovered"


# ===========================================================================
# 10. Audit event is created
# ===========================================================================


class TestReminderAuditEvent:
    def test_audit_event_recorded_for_reminder(self):
        """An audit event should be created when a reminder is executed."""
        audit_store = AuditStore()
        audit_service = AuditService(audit_store)

        # Execute reminder.
        mock_client = _make_mock_razorpay_client(notify_success=True)
        cap = PaymentLinkReminderCapability(mock_client)
        context = _make_reminder_context()
        result = cap.execute(context)

        # Record audit event (as the pipeline would do).
        audit_event = audit_service.record(
            event_type=AuditEventType.REMINDER_SENT,
            case_id=context.case_id,
            merchant_id=context.merchant_id,
            actor="payment_link_reminder_capability",
            execution_id=result.execution_id,
            data={
                "capability_id": result.capability_id,
                "payment_link_id": context.decision_context.get("payment_link_id"),
                "medium": context.decision_context.get("medium"),
                "status": result.status.value,
            },
        )

        assert audit_event is not None
        assert audit_event.event_type == AuditEventType.REMINDER_SENT
        assert audit_event.case_id == "case_reminder_test_001"
        assert audit_event.merchant_id == "acc_test_merchant"
        assert audit_event.execution_id == result.execution_id

        # Verify the audit store has the event.
        case_events = audit_service.get_case_audit("case_reminder_test_001")
        reminder_events = [
            e for e in case_events if e.event_type == AuditEventType.REMINDER_SENT
        ]
        assert len(reminder_events) == 1
        assert reminder_events[0].data["payment_link_id"] == "plink_test_existing_001"
        assert reminder_events[0].data["medium"] == "email"
        assert reminder_events[0].data["status"] == "executed"

    def test_audit_event_for_failed_reminder(self):
        """An audit event should also be created when a reminder fails."""
        audit_store = AuditStore()
        audit_service = AuditService(audit_store)

        mock_client = _make_mock_razorpay_client(notify_success=False)
        cap = PaymentLinkReminderCapability(mock_client)
        context = _make_reminder_context()
        result = cap.execute(context)

        audit_event = audit_service.record(
            event_type=AuditEventType.REMINDER_SENT,
            case_id=context.case_id,
            merchant_id=context.merchant_id,
            actor="payment_link_reminder_capability",
            execution_id=result.execution_id,
            data={
                "capability_id": result.capability_id,
                "status": result.status.value,
                "error_message": result.error_message,
            },
        )

        assert audit_event is not None
        assert audit_event.data["status"] == "failed"
        assert audit_event.data["error_message"] is not None


# ===========================================================================
# Additional edge case tests
# ===========================================================================


class TestReminderFullExecutorPipeline:
    def test_reminder_through_full_executor_pipeline(self):
        """Decision → Policy → Registry → Reminder Capability → EXECUTED."""
        mock_client = _make_mock_razorpay_client(notify_success=True)
        executor = _build_executor_with_reminder(mock_client)

        decision = _make_decision()
        case = _make_case()

        result = executor.execute(decision, case)

        assert result.status == ExecutionStatus.EXECUTED
        assert result.capability_id == "payment_link_reminder"
        assert result.provider == "razorpay"
        mock_client.notify_by.assert_called_once()

    def test_reminder_api_failure_through_executor(self):
        """API failure through executor returns FAILED."""
        mock_client = _make_mock_razorpay_client(notify_success=False)
        executor = _build_executor_with_reminder(mock_client)

        decision = _make_decision()
        case = _make_case()

        result = executor.execute(decision, case)

        assert result.status == ExecutionStatus.FAILED
        assert result.error_message is not None

    def test_reminder_policy_block_for_invalid_case(self):
        """Policy blocks reminder for NOT_AT_RISK case."""
        mock_client = _make_mock_razorpay_client(notify_success=True)
        executor = _build_executor_with_reminder(mock_client)

        decision = _make_decision()
        case = _make_case(risk_status=RiskStatus.NOT_AT_RISK)

        result = executor.execute(decision, case)

        assert result.status == ExecutionStatus.BLOCKED
        mock_client.notify_by.assert_not_called()


# ===========================================================================
# 11. Observability: structured log event verification
# ===========================================================================


class TestReminderObservability:
    """Verify that every required structured log event is emitted."""

    def test_candidate_evaluated_eligible(self, caplog):
        """payment_link_reminder_candidate_evaluated with eligible=True."""
        import logging
        with caplog.at_level(logging.INFO):
            context = _make_agent_context()
            diagnosis = _make_diagnosis()
            generate_candidates_with_context(
                context, diagnosis, pending_payment_link_id="plink_test_001"
            )

        msgs = [r for r in caplog.records if r.message == "payment_link_reminder_candidate_evaluated"]
        assert len(msgs) >= 1
        eligible_record = [r for r in msgs if getattr(r, "eligible", None) is True]
        assert len(eligible_record) == 1
        r = eligible_record[0]
        assert r.case_id == context.case_id
        assert r.payment_link_id == "plink_test_001"
        assert r.reason == "existing_pending_payment_link"

    def test_candidate_evaluated_ineligible_no_pending(self, caplog):
        """payment_link_reminder_candidate_evaluated with eligible=False."""
        import logging
        with caplog.at_level(logging.INFO):
            context = _make_agent_context()
            diagnosis = _make_diagnosis()
            generate_candidates_with_context(
                context, diagnosis, pending_payment_link_id=None
            )

        msgs = [r for r in caplog.records if r.message == "payment_link_reminder_candidate_evaluated"]
        assert len(msgs) >= 1
        ineligible_record = [r for r in msgs if getattr(r, "eligible", None) is False]
        assert len(ineligible_record) == 1
        r = ineligible_record[0]
        assert r.reason == "no_existing_pending_payment_link"

    def test_capability_execution_started_log(self, caplog):
        """capability_execution_started is emitted with required fields."""
        import logging
        with caplog.at_level(logging.INFO):
            mock_client = _make_mock_razorpay_client(notify_success=True)
            cap = PaymentLinkReminderCapability(mock_client)
            context = _make_reminder_context()
            cap.execute(context)

        msgs = [r for r in caplog.records if r.message == "capability_execution_started"]
        assert len(msgs) == 1
        r = msgs[0]
        assert r.capability_id == "payment_link_reminder"
        assert r.payment_link_id == "plink_test_existing_001"
        assert r.medium == "email"
        assert r.merchant_id == "acc_test_merchant"
        assert r.case_id == "case_reminder_test_001"

    def test_razorpay_notification_requested_log(self, caplog):
        """razorpay_payment_link_notification_requested is emitted."""
        import logging
        with caplog.at_level(logging.INFO):
            mock_client = _make_mock_razorpay_client(notify_success=True)
            cap = PaymentLinkReminderCapability(mock_client)
            context = _make_reminder_context(medium="sms")
            cap.execute(context)

        msgs = [r for r in caplog.records if r.message == "razorpay_payment_link_notification_requested"]
        assert len(msgs) == 1
        r = msgs[0]
        assert r.payment_link_id == "plink_test_existing_001"
        assert r.medium == "sms"

    def test_capability_execution_completed_success_log(self, caplog):
        """capability_execution_completed status='executed' provider_success=True."""
        import logging
        with caplog.at_level(logging.INFO):
            mock_client = _make_mock_razorpay_client(notify_success=True)
            cap = PaymentLinkReminderCapability(mock_client)
            context = _make_reminder_context()
            cap.execute(context)

        msgs = [r for r in caplog.records if r.message == "capability_execution_completed"]
        assert len(msgs) == 1
        r = msgs[0]
        assert r.status == "executed"
        assert r.provider_success is True
        assert r.capability_id == "payment_link_reminder"
        assert r.payment_link_id == "plink_test_existing_001"
        assert r.medium == "email"

    def test_capability_execution_completed_failure_log(self, caplog):
        """capability_execution_completed status='failed' provider_success=False on API error."""
        import logging
        with caplog.at_level(logging.WARNING):
            mock_client = _make_mock_razorpay_client(notify_success=False)
            cap = PaymentLinkReminderCapability(mock_client)
            context = _make_reminder_context()
            cap.execute(context)

        msgs = [r for r in caplog.records if r.message == "capability_execution_completed"]
        assert len(msgs) == 1
        r = msgs[0]
        assert r.status == "failed"
        assert r.provider_success is False
        assert r.error_message is not None

    def test_recovery_status_unchanged_log(self, caplog):
        """recovery_status_unchanged is emitted after successful reminder."""
        import logging
        with caplog.at_level(logging.INFO):
            mock_client = _make_mock_razorpay_client(notify_success=True)
            cap = PaymentLinkReminderCapability(mock_client)
            context = _make_reminder_context()
            cap.execute(context)

        msgs = [r for r in caplog.records if r.message == "recovery_status_unchanged"]
        assert len(msgs) == 1
        r = msgs[0]
        assert r.case_id == "case_reminder_test_001"
        assert r.reason == "reminder_sent_waiting_for_payment_link_paid"
        assert r.verification_status == "pending"
        assert r.payment_link_id == "plink_test_existing_001"

    def test_recovery_status_unchanged_not_emitted_on_failure(self, caplog):
        """recovery_status_unchanged is NOT emitted when reminder fails."""
        import logging
        with caplog.at_level(logging.INFO):
            mock_client = _make_mock_razorpay_client(notify_success=False)
            cap = PaymentLinkReminderCapability(mock_client)
            context = _make_reminder_context()
            cap.execute(context)

        msgs = [r for r in caplog.records if r.message == "recovery_status_unchanged"]
        assert len(msgs) == 0, "Should not log recovery_status_unchanged for failed reminder"

    def test_full_executor_log_sequence(self, caplog):
        """Full executor pipeline emits the correct log sequence."""
        import logging
        with caplog.at_level(logging.INFO):
            mock_client = _make_mock_razorpay_client(notify_success=True)
            executor = _build_executor_with_reminder(mock_client)
            decision = _make_decision()
            case = _make_case()
            executor.execute(decision, case)

        # Verify the key log events appear in order.
        log_messages = [r.message for r in caplog.records]

        assert "capability_resolved" in log_messages
        assert "capability_execution_started" in log_messages
        assert "razorpay_payment_link_notification_requested" in log_messages
        assert "capability_execution_completed" in log_messages
        assert "recovery_status_unchanged" in log_messages

        # Verify ordering: resolved before started before completed.
        resolved_idx = log_messages.index("capability_resolved")
        started_idx = log_messages.index("capability_execution_started")
        requested_idx = log_messages.index("razorpay_payment_link_notification_requested")
        completed_idx = log_messages.index("capability_execution_completed")
        unchanged_idx = log_messages.index("recovery_status_unchanged")

        assert resolved_idx < started_idx
        assert started_idx < requested_idx
        assert requested_idx < completed_idx
        assert completed_idx < unchanged_idx

