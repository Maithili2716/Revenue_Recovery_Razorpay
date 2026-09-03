"""Focused tests for the read-only live demo snapshot contract."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import demo
from app.demo.store import DemoSession, DemoSessionStore
from app.recovery.audit.models import AuditEventType
from app.recovery.audit.service import AuditService
from app.recovery.audit.store import AuditStore
from app.signals.models import RevenueSignal, SignalStatus, SignalType


@pytest.fixture
def demo_state(monkeypatch):
    sessions = DemoSessionStore()
    audit = AuditService(AuditStore())
    monkeypatch.setattr(demo, "get_demo_session_store", lambda: sessions)
    monkeypatch.setattr(demo, "get_audit_service", lambda: audit)
    return sessions, audit


def _session(*, signal: RevenueSignal | None = None, case_id: str | None = None) -> DemoSession:
    return DemoSession(
        demo_id="demo_snapshot", order_id="order_snapshot", amount_minor=10_000,
        currency="INR", status="payment_ready" if signal is None else "recovery_case_created",
        signal=signal, case_id=case_id,
    )


def _signal() -> RevenueSignal:
    return RevenueSignal(
        signal_id="sig_snapshot", merchant_id="merchant_snapshot",
        signal_type=SignalType.PAYMENT_FAILURE, status=SignalStatus.FAILED,
        amount_minor=10_000, currency="INR", provider="razorpay",
        provider_event_id="event_snapshot", provider_entity_id="pay_snapshot",
        occurred_at=datetime.now(timezone.utc), raw_event_type="payment.failed",
    )


def _case_created(audit: AuditService) -> None:
    audit.record(
        event_type=AuditEventType.CASE_CREATED, case_id="case_snapshot",
        merchant_id="merchant_snapshot", signal_id="sig_snapshot", actor="risk_detector",
        data={"amount_at_risk_minor": 10_000, "currency": "INR", "risk_status": "at_risk",
              "recoverability": "likely", "urgency": "medium", "reason_codes": ["payment_failed"]},
    )


def test_fresh_demo_snapshot_has_safe_empty_optional_sections(client: TestClient, demo_state) -> None:
    sessions, _ = demo_state
    sessions.store(_session())

    payload = client.get("/demo/demo_snapshot").json()

    assert payload["demo"]["status"] == "payment_ready"
    assert payload["signal"] is None and payload["case"] is None
    assert all(payload[key] is None for key in ("diagnosis", "decision", "policy", "execution", "verification", "learning"))
    assert payload["activity"] == []


def test_signal_and_case_are_exposed_from_existing_demo_and_audit_state(client: TestClient, demo_state) -> None:
    sessions, audit = demo_state
    sessions.store(_session(signal=_signal(), case_id="case_snapshot"))
    _case_created(audit)

    payload = client.get("/demo/demo_snapshot").json()

    assert payload["signal"]["signal_type"] == "payment_failure"
    assert payload["case"]["amount_at_risk_minor"] == 10_000
    assert payload["case"]["reason_codes"] == ["payment_failed"]
    assert payload["activity"][0]["event_type"] == "case_created"


def test_snapshot_exposes_available_diagnosis_decision_policy_and_execution(client: TestClient, demo_state) -> None:
    sessions, audit = demo_state
    sessions.store(_session(signal=_signal(), case_id="case_snapshot"))
    _case_created(audit)
    audit.record(event_type=AuditEventType.DIAGNOSIS_CREATED, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="agent", data={"category": "payment_failure", "primary_reason": "declined", "failure_stage": "payment_authorization", "confidence": 0.9, "diagnosis_source": "deterministic"})
    audit.record(event_type=AuditEventType.DECISION_CREATED, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="agent", data={"candidate_action_ids": ["payment_link_recovery"], "selected_capability_id": "payment_link_recovery", "decision_source": "contextual_bandit", "reason": "Eligible recovery action."})
    audit.record(event_type=AuditEventType.POLICY_DECISION, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="policy", data={"verdict": "allow", "reasons": ["All policy checks passed."]})
    audit.record(event_type=AuditEventType.CAPABILITY_EXECUTED, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="payment_link_recovery", execution_id="exec_snapshot", data={"status": "executed", "capability_id": "payment_link_recovery", "provider": "razorpay", "provider_reference": "plink_snapshot", "payment_link_url": "https://rzp.io/i/snapshot"})

    payload = client.get("/demo/demo_snapshot").json()

    assert payload["diagnosis"]["failure_stage"] == "payment_authorization"
    assert payload["decision"]["selected_strategy"] == "payment_link_recovery"
    assert payload["policy"]["verdict"] == "allow"
    assert payload["execution"]["payment_link_url"] == "https://rzp.io/i/snapshot"
    assert payload["verification"] is None  # Execution is never represented as recovery.


def test_pending_unknown_and_verified_recovery_remain_distinct(client: TestClient, demo_state) -> None:
    sessions, audit = demo_state
    sessions.store(_session(signal=_signal(), case_id="case_snapshot"))
    _case_created(audit)
    audit.record(event_type=AuditEventType.VERIFICATION_PENDING, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="verification", data={"verification_status": "pending", "reason": "Awaiting payment."})
    assert client.get("/demo/demo_snapshot").json()["verification"]["verification_status"] == "pending"
    audit.record(event_type=AuditEventType.VERIFICATION_COMPLETED, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="verification", data={"verification_status": "unknown", "reason": "Provider unavailable."})
    assert client.get("/demo/demo_snapshot").json()["verification"]["verification_status"] == "unknown"
    audit.record(event_type=AuditEventType.VERIFICATION_COMPLETED, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="verification", data={"verification_status": "recovered", "amount_recovered_minor": 10_000, "provider_reference": "plink_snapshot", "provider_payment_id": "pay_snapshot"})
    audit.record(event_type=AuditEventType.LEARNING_UPDATED, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="learning", data={"capability_id": "payment_link_recovery", "verification_status": "recovered", "context_key": "payment_failure|unknown|medium"})

    payload = client.get("/demo/demo_snapshot").json()

    assert payload["verification"]["verification_status"] == "recovered"
    assert payload["verification"]["amount_recovered_minor"] == 10_000
    assert payload["learning"] == {"updated": True, "strategy": "payment_link_recovery", "outcome": "recovered", "context_key": "payment_failure|unknown|medium"}


def test_snapshot_read_has_no_side_effects(client: TestClient, demo_state) -> None:
    sessions, audit = demo_state
    sessions.store(_session(signal=_signal(), case_id="case_snapshot"))
    _case_created(audit)
    before = [event.model_dump() for event in audit.get_all()]

    response = client.get("/demo/demo_snapshot")

    assert response.status_code == 200
    assert [event.model_dump() for event in audit.get_all()] == before
