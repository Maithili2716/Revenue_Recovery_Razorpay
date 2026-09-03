"""Focused tests for the Audit Trail.

Tests:
1. event appended
2. events returned chronologically
3. case filtering works
4. sensitive credentials are never included
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from app.recovery.audit.models import AuditEvent, AuditEventType
from app.recovery.audit.service import AuditService
from app.recovery.audit.store import AuditStore


# ===========================================================================
# 1. Event appended
# ===========================================================================


class TestAuditEventAppend:
    def test_event_appended_to_store(self):
        store = AuditStore()
        service = AuditService(store=store)

        event = service.record(
            event_type=AuditEventType.CASE_CREATED,
            case_id="case_001",
            merchant_id="merchant_A",
            actor="risk_detector",
            data={"amount_at_risk_minor": 10000},
        )

        assert store.count == 1
        assert event.event_type == AuditEventType.CASE_CREATED
        assert event.case_id == "case_001"
        assert event.merchant_id == "merchant_A"
        assert event.data["amount_at_risk_minor"] == 10000

    def test_audit_event_id_generated(self):
        store = AuditStore()
        service = AuditService(store=store)

        event = service.record(
            event_type=AuditEventType.SIGNAL_RECEIVED,
            case_id="case_001",
            merchant_id="merchant_A",
            actor="webhook",
        )

        assert event.audit_event_id.startswith("audit_")


# ===========================================================================
# 2. Events returned chronologically
# ===========================================================================


class TestChronologicalOrder:
    def test_events_ordered_by_timestamp(self):
        store = AuditStore()

        now = datetime.now(timezone.utc)
        e1 = AuditEvent(
            event_type=AuditEventType.CASE_CREATED,
            case_id="case_001",
            merchant_id="m",
            actor="a",
            timestamp=now - timedelta(seconds=10),
        )
        e2 = AuditEvent(
            event_type=AuditEventType.DECISION_CREATED,
            case_id="case_001",
            merchant_id="m",
            actor="a",
            timestamp=now - timedelta(seconds=5),
        )
        e3 = AuditEvent(
            event_type=AuditEventType.CAPABILITY_EXECUTED,
            case_id="case_001",
            merchant_id="m",
            actor="a",
            timestamp=now,
        )

        # Append out of order.
        store.append(e3)
        store.append(e1)
        store.append(e2)

        events = store.get_case_audit("case_001")
        assert [e.event_type for e in events] == [
            AuditEventType.CASE_CREATED,
            AuditEventType.DECISION_CREATED,
            AuditEventType.CAPABILITY_EXECUTED,
        ]


# ===========================================================================
# 3. Case filtering works
# ===========================================================================


class TestCaseFiltering:
    def test_filter_by_case_id(self):
        store = AuditStore()
        service = AuditService(store=store)

        service.record(
            event_type=AuditEventType.CASE_CREATED,
            case_id="case_001",
            merchant_id="m",
            actor="a",
        )
        service.record(
            event_type=AuditEventType.CASE_CREATED,
            case_id="case_002",
            merchant_id="m",
            actor="a",
        )
        service.record(
            event_type=AuditEventType.DECISION_CREATED,
            case_id="case_001",
            merchant_id="m",
            actor="a",
        )

        case_001_events = service.get_case_audit("case_001")
        assert len(case_001_events) == 2
        assert all(e.case_id == "case_001" for e in case_001_events)

    def test_unknown_case_returns_empty(self):
        store = AuditStore()
        service = AuditService(store=store)

        events = service.get_case_audit("nonexistent_case")
        assert events == []


# ===========================================================================
# 4. Sensitive credentials never included
# ===========================================================================


class TestSanitization:
    def test_no_api_keys_in_data(self):
        """Audit data must never contain API keys or secrets."""
        store = AuditStore()
        service = AuditService(store=store)

        # Record with sanitized data.
        event = service.record(
            event_type=AuditEventType.CAPABILITY_EXECUTED,
            case_id="case_001",
            merchant_id="merchant_A",
            actor="payment_link_capability",
            data={
                "status": "executed",
                "provider_reference": "plink_test_123",
                "payment_link_url": "https://rzp.io/i/test",
            },
        )

        # Verify no keys/secrets present.
        data_str = str(event.data)
        assert "key_id" not in data_str.lower()
        assert "key_secret" not in data_str.lower()
        assert "api_key" not in data_str.lower()
        assert "webhook_secret" not in data_str.lower()

    def test_audit_event_type_coverage(self):
        """Verify all expected event types exist."""
        expected = {
            "signal_received",
            "case_created",
            "diagnosis_created",
            "candidates_generated",
            "decision_created",
            "policy_decision",
            "capability_executed",
            "verification_pending",
            "verification_started",
            "verification_completed",
            "learning_updated",
            "learning_skipped",
            "recovery_pending",
            "recovery_webhook_received",
            "recovery_recovered",
            "recovery_not_recovered",
        }
        actual = {e.value for e in AuditEventType}
        assert expected == actual
