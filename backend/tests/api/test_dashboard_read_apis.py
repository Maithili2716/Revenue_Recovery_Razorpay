"""Focused tests for read-only dashboard APIs using isolated in-memory state."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import dashboard
from app.recovery.audit.models import AuditEventType
from app.recovery.audit.service import AuditService
from app.recovery.audit.store import AuditStore
from app.recovery.pending_store import PendingRecovery, PendingRecoveryStore


@pytest.fixture
def dashboard_state(monkeypatch):
    audit = AuditService(AuditStore())
    pending = PendingRecoveryStore()
    monkeypatch.setattr(dashboard, "get_audit_service", lambda: audit)
    monkeypatch.setattr(dashboard, "get_pending_store", lambda: pending)
    return audit, pending


def _record_case(audit: AuditService, *, case_id: str, amount: int, created_at: datetime) -> None:
    event = audit.record(
        event_type=AuditEventType.CASE_CREATED,
        case_id=case_id,
        merchant_id="merchant_dashboard",
        signal_id=f"signal_{case_id}",
        actor="risk_detector",
        data={
            "amount_at_risk_minor": amount,
            "currency": "INR",
            "risk_status": "at_risk",
            "recoverability": "likely",
            "urgency": "medium",
            "reason_codes": ["payment_failed"],
        },
    )
    event.timestamp = created_at


def test_dashboard_summary_empty_state(client: TestClient, dashboard_state) -> None:
    response = client.get("/dashboard/summary")

    assert response.status_code == 200
    assert response.json() == {
        "revenue_at_risk_minor": 0,
        "recovered_minor": 0,
        "recovery_rate": 0.0,
        "active_cases": 0,
        "total_cases": 0,
    }


def test_dashboard_summary_uses_known_case_and_pending_state(client: TestClient, dashboard_state) -> None:
    audit, pending = dashboard_state
    _record_case(audit, case_id="case_pending", amount=10_000, created_at=datetime.now(timezone.utc))
    pending.store(PendingRecovery(
        payment_link_id="plink_pending", case_id="case_pending", execution_id="exec_1",
        decision_id="dec_1", merchant_id="merchant_dashboard", capability_id="payment_link_recovery",
        signal_id="signal_case_pending", amount_at_risk_minor=10_000, currency="INR",
    ))

    response = client.get("/dashboard/summary")

    assert response.status_code == 200
    assert response.json()["revenue_at_risk_minor"] == 10_000
    assert response.json()["active_cases"] == 1
    assert response.json()["recovered_minor"] == 0


def test_recovery_status_distinguishes_escalated_from_genuinely_pending(dashboard_state) -> None:
    audit, pending = dashboard_state
    now = datetime.now(timezone.utc)
    _record_case(audit, case_id="case_escalated", amount=10_000, created_at=now)
    _record_case(audit, case_id="case_pending", amount=10_000, created_at=now - timedelta(seconds=1))
    for case_id, payment_link_id in (
        ("case_escalated", "plink_escalated"),
        ("case_pending", "plink_pending"),
    ):
        pending.store(PendingRecovery(
            payment_link_id=payment_link_id, case_id=case_id, execution_id=f"exec_{case_id}",
            decision_id=f"dec_{case_id}", merchant_id="merchant_dashboard",
            capability_id="payment_link_recovery", signal_id=f"signal_{case_id}",
            amount_at_risk_minor=10_000, currency="INR",
        ))
    audit.record(
        event_type=AuditEventType.RECOVERY_ESCALATED,
        case_id="case_escalated", merchant_id="merchant_dashboard",
        actor="recovery_boundary", data={"next_action": "merchant_follow_up"},
    )

    assert dashboard._recovery_status(
        audit.get_case_audit("case_escalated"),
        pending.get_by_case_id("case_escalated"),
    ) == "escalated"
    assert dashboard._recovery_status(
        audit.get_case_audit("case_pending"),
        pending.get_by_case_id("case_pending"),
    ) == "pending"


def test_dashboard_summary_counts_only_verified_recovery_and_calculates_rate(client: TestClient, dashboard_state) -> None:
    audit, _ = dashboard_state
    now = datetime.now(timezone.utc)
    _record_case(audit, case_id="case_recovered", amount=20_000, created_at=now)
    _record_case(audit, case_id="case_other", amount=10_000, created_at=now)
    audit.record(
        event_type=AuditEventType.RECOVERY_RECOVERED,
        case_id="case_recovered", merchant_id="merchant_dashboard", actor="recovery_webhook",
        data={"amount_recovered_minor": 20_000},
    )

    payload = client.get("/dashboard/summary").json()

    assert payload["recovered_minor"] == 20_000
    assert payload["recovery_rate"] == pytest.approx(20_000 / 30_000)
    assert payload["total_cases"] == 2


def test_recovery_cases_empty_and_does_not_fabricate_cases(client: TestClient, dashboard_state) -> None:
    response = client.get("/recovery/cases")

    assert response.status_code == 200
    assert response.json() == {"cases": []}


def test_recovery_cases_returns_actual_fields_newest_first(client: TestClient, dashboard_state) -> None:
    audit, _ = dashboard_state
    now = datetime.now(timezone.utc)
    _record_case(audit, case_id="case_old", amount=5_000, created_at=now - timedelta(minutes=1))
    _record_case(audit, case_id="case_new", amount=15_000, created_at=now)

    payload = client.get("/recovery/cases").json()

    assert [case["case_id"] for case in payload["cases"]] == ["case_new", "case_old"]
    assert payload["cases"][0]["amount_at_risk_minor"] == 15_000
    assert payload["cases"][0]["reason_codes"] == ["payment_failed"]
    assert payload["cases"][0]["recovery_status"] == "unknown"
