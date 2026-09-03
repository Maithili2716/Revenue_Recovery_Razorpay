"""Tests for event-driven and manual verification of Payment Link recovery.

Tests:
    1. payment_link.paid webhook recognized
    2. Payment Link ID extracted from webhook payload
    3. Pending recovery correlation works
    4. Webhook triggers independent verification
    5. Independently verified `paid` → RECOVERED
    6. `created` → PENDING
    7. `expired/cancelled` → NOT_RECOVERED
    8. Provider verification error → UNKNOWN
    9. PENDING does not update learning
    10. RECOVERED updates learning exactly once
    11. Duplicate webhook is idempotent
    12. Manual verification endpoint works
    13. Manual verification does not create duplicate learning updates
    14. Webhook endpoint remains fast and does not synchronously wait
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

from unittest.mock import MagicMock, patch

import pytest

from app.recovery.audit.models import AuditEventType
from app.recovery.audit.service import AuditService
from app.recovery.audit.store import AuditStore
from app.recovery.capabilities.models import ExecutionResult, ExecutionStatus
from app.recovery.learning.service import LearningService, build_context_key
from app.recovery.learning.store import StrategyStore
from app.recovery.pending_store import PendingRecovery, PendingRecoveryStore
from app.recovery.verification.models import VerificationStatus, VerifiedOutcome
from app.recovery.verification.razorpay import (
    PaymentLinkVerificationResponse,
    VerificationProvider,
)
from app.recovery.verification.service import VerificationService
from app.signals.router import (
    RECOVERY_EVENT_TYPES,
    is_recovery_event,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pending(
    *,
    payment_link_id: str = "plink_test_001",
    case_id: str = "case_evt_001",
    execution_id: str = "exec_evt_001",
    decision_id: str = "dec_evt_001",
    merchant_id: str = "merchant_evt",
    capability_id: str = "payment_link_recovery",
    signal_id: str = "sig_evt_001",
    amount_at_risk_minor: int = 10000,
    currency: str = "INR",
) -> PendingRecovery:
    return PendingRecovery(
        payment_link_id=payment_link_id,
        case_id=case_id,
        execution_id=execution_id,
        decision_id=decision_id,
        merchant_id=merchant_id,
        capability_id=capability_id,
        signal_id=signal_id,
        amount_at_risk_minor=amount_at_risk_minor,
        currency=currency,
    )


def _make_payment_link_paid_payload(
    payment_link_id: str = "plink_test_001",
    amount: int = 10000,
    amount_paid: int = 10000,
) -> dict:
    """Simulate a Razorpay payment_link.paid webhook payload."""
    return {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": payment_link_id,
                    "amount": amount,
                    "amount_paid": amount_paid,
                    "status": "paid",
                    "currency": "INR",
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_test_001",
                    "amount": amount_paid,
                    "status": "captured",
                }
            },
        },
    }


def _make_mock_provider(
    response: PaymentLinkVerificationResponse,
) -> VerificationProvider:
    provider = MagicMock(spec=VerificationProvider)
    provider.fetch_payment_link.return_value = response
    return provider


# ===========================================================================
# 1. payment_link.paid webhook recognized
# ===========================================================================


class TestWebhookRecognition:
    def test_payment_link_paid_is_recovery_event(self):
        """payment_link.paid should be recognized as a recovery event."""
        assert is_recovery_event("payment_link.paid") is True

    def test_payment_authorized_is_recovery_event(self):
        """payment.authorized should be recognized as a recovery event."""
        assert is_recovery_event("payment.authorized") is True

    def test_payment_captured_is_recovery_event(self):
        """payment.captured should be recognized as a recovery event."""
        assert is_recovery_event("payment.captured") is True

    def test_payment_failed_is_not_recovery_event(self):
        """payment.failed is a signal event, not a recovery event."""
        assert is_recovery_event("payment.failed") is False

    def test_unknown_event_is_not_recovery_event(self):
        """Unknown events are not recovery events."""
        assert is_recovery_event("order.paid") is False

    def test_none_is_not_recovery_event(self):
        assert is_recovery_event(None) is False

    def test_recovery_event_types_set(self):
        """All four required event types are in the recognized set."""
        # payment.failed is recognized but handled by signal normalizer
        assert "payment_link.paid" in RECOVERY_EVENT_TYPES
        assert "payment.authorized" in RECOVERY_EVENT_TYPES
        assert "payment.captured" in RECOVERY_EVENT_TYPES


# ===========================================================================
# 2. Payment Link ID extracted
# ===========================================================================


class TestPaymentLinkIdExtraction:
    def test_extract_from_payment_link_paid(self):
        """Extract plink_id from payment_link.paid payload."""
        from app.signals.service import _extract_payment_link_id

        payload = _make_payment_link_paid_payload("plink_abc123")
        assert _extract_payment_link_id(payload) == "plink_abc123"

    def test_extract_from_payment_with_notes(self):
        """Extract plink_id from payment.authorized with notes."""
        from app.signals.service import _extract_payment_link_id

        payload = {
            "event": "payment.authorized",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_test",
                        "notes": {
                            "payment_link_id": "plink_from_notes",
                        },
                    }
                }
            },
        }
        assert _extract_payment_link_id(payload) == "plink_from_notes"

    def test_extract_returns_none_for_missing_payload(self):
        from app.signals.service import _extract_payment_link_id

        assert _extract_payment_link_id({}) is None

    def test_extract_returns_none_for_no_plink(self):
        from app.signals.service import _extract_payment_link_id

        payload = {
            "event": "payment.authorized",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_test",
                    }
                }
            },
        }
        assert _extract_payment_link_id(payload) is None

    def test_extract_ignores_non_plink_ids(self):
        """IDs not starting with plink_ should be ignored."""
        from app.signals.service import _extract_payment_link_id

        payload = {
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": "inv_something",
                    }
                }
            },
        }
        assert _extract_payment_link_id(payload) is None


# ===========================================================================
# 3. Pending recovery correlation works
# ===========================================================================


class TestPendingRecoveryStore:
    def test_store_and_retrieve_by_payment_link_id(self):
        store = PendingRecoveryStore()
        pending = _make_pending()
        store.store(pending)

        result = store.get_by_payment_link_id("plink_test_001")
        assert result is not None
        assert result.case_id == "case_evt_001"

    def test_retrieve_by_case_id(self):
        store = PendingRecoveryStore()
        pending = _make_pending()
        store.store(pending)

        result = store.get_by_case_id("case_evt_001")
        assert result is not None
        assert result.payment_link_id == "plink_test_001"

    def test_returns_none_for_unknown_plink(self):
        store = PendingRecoveryStore()
        assert store.get_by_payment_link_id("plink_unknown") is None

    def test_returns_none_for_unknown_case(self):
        store = PendingRecoveryStore()
        assert store.get_by_case_id("case_unknown") is None

    def test_mark_resolved(self):
        store = PendingRecoveryStore()
        pending = _make_pending()
        store.store(pending)

        resolved = store.mark_resolved("plink_test_001", "recovered", "webhook")
        assert resolved is True
        assert store.pending_count == 0

    def test_mark_resolved_idempotent(self):
        """Second resolution attempt returns False."""
        store = PendingRecoveryStore()
        pending = _make_pending()
        store.store(pending)

        assert store.mark_resolved("plink_test_001", "recovered", "webhook") is True
        assert store.mark_resolved("plink_test_001", "recovered", "manual") is False


# ===========================================================================
# 4. Webhook triggers independent verification
# ===========================================================================


class TestWebhookTriggersVerification:
    def test_webhook_calls_verification_service(self):
        """Webhook must independently verify via Razorpay API, not trust the event."""
        from app.signals.service import _perform_independent_verification

        # Mock the verification service to return RECOVERED
        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_test_001",
            status="paid",
            amount=10000,
            amount_paid=10000,
            currency="INR",
            payments=[{"payment_id": "pay_test"}],
        )
        mock_provider = _make_mock_provider(response)
        mock_vs = VerificationService(provider=mock_provider)

        pending = _make_pending()

        # Patch the module-level singletons
        with patch("app.signals.service._verification_service", mock_vs), \
             patch("app.signals.service._pending_store", PendingRecoveryStore()) as store, \
             patch("app.signals.service._audit_service", AuditService(store=AuditStore())), \
             patch("app.signals.service._learning_service", LearningService(store=StrategyStore())):
            store.store(pending)
            result = _perform_independent_verification(pending, "webhook")

        assert result.verification_status == "recovered"
        # The mock provider should have been called (independent verification)
        mock_provider.fetch_payment_link.assert_called_once()


# ===========================================================================
# 5. Independently verified `paid` → RECOVERED
# ===========================================================================


class TestPaidReturnsRecovered:
    def test_paid_status_produces_recovered(self):
        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_test_paid",
            status="paid",
            amount=15000,
            amount_paid=15000,
            currency="INR",
            payments=[{"payment_id": "pay_paid_001"}],
        )
        provider = _make_mock_provider(response)
        vs = VerificationService(provider=provider)

        exec_result = ExecutionResult(
            case_id="case_paid",
            decision_id="dec_paid",
            capability_id="payment_link_recovery",
            action_type="create_payment_link",
            status=ExecutionStatus.EXECUTED,
            provider="razorpay",
            provider_reference="plink_test_paid",
        )

        outcome = vs.verify(
            execution_result=exec_result,
            amount_at_risk_minor=15000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.RECOVERED
        assert outcome.amount_recovered_minor == 15000


# ===========================================================================
# 6. `created` → PENDING
# ===========================================================================


class TestCreatedReturnsPending:
    def test_created_status_returns_pending(self):
        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_test_created",
            status="created",
            amount=10000,
            amount_paid=0,
            currency="INR",
        )
        provider = _make_mock_provider(response)
        vs = VerificationService(provider=provider)

        exec_result = ExecutionResult(
            case_id="case_created",
            decision_id="dec_created",
            capability_id="payment_link_recovery",
            action_type="create_payment_link",
            status=ExecutionStatus.EXECUTED,
            provider="razorpay",
            provider_reference="plink_test_created",
        )

        outcome = vs.verify(
            execution_result=exec_result,
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.PENDING
        assert outcome.amount_recovered_minor == 0


# ===========================================================================
# 7. `expired/cancelled` → NOT_RECOVERED
# ===========================================================================


class TestExpiredCancelledNotRecovered:
    @pytest.mark.parametrize("status", ["expired", "cancelled"])
    def test_terminal_failure_statuses(self, status):
        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_test_terminal",
            status=status,
            amount=10000,
            amount_paid=0,
            currency="INR",
        )
        provider = _make_mock_provider(response)
        vs = VerificationService(provider=provider)

        exec_result = ExecutionResult(
            case_id="case_terminal",
            decision_id="dec_terminal",
            capability_id="payment_link_recovery",
            action_type="create_payment_link",
            status=ExecutionStatus.EXECUTED,
            provider="razorpay",
            provider_reference="plink_test_terminal",
        )

        outcome = vs.verify(
            execution_result=exec_result,
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.NOT_RECOVERED


# ===========================================================================
# 8. Provider verification error → UNKNOWN
# ===========================================================================


class TestProviderErrorReturnsUnknown:
    def test_api_failure_returns_unknown(self):
        response = PaymentLinkVerificationResponse(
            success=False,
            error_message="API timeout",
        )
        provider = _make_mock_provider(response)
        vs = VerificationService(provider=provider)

        exec_result = ExecutionResult(
            case_id="case_error",
            decision_id="dec_error",
            capability_id="payment_link_recovery",
            action_type="create_payment_link",
            status=ExecutionStatus.EXECUTED,
            provider="razorpay",
            provider_reference="plink_test_error",
        )

        outcome = vs.verify(
            execution_result=exec_result,
            amount_at_risk_minor=10000,
            currency="INR",
        )

        assert outcome.status == VerificationStatus.UNKNOWN


# ===========================================================================
# 9. PENDING does not update learning
# ===========================================================================


class TestPendingNoLearning:
    def test_pending_skips_learning_update(self):
        learning_store = StrategyStore()
        learning_service = LearningService(store=learning_store)

        outcome = VerifiedOutcome(
            execution_id="exec_pending",
            case_id="case_pending",
            capability_id="payment_link_recovery",
            status=VerificationStatus.PENDING,
            amount_recovered_minor=0,
            amount_at_risk_minor=10000,
            currency="INR",
            reason="Payment link is created, awaiting customer.",
        )

        context_key = build_context_key()
        updated = learning_service.record_outcome(
            merchant_id="merchant_pending",
            capability_id="payment_link_recovery",
            context_key=context_key,
            verified_outcome=outcome,
        )

        assert updated is False
        stats = learning_service.get_statistics(
            "merchant_pending", "payment_link_recovery", context_key
        )
        assert stats.successes == 1  # Only prior
        assert stats.failures == 1  # Only prior


# ===========================================================================
# 10. RECOVERED updates learning exactly once
# ===========================================================================


class TestRecoveredLearningOnce:
    def test_recovered_updates_learning(self):
        """First RECOVERED → learning updated (successes += 1)."""
        from app.signals.service import _perform_independent_verification

        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_learn_001",
            status="paid",
            amount=10000,
            amount_paid=10000,
            currency="INR",
            payments=[{"payment_id": "pay_learn_001"}],
        )
        mock_provider = _make_mock_provider(response)
        mock_vs = VerificationService(provider=mock_provider)

        ls = StrategyStore()
        learning = LearningService(store=ls)
        ps = PendingRecoveryStore()

        pending = _make_pending(payment_link_id="plink_learn_001")
        ps.store(pending)

        with patch("app.signals.service._verification_service", mock_vs), \
             patch("app.signals.service._pending_store", ps), \
             patch("app.signals.service._audit_service", AuditService(store=AuditStore())), \
             patch("app.signals.service._learning_service", learning):
            result = _perform_independent_verification(pending, "webhook")

        assert result.verification_status == "recovered"
        assert result.learning_updated is True

        # Check that learning store was updated
        context_key = build_context_key()
        stats = learning.get_statistics(
            "merchant_evt", "payment_link_recovery", context_key
        )
        assert stats.successes == 2  # 1 prior + 1 recovered


# ===========================================================================
# 11. Duplicate webhook is idempotent
# ===========================================================================


class TestDuplicateWebhookIdempotent:
    def test_second_webhook_does_not_update_learning(self):
        """Second RECOVERED webhook for same plink → no duplicate learning."""
        from app.signals.service import _perform_independent_verification

        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_dup_001",
            status="paid",
            amount=10000,
            amount_paid=10000,
            currency="INR",
            payments=[{"payment_id": "pay_dup_001"}],
        )
        mock_provider = _make_mock_provider(response)
        mock_vs = VerificationService(provider=mock_provider)

        ls = StrategyStore()
        learning = LearningService(store=ls)
        ps = PendingRecoveryStore()

        pending = _make_pending(payment_link_id="plink_dup_001")
        ps.store(pending)

        with patch("app.signals.service._verification_service", mock_vs), \
             patch("app.signals.service._pending_store", ps), \
             patch("app.signals.service._audit_service", AuditService(store=AuditStore())), \
             patch("app.signals.service._learning_service", learning):
            # First call
            result1 = _perform_independent_verification(pending, "webhook")
            # Second call (duplicate)
            result2 = _perform_independent_verification(pending, "webhook")

        assert result1.verification_status == "recovered"
        assert result1.learning_updated is True

        assert result2.verification_status == "recovered"
        assert result2.learning_updated is False  # Duplicate — no update

        # Learning should have been updated exactly once
        context_key = build_context_key()
        stats = learning.get_statistics(
            "merchant_evt", "payment_link_recovery", context_key
        )
        assert stats.successes == 2  # 1 prior + 1 (not 3)


# ===========================================================================
# 12. Manual verification endpoint works
# ===========================================================================


class TestManualVerification:
    def test_manual_verify_returns_result(self):
        """Manual verification should trigger independent Razorpay API check."""
        from app.signals.service import verify_case_manually

        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_manual_001",
            status="paid",
            amount=10000,
            amount_paid=10000,
            currency="INR",
            payments=[{"payment_id": "pay_manual_001"}],
        )
        mock_provider = _make_mock_provider(response)
        mock_vs = VerificationService(provider=mock_provider)

        ps = PendingRecoveryStore()
        pending = _make_pending(
            payment_link_id="plink_manual_001",
            case_id="case_manual_001",
        )
        ps.store(pending)

        with patch("app.signals.service._verification_service", mock_vs), \
             patch("app.signals.service._pending_store", ps), \
             patch("app.signals.service._audit_service", AuditService(store=AuditStore())), \
             patch("app.signals.service._learning_service", LearningService(store=StrategyStore())):
            result = verify_case_manually("case_manual_001")

        assert result.case_id == "case_manual_001"
        assert result.verification_status == "recovered"
        assert result.amount_recovered_minor == 10000
        mock_provider.fetch_payment_link.assert_called_once()

    def test_manual_verify_case_not_found(self):
        """Manual verification for unknown case returns informative message."""
        from app.signals.service import verify_case_manually

        ps = PendingRecoveryStore()

        with patch("app.signals.service._pending_store", ps):
            result = verify_case_manually("case_nonexistent")

        assert result.case_id == "case_nonexistent"
        assert result.verification_status is None
        assert "No pending recovery" in result.message


# ===========================================================================
# 13. Manual verification does not create duplicate learning updates
# ===========================================================================


class TestManualNoDuplicateLearning:
    def test_manual_after_webhook_no_duplicate(self):
        """If webhook already resolved, manual verify does not re-update."""
        from app.signals.service import _perform_independent_verification

        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_nodup_001",
            status="paid",
            amount=10000,
            amount_paid=10000,
            currency="INR",
            payments=[{"payment_id": "pay_nodup_001"}],
        )
        mock_provider = _make_mock_provider(response)
        mock_vs = VerificationService(provider=mock_provider)

        ls = StrategyStore()
        learning = LearningService(store=ls)
        ps = PendingRecoveryStore()

        pending = _make_pending(payment_link_id="plink_nodup_001")
        ps.store(pending)

        with patch("app.signals.service._verification_service", mock_vs), \
             patch("app.signals.service._pending_store", ps), \
             patch("app.signals.service._audit_service", AuditService(store=AuditStore())), \
             patch("app.signals.service._learning_service", learning):
            # Webhook resolves first
            webhook_result = _perform_independent_verification(pending, "webhook")
            # Manual resolves second
            manual_result = _perform_independent_verification(pending, "manual")

        assert webhook_result.learning_updated is True
        assert manual_result.learning_updated is False

        context_key = build_context_key()
        stats = learning.get_statistics(
            "merchant_evt", "payment_link_recovery", context_key
        )
        assert stats.successes == 2  # 1 prior + 1 (not 3)


# ===========================================================================
# 14. Webhook endpoint remains fast
# ===========================================================================


class TestWebhookEndpointFast:
    def test_handle_recovery_webhook_dispatches_async(self):
        """handle_recovery_webhook should not block the event loop."""
        import asyncio

        from app.integrations.razorpay.events import RazorpayWebhookEvent
        from app.signals.service import handle_recovery_webhook

        event = RazorpayWebhookEvent(
            event_type="payment_link.paid",
            event_id="evt_fast_test",
            raw_body=b"{}",
            payload=_make_payment_link_paid_payload("plink_fast_001"),
        )

        ps = PendingRecoveryStore()
        # No pending recovery stored — should return quickly with "no pending"
        with patch("app.signals.service._pending_store", ps):
            result = asyncio.get_event_loop().run_until_complete(
                handle_recovery_webhook(event)
            )

        # The result should be returned without blocking
        assert result.payment_link_id == "plink_fast_001"
        assert result.case_id is None
        assert "No pending recovery" in result.message


# ===========================================================================
# Audit event recording for event-driven verification
# ===========================================================================


class TestAuditEventsRecorded:
    def test_webhook_verification_records_audit_events(self):
        """Full webhook verification should produce expected audit trail."""
        from app.signals.service import _perform_independent_verification

        response = PaymentLinkVerificationResponse(
            success=True,
            payment_link_id="plink_audit_001",
            status="paid",
            amount=10000,
            amount_paid=10000,
            currency="INR",
            payments=[{"payment_id": "pay_audit_001"}],
        )
        mock_provider = _make_mock_provider(response)
        mock_vs = VerificationService(provider=mock_provider)

        audit_store = AuditStore()
        audit_service = AuditService(store=audit_store)
        ps = PendingRecoveryStore()

        pending = _make_pending(
            payment_link_id="plink_audit_001",
            case_id="case_audit_evt",
        )
        ps.store(pending)

        with patch("app.signals.service._verification_service", mock_vs), \
             patch("app.signals.service._pending_store", ps), \
             patch("app.signals.service._audit_service", audit_service), \
             patch("app.signals.service._learning_service", LearningService(store=StrategyStore())):
            _perform_independent_verification(pending, "webhook")

        events = audit_service.get_case_audit("case_audit_evt")
        event_types = [e.event_type for e in events]

        assert AuditEventType.VERIFICATION_STARTED in event_types
        assert AuditEventType.VERIFICATION_COMPLETED in event_types
        assert AuditEventType.LEARNING_UPDATED in event_types
        assert AuditEventType.RECOVERY_RECOVERED in event_types
