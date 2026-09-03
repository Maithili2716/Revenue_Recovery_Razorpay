"""Focused tests for pending Payment Link wiring into agent decision.

Verifies:
1. No pending recovery → agent receives pending_payment_link_id=None.
2. Matching pending recovery → agent receives its payment_link_id.
3. Unrelated pending recovery (different case) is not used.
4. Existing multi-candidate selection tests remain valid (covered
   separately in test_multi_candidate_selection.py).
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
from unittest.mock import patch

import pytest

from app.recovery.pending_store import PendingRecovery, PendingRecoveryStore


# ---------------------------------------------------------------------------
# Helper: build a PendingRecovery entry
# ---------------------------------------------------------------------------


def _make_pending(
    *,
    payment_link_id: str = "plink_wire_001",
    case_id: str = "case_wire_001",
    execution_id: str = "exec_wire_001",
    decision_id: str = "dec_wire_001",
    merchant_id: str = "merchant_wire",
    capability_id: str = "payment_link_recovery",
    signal_id: str = "sig_wire_001",
    amount_at_risk_minor: int = 50000,
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


# ===========================================================================
# 1. No pending recovery → agent receives None
# ===========================================================================


class TestNoPendingRecovery:
    def test_empty_store_returns_none(self):
        """get_by_case_id returns None when the store is empty."""
        store = PendingRecoveryStore()
        assert store.get_by_case_id("case_nonexistent") is None

    def test_pipeline_passes_none_when_no_pending(self, caplog):
        """Pipeline log shows has_pending_payment_link=False."""
        from app.signals.service import _run_pipeline
        from app.signals.models import RevenueSignal, SignalStatus, SignalType
        from datetime import datetime, timezone

        signal = RevenueSignal(
            signal_id="sig_nopending_001",
            merchant_id="acc_nopending",
            customer_id=None,
            signal_type=SignalType.PAYMENT_FAILURE,
            status=SignalStatus.FAILED,
            amount_minor=10000,
            currency="INR",
            provider="razorpay",
            provider_event_id="evt_nopending",
            provider_entity_id="pay_nopending",
            reason="Payment failed",
            failure_source="bank",
            failure_step="payment_authorization",
            occurred_at=datetime.now(timezone.utc),
            raw_event_type="payment.failed",
            metadata={"method": "card"},
        )

        # Use an empty pending store — no prior Payment Link.
        empty_store = PendingRecoveryStore()

        with caplog.at_level(logging.INFO), \
             patch("app.signals.service._pending_store", empty_store):
            _run_pipeline(signal)

        # Find the agent_pending_link_context log.
        ctx_logs = [
            r for r in caplog.records
            if r.message == "agent_pending_link_context"
        ]
        assert len(ctx_logs) == 1
        log = ctx_logs[0]
        assert log.pending_payment_link_id is None
        assert log.has_pending_payment_link is False


# ===========================================================================
# 2. Matching pending recovery → agent receives its payment_link_id
# ===========================================================================


class TestMatchingPendingRecovery:
    def test_store_returns_matching_pending(self):
        """get_by_case_id returns the pending entry for the matching case."""
        store = PendingRecoveryStore()
        pending = _make_pending(case_id="case_match_001", payment_link_id="plink_match_001")
        store.store(pending)

        result = store.get_by_case_id("case_match_001")
        assert result is not None
        assert result.payment_link_id == "plink_match_001"

    def test_pipeline_passes_pending_link_id(self, caplog):
        """Pipeline log shows has_pending_payment_link=True with correct ID."""
        from app.signals.service import _run_pipeline
        from app.signals.models import RevenueSignal, SignalStatus, SignalType
        from app.recovery.models import build_case_id
        from datetime import datetime, timezone

        signal = RevenueSignal(
            signal_id="sig_haspending_001",
            merchant_id="acc_haspending",
            customer_id=None,
            signal_type=SignalType.PAYMENT_FAILURE,
            status=SignalStatus.FAILED,
            amount_minor=10000,
            currency="INR",
            provider="razorpay",
            provider_event_id="evt_haspending",
            provider_entity_id="pay_haspending",
            reason="Payment failed",
            failure_source="bank",
            failure_step="payment_authorization",
            occurred_at=datetime.now(timezone.utc),
            raw_event_type="payment.failed",
            metadata={"method": "card"},
        )

        # Determine the case_id that detect_recovery_case will produce.
        expected_case_id = build_case_id(signal.signal_id)

        # Populate the pending store with a matching entry.
        store_with_pending = PendingRecoveryStore()
        pending = _make_pending(
            case_id=expected_case_id,
            payment_link_id="plink_haspending_001",
            merchant_id=signal.merchant_id,
        )
        store_with_pending.store(pending)

        with caplog.at_level(logging.INFO), \
             patch("app.signals.service._pending_store", store_with_pending):
            _run_pipeline(signal)

        ctx_logs = [
            r for r in caplog.records
            if r.message == "agent_pending_link_context"
        ]
        assert len(ctx_logs) == 1
        log = ctx_logs[0]
        assert log.pending_payment_link_id == "plink_haspending_001"
        assert log.has_pending_payment_link is True


# ===========================================================================
# 3. Unrelated pending recovery (different case) is not used
# ===========================================================================


class TestUnrelatedPendingRecovery:
    def test_different_case_id_returns_none(self):
        """get_by_case_id does not return entries from a different case."""
        store = PendingRecoveryStore()
        pending = _make_pending(case_id="case_other_999", payment_link_id="plink_other_999")
        store.store(pending)

        assert store.get_by_case_id("case_this_001") is None

    def test_pipeline_ignores_unrelated_pending(self, caplog):
        """Pipeline must not use a pending entry belonging to a different case."""
        from app.signals.service import _run_pipeline
        from app.signals.models import RevenueSignal, SignalStatus, SignalType
        from app.recovery.models import build_case_id
        from datetime import datetime, timezone

        signal = RevenueSignal(
            signal_id="sig_unrelated_001",
            merchant_id="acc_unrelated",
            customer_id=None,
            signal_type=SignalType.PAYMENT_FAILURE,
            status=SignalStatus.FAILED,
            amount_minor=10000,
            currency="INR",
            provider="razorpay",
            provider_event_id="evt_unrelated",
            provider_entity_id="pay_unrelated",
            reason="Payment failed",
            failure_source="bank",
            failure_step="payment_authorization",
            occurred_at=datetime.now(timezone.utc),
            raw_event_type="payment.failed",
            metadata={"method": "card"},
        )

        expected_case_id = build_case_id(signal.signal_id)

        # Store a pending entry for a DIFFERENT case.
        store_with_other = PendingRecoveryStore()
        other_pending = _make_pending(
            case_id="case_someone_else_999",
            payment_link_id="plink_someone_else_999",
            merchant_id="acc_other_merchant",
        )
        store_with_other.store(other_pending)

        with caplog.at_level(logging.INFO), \
             patch("app.signals.service._pending_store", store_with_other):
            _run_pipeline(signal)

        ctx_logs = [
            r for r in caplog.records
            if r.message == "agent_pending_link_context"
        ]
        assert len(ctx_logs) == 1
        log = ctx_logs[0]
        # Must NOT pick up the unrelated pending entry.
        assert log.pending_payment_link_id is None
        assert log.has_pending_payment_link is False
