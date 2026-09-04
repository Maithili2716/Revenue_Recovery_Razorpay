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
    audit.record(event_type=AuditEventType.DECISION_CREATED, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="agent", data={"candidate_action_ids": ["payment_link_recovery"], "selected_capability_id": "payment_link_recovery", "selected_action_type": "create_payment_link", "decision_source": "contextual_bandit", "reason": "Eligible recovery action."})
    audit.record(event_type=AuditEventType.POLICY_DECISION, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="policy", data={"verdict": "allow", "reasons": ["All policy checks passed."]})
    audit.record(event_type=AuditEventType.CAPABILITY_EXECUTED, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="payment_link_recovery", execution_id="exec_snapshot", data={"status": "executed", "capability_id": "payment_link_recovery", "provider": "razorpay", "provider_reference": "plink_snapshot", "payment_link_url": "https://rzp.io/i/snapshot"})

    payload = client.get("/demo/demo_snapshot").json()

    assert payload["diagnosis"]["failure_stage"] == "payment_authorization"
    assert payload["decision"]["selected_strategy"] == "payment_link_recovery"
    assert payload["decision"]["selected_action_type"] == "create_payment_link"
    assert payload["policy"]["verdict"] == "allow"
    assert payload["execution"]["payment_link_url"] == "https://rzp.io/i/snapshot"
    assert payload["verification"] is None  # Execution is never represented as recovery.


def test_snapshot_exposes_actual_invoice_execution_details(client: TestClient, demo_state) -> None:
    sessions, audit = demo_state
    sessions.store(_session(signal=_signal(), case_id="case_snapshot"))
    _case_created(audit)
    audit.record(event_type=AuditEventType.DECISION_CREATED, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="agent", data={"candidate_action_ids": ["payment_link_recovery", "invoice_recovery"], "selected_capability_id": "invoice_recovery", "selected_action_type": "create_invoice", "decision_source": "contextual_bandit", "reason": "Eligible recovery action."})
    audit.record(event_type=AuditEventType.CAPABILITY_EXECUTED, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="invoice_recovery", execution_id="exec_invoice", data={"status": "executed", "provider": "razorpay", "provider_reference": "inv_snapshot", "payment_link_url": "https://rzp.io/i/invoice-snapshot"})

    payload = client.get("/demo/demo_snapshot").json()

    assert payload["decision"]["selected_strategy"] == "invoice_recovery"
    assert payload["decision"]["selected_action_type"] == "create_invoice"
    assert payload["execution"] == {
        "execution_status": "executed", "execution_id": "exec_invoice",
        "capability_id": "invoice_recovery", "provider": "razorpay",
        "provider_reference": "inv_snapshot",
        "payment_link_url": "https://rzp.io/i/invoice-snapshot", "error_message": None,
    }


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


def test_escalation_suppresses_older_pending_verification(client: TestClient, demo_state) -> None:
    sessions, audit = demo_state
    sessions.store(_session(signal=_signal(), case_id="case_snapshot"))
    _case_created(audit)
    audit.record(event_type=AuditEventType.VERIFICATION_PENDING, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="verification", data={"verification_status": "pending", "reason": "Awaiting payment."})
    audit.record(event_type=AuditEventType.CAPABILITY_EXECUTED, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="recovery_escalation", execution_id="exec_escalation", data={"status": "recovery_escalated", "capability_id": "recovery_escalation", "provider": "internal"})
    audit.record(event_type=AuditEventType.RECOVERY_ESCALATED, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="recovery_boundary", execution_id="exec_escalation", data={"next_action": "merchant_follow_up"})

    payload = client.get("/demo/demo_snapshot").json()

    assert payload["execution"]["execution_status"] == "recovery_escalated"
    assert payload["verification"] is None


def test_failed_execution_preserves_error_and_skips_verification_and_learning(client: TestClient, demo_state) -> None:
    sessions, audit = demo_state
    sessions.store(_session(signal=_signal(), case_id="case_snapshot"))
    _case_created(audit)
    audit.record(event_type=AuditEventType.CAPABILITY_EXECUTED, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="payment_link_recovery", execution_id="exec_snapshot", data={"status": "failed", "capability_id": "payment_link_recovery", "provider": "razorpay", "error_message": "Test Mode payment-link limit reached."})
    audit.record(event_type=AuditEventType.VERIFICATION_SKIPPED, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="verification_service", execution_id="exec_snapshot", data={"reason": "Capability execution failed; verification was not run.", "execution_status": "failed"})
    audit.record(event_type=AuditEventType.LEARNING_SKIPPED, case_id="case_snapshot", merchant_id="merchant_snapshot", actor="learning_service", execution_id="exec_snapshot", data={"reason": "Capability execution failed; learning was not updated.", "execution_status": "failed", "capability_id": "payment_link_recovery", "context_key": "payment_failure|unknown|medium", "learning_updated": False})

    payload = client.get("/demo/demo_snapshot").json()

    assert payload["execution"]["execution_status"] == "failed"
    assert payload["execution"]["error_message"] == "Test Mode payment-link limit reached."
    assert payload["verification"] is None
    assert payload["learning"] == {"updated": False, "strategy": "payment_link_recovery", "outcome": None, "context_key": "payment_failure|unknown|medium"}
    assert [event["event_type"] for event in payload["activity"][-2:]] == ["verification_skipped", "learning_skipped"]


def test_snapshot_read_has_no_side_effects(client: TestClient, demo_state) -> None:
    sessions, audit = demo_state
    sessions.store(_session(signal=_signal(), case_id="case_snapshot"))
    _case_created(audit)
    before = [event.model_dump() for event in audit.get_all()]

    response = client.get("/demo/demo_snapshot")

    assert response.status_code == 200
    assert [event.model_dump() for event in audit.get_all()] == before
