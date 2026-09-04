"""Endpoints supporting the Razorpay Test Mode checkout demo."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.config import settings
from app.demo.store import DemoSession, get_demo_session_store
from app.integrations.razorpay.orders import RazorpayOrderClient
from app.recovery.audit.models import AuditEvent, AuditEventType
from app.signals.service import get_audit_service

router = APIRouter(prefix="/demo", tags=["demo"])


class TestPaymentRequest(BaseModel):
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)


class TestPaymentResponse(BaseModel):
    demo_id: str
    order_id: str
    amount_minor: int
    currency: str
    key_id: str
    status: Literal["payment_ready"]


class DemoSnapshot(BaseModel):
    demo_id: str
    order_id: str
    amount_minor: int
    currency: str
    status: str


class SignalSnapshot(BaseModel):
    signal_type: str
    provider: str
    amount_minor: int
    currency: str


class CaseSnapshot(BaseModel):
    case_id: str
    merchant_id: str | None = None
    amount_at_risk_minor: int | None = None
    currency: str | None = None
    risk_status: str | None = None
    recoverability: str | None = None
    urgency: str | None = None
    reason_codes: list[str] = Field(default_factory=list)


class DiagnosisSnapshot(BaseModel):
    category: str
    primary_reason: str
    failure_stage: str
    confidence: float
    diagnosis_source: str


class DecisionSnapshot(BaseModel):
    candidate_strategy_ids: list[str] = Field(default_factory=list)
    selected_strategy: str
    selected_action_type: str | None = None
    decision_source: str
    reason: str


class PolicySnapshot(BaseModel):
    verdict: str
    reasons: list[str] = Field(default_factory=list)


class ExecutionSnapshot(BaseModel):
    execution_status: str
    execution_id: str | None = None
    capability_id: str | None = None
    provider: str | None = None
    provider_reference: str | None = None
    payment_link_url: str | None = None
    error_message: str | None = None


class VerificationSnapshot(BaseModel):
    verification_status: str
    amount_recovered_minor: int = 0
    provider_reference: str | None = None
    provider_payment_id: str | None = None
    reason: str | None = None


class LearningSnapshot(BaseModel):
    updated: bool
    strategy: str | None = None
    outcome: str | None = None
    context_key: str | None = None


class ReminderSnapshot(BaseModel):
    status: str
    payment_link_id: str | None = None
    medium: str | None = None


class ActivitySnapshot(BaseModel):
    event_type: str
    timestamp: datetime
    metadata: dict[str, object] = Field(default_factory=dict)


class DemoStatusResponse(BaseModel):
    """Read-only aggregated state for a single live Test Mode demo."""

    demo: DemoSnapshot
    signal: SignalSnapshot | None = None
    case: CaseSnapshot | None = None
    diagnosis: DiagnosisSnapshot | None = None
    decision: DecisionSnapshot | None = None
    policy: PolicySnapshot | None = None
    execution: ExecutionSnapshot | None = None
    verification: VerificationSnapshot | None = None
    learning: LearningSnapshot | None = None
    reminder: ReminderSnapshot | None = None
    activity: list[ActivitySnapshot] = Field(default_factory=list)


@router.post("/test-payment", response_model=TestPaymentResponse)
def create_test_payment(request: TestPaymentRequest) -> TestPaymentResponse:
    """Create a Razorpay Test Mode order for browser Checkout."""
    demo_id = "demo_" + uuid.uuid4().hex
    currency = request.currency.upper()
    client = RazorpayOrderClient(
        key_id=settings.razorpay_key_id,
        key_secret=settings.razorpay_key_secret,
    )
    result = client.create_order(
        amount_minor=request.amount_minor,
        currency=currency,
        receipt=demo_id,
    )
    if not result.success or not result.order_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=result.error_message or "Unable to create Razorpay order.",
        )

    get_demo_session_store().store(
        DemoSession(
            demo_id=demo_id,
            order_id=result.order_id,
            amount_minor=result.amount if result.amount is not None else request.amount_minor,
            currency=result.currency or currency,
            demo_customer_id=_demo_customer_id(),
        )
    )
    return TestPaymentResponse(
        demo_id=demo_id,
        order_id=result.order_id,
        amount_minor=result.amount if result.amount is not None else request.amount_minor,
        currency=result.currency or currency,
        key_id=settings.razorpay_key_id,
        status="payment_ready",
    )


@router.get("/{demo_id}", response_model=DemoStatusResponse)
def get_demo_status(demo_id: str) -> DemoStatusResponse:
    """Return the demo session with its correlated existing recovery state."""
    session = get_demo_session_store().get(demo_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Demo session not found.")

    demo = DemoSnapshot(
        demo_id=session.demo_id, order_id=session.order_id,
        amount_minor=session.amount_minor, currency=session.currency,
        status=session.status,
    )
    signal = _signal_snapshot(session.signal) if session.signal else None
    events = (
        get_audit_service().get_case_audit(session.case_id)
        if session.case_id else []
    )
    case = _case_snapshot(session.case_id, events) if session.case_id else None
    return DemoStatusResponse(
        demo=demo,
        signal=signal,
        case=case,
        diagnosis=_diagnosis_snapshot(events),
        decision=_decision_snapshot(events),
        policy=_policy_snapshot(events),
        execution=_execution_snapshot(events),
        verification=_verification_snapshot(events),
        learning=_learning_snapshot(events),
        reminder=_reminder_snapshot(events),
        activity=[_activity_snapshot(event) for event in events],
    )


def _signal_snapshot(signal: object) -> SignalSnapshot:
    """Serialize only the signal fields exposed by the demo contract."""
    from app.signals.models import RevenueSignal

    assert isinstance(signal, RevenueSignal)
    return SignalSnapshot(signal_type=signal.signal_type.value, provider=signal.provider,
                          amount_minor=signal.amount_minor, currency=signal.currency)


def _case_snapshot(case_id: str, events: list[AuditEvent]) -> CaseSnapshot:
    """Read the detector-produced case attributes from the existing audit state."""
    case_event = next(
        (event for event in events if event.event_type == AuditEventType.CASE_CREATED),
        None,
    )
    if case_event is None:
        return CaseSnapshot(case_id=case_id)
    return CaseSnapshot(
        case_id=case_event.case_id, merchant_id=case_event.merchant_id,
        amount_at_risk_minor=_int_or_none(case_event.data.get("amount_at_risk_minor")),
        currency=_str_or_none(case_event.data.get("currency")),
        risk_status=_str_or_none(case_event.data.get("risk_status")),
        recoverability=_str_or_none(case_event.data.get("recoverability")),
        urgency=_str_or_none(case_event.data.get("urgency")),
        reason_codes=_strings(case_event.data.get("reason_codes")),
    )


def _latest(events: list[AuditEvent], event_type: AuditEventType) -> AuditEvent | None:
    return next((event for event in reversed(events) if event.event_type == event_type), None)


def _diagnosis_snapshot(events: list[AuditEvent]) -> DiagnosisSnapshot | None:
    event = _latest(events, AuditEventType.DIAGNOSIS_CREATED)
    if event is None:
        return None
    data = event.data
    category = _str_or_none(data.get("category"))
    reason = _str_or_none(data.get("primary_reason"))
    stage = _str_or_none(data.get("failure_stage"))
    confidence = data.get("confidence")
    source = _str_or_none(data.get("diagnosis_source"))
    if not all((category, reason, stage, source)) or not isinstance(confidence, (int, float)):
        return None
    return DiagnosisSnapshot(category=category, primary_reason=reason,
                             failure_stage=stage, confidence=float(confidence), diagnosis_source=source)


def _decision_snapshot(events: list[AuditEvent]) -> DecisionSnapshot | None:
    event = _latest(events, AuditEventType.DECISION_CREATED)
    if event is None:
        return None
    data = event.data
    strategy = _str_or_none(data.get("selected_capability_id"))
    source = _str_or_none(data.get("decision_source"))
    reason = _str_or_none(data.get("reason"))
    if not all((strategy, source, reason)):
        return None
    return DecisionSnapshot(candidate_strategy_ids=_strings(data.get("candidate_action_ids")),
                            selected_strategy=strategy,
                            selected_action_type=_str_or_none(data.get("selected_action_type")),
                            decision_source=source, reason=reason)


def _policy_snapshot(events: list[AuditEvent]) -> PolicySnapshot | None:
    event = _latest(events, AuditEventType.POLICY_DECISION)
    if event is None:
        return None
    verdict = _str_or_none(event.data.get("verdict"))
    if verdict is None:
        return None
    return PolicySnapshot(verdict=verdict, reasons=_strings(event.data.get("reasons")))


def _execution_snapshot(events: list[AuditEvent]) -> ExecutionSnapshot | None:
    event = _latest(events, AuditEventType.CAPABILITY_EXECUTED)
    if event is None:
        return None
    data = event.data
    execution_status = _str_or_none(data.get("status"))
    if execution_status is None:
        return None
    return ExecutionSnapshot(execution_status=execution_status, execution_id=event.execution_id,
                             # Capability execution events store the executed
                             # capability as their actor. Keep supporting the
                             # metadata form, but use the existing actor when
                             # older/live events do not duplicate it in data.
                             capability_id=_str_or_none(data.get("capability_id")) or event.actor,
                             provider=_str_or_none(data.get("provider")),
                             provider_reference=_str_or_none(data.get("provider_reference")),
                             payment_link_url=_str_or_none(data.get("payment_link_url")),
                             error_message=_str_or_none(data.get("error_message")))


def _verification_snapshot(events: list[AuditEvent]) -> VerificationSnapshot | None:
    if _latest(events, AuditEventType.RECOVERY_ESCALATED) is not None:
        return None
    event = next(
        (
            item for item in reversed(events)
            if item.event_type in {
                AuditEventType.VERIFICATION_COMPLETED,
                AuditEventType.VERIFICATION_PENDING,
            }
        ),
        None,
    )
    if event is None:
        return None
    data = event.data
    verification_status = _str_or_none(data.get("verification_status"))
    if verification_status is None:
        return None
    return VerificationSnapshot(verification_status=verification_status,
                                amount_recovered_minor=_int_or_zero(data.get("amount_recovered_minor")),
                                provider_reference=_str_or_none(data.get("provider_reference")),
                                provider_payment_id=_str_or_none(data.get("provider_payment_id")),
                                reason=_str_or_none(data.get("reason")))


def _learning_snapshot(events: list[AuditEvent]) -> LearningSnapshot | None:
    event = _latest(events, AuditEventType.LEARNING_UPDATED) or _latest(events, AuditEventType.LEARNING_SKIPPED)
    if event is None:
        return None
    data = event.data
    execution = _execution_snapshot(events)
    return LearningSnapshot(updated=event.event_type == AuditEventType.LEARNING_UPDATED,
                            strategy=_str_or_none(data.get("capability_id")) or (execution.capability_id if execution else None),
                            outcome=_str_or_none(data.get("verification_status")),
                            context_key=_str_or_none(data.get("context_key")))


def _reminder_snapshot(events: list[AuditEvent]) -> ReminderSnapshot | None:
    event = _latest(events, AuditEventType.REMINDER_SENT)
    if event is None:
        return None
    status = _str_or_none(event.data.get("status"))
    if status is None:
        return None
    return ReminderSnapshot(
        status=status,
        payment_link_id=_str_or_none(event.data.get("payment_link_id")),
        medium=_str_or_none(event.data.get("medium")),
    )


_ACTIVITY_KEYS = frozenset({"amount_at_risk_minor", "amount_recovered_minor", "currency", "risk_status", "recoverability", "urgency", "reason_codes", "selected_capability_id", "candidate_action_ids", "decision_source", "execution_status", "capability_id", "status", "provider", "provider_reference", "payment_link_url", "verification_status", "learning_updated", "context_key", "reason"})


def _activity_snapshot(event: AuditEvent) -> ActivitySnapshot:
    metadata = {key: value for key, value in event.data.items() if key in _ACTIVITY_KEYS}
    return ActivitySnapshot(event_type=event.event_type.value, timestamp=event.timestamp, metadata=metadata)


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _demo_customer_id() -> str | None:
    """Return an explicitly configured Razorpay Test Mode customer ID only."""
    customer_id = settings.demo_razorpay_customer_id
    return customer_id if customer_id and customer_id.startswith("cust_") else None
