"""Read-only aggregate dashboard data derived from existing application state."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.recovery.audit.models import AuditEvent, AuditEventType
from app.signals.service import get_audit_service, get_pending_store

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


class DashboardSummaryResponse(BaseModel):
    """Aggregate values from known in-memory recovery cases.

    ``recovered_minor`` contains only amounts from independently verified,
    terminal recovery audit events; capability execution is never counted.
    """

    revenue_at_risk_minor: int = Field(ge=0)
    recovered_minor: int = Field(ge=0)
    recovery_rate: float = Field(ge=0.0)
    active_cases: int = Field(ge=0)
    total_cases: int = Field(ge=0)


class RecoveryCaseResponse(BaseModel):
    """A recovery case reconstructed from its authoritative audit record.

    ``created_at`` is the timestamp at which the case was recorded in the
    existing audit trail. ``recovery_status`` is ``unknown`` when no pending
    or terminal recovery state is currently available.
    """

    case_id: str
    signal_id: str | None = None
    merchant_id: str
    amount_at_risk_minor: int = Field(ge=0)
    currency: str | None = None
    risk_status: str | None = None
    recoverability: str | None = None
    urgency: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    created_at: str
    recovery_status: str


class RecoveryCasesResponse(BaseModel):
    """Recent known recovery cases, newest audit-recorded case first."""

    cases: list[RecoveryCaseResponse] = Field(default_factory=list)


@dataclass(frozen=True)
class _KnownCase:
    event: AuditEvent
    recovery_status: str
    recovered_minor: int


def get_known_cases() -> list[_KnownCase]:
    """Read case snapshots from audit and pending state without creating state."""
    audit_service = get_audit_service()
    pending_store = get_pending_store()
    case_events: dict[str, AuditEvent] = {}
    for event in audit_service.get_all():
        if event.event_type == AuditEventType.CASE_CREATED:
            case_events.setdefault(event.case_id, event)

    known: list[_KnownCase] = []
    for case_id, case_event in case_events.items():
        events = audit_service.get_case_audit(case_id)
        pending = pending_store.get_by_case_id(case_id)
        status = _recovery_status(events, pending)
        known.append(_KnownCase(
            event=case_event,
            recovery_status=status,
            recovered_minor=_verified_recovered_minor(events),
        ))
    return sorted(known, key=lambda item: item.event.timestamp, reverse=True)


@router.get("/summary", response_model=DashboardSummaryResponse)
def dashboard_summary() -> DashboardSummaryResponse:
    """Return metrics derived solely from existing in-memory recovery state."""
    cases = get_known_cases()
    revenue_at_risk = sum(
        _amount_at_risk(case.event) for case in cases
        if case.event.data.get("risk_status") == "at_risk"
    )
    recovered = sum(case.recovered_minor for case in cases)
    return DashboardSummaryResponse(
        revenue_at_risk_minor=revenue_at_risk,
        recovered_minor=recovered,
        recovery_rate=(recovered / revenue_at_risk if revenue_at_risk else 0.0),
        active_cases=sum(case.recovery_status == "pending" for case in cases),
        total_cases=len(cases),
    )


def recovery_cases_response() -> RecoveryCasesResponse:
    """Build the shared read-only response used by ``GET /recovery/cases``."""
    return RecoveryCasesResponse(cases=[
        RecoveryCaseResponse(
            case_id=case.event.case_id,
            signal_id=case.event.signal_id,
            merchant_id=case.event.merchant_id,
            amount_at_risk_minor=_amount_at_risk(case.event),
            currency=_optional_string(case.event.data.get("currency")),
            risk_status=_optional_string(case.event.data.get("risk_status")),
            recoverability=_optional_string(case.event.data.get("recoverability")),
            urgency=_optional_string(case.event.data.get("urgency")),
            reason_codes=_reason_codes(case.event),
            created_at=case.event.timestamp.isoformat(),
            recovery_status=case.recovery_status,
        )
        for case in get_known_cases()
    ])


def _recovery_status(events: list[AuditEvent], pending: object | None) -> str:
    """Derive a conservative status from authoritative state already held."""
    if pending is not None:
        if not pending.resolved:
            return "pending"
        if pending.resolution_status in {"recovered", "not_recovered"}:
            return pending.resolution_status

    event_types = {event.event_type for event in events}
    if AuditEventType.RECOVERY_RECOVERED in event_types:
        return "recovered"
    if AuditEventType.RECOVERY_NOT_RECOVERED in event_types:
        return "not_recovered"
    if AuditEventType.VERIFICATION_PENDING in event_types:
        return "pending"
    return "unknown"


def _verified_recovered_minor(events: list[AuditEvent]) -> int:
    """Return the latest verified recovered amount, never an execution amount."""
    for event in reversed(events):
        if event.event_type != AuditEventType.RECOVERY_RECOVERED:
            continue
        amount = event.data.get("amount_recovered_minor")
        return amount if isinstance(amount, int) and amount > 0 else 0
    return 0


def _amount_at_risk(event: AuditEvent) -> int:
    amount = event.data.get("amount_at_risk_minor")
    return amount if isinstance(amount, int) and amount > 0 else 0


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _reason_codes(event: AuditEvent) -> list[str]:
    codes = event.data.get("reason_codes")
    return [code for code in codes if isinstance(code, str)] if isinstance(codes, list) else []
