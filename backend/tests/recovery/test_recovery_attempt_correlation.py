"""Regression tests for Payment Link attempt-to-case correlation."""

from datetime import datetime, timezone
from unittest.mock import Mock

from app.recovery.agent.models import ActionType, AgentDecision, DecisionSource
from app.recovery.audit.models import AuditEventType
from app.recovery.audit.service import AuditService
from app.recovery.audit.store import AuditStore
from app.recovery.capabilities.invoice import InvoiceRecoveryCapability
from app.recovery.capabilities.models import ExecutionContext, ExecutionResult, ExecutionStatus
from app.recovery.pending_store import PendingRecovery, PendingRecoveryStore
from app.recovery.pipeline import RecoveryPipelineResult
from app.recovery.verification.models import VerificationStatus, VerifiedOutcome
from app.signals import service as signal_service
from app.signals.models import RevenueSignal, SignalStatus, SignalType
from app.integrations.razorpay.client import InvoiceResponse


def _signal(
    signal_id: str,
    *,
    payment_link_id: str | None = None,
    invoice_id: str | None = None,
    customer_id: str | None = None,
) -> RevenueSignal:
    metadata = {}
    if payment_link_id:
        metadata["payment_link_id"] = payment_link_id
    if invoice_id:
        metadata["invoice_id"] = invoice_id
    return RevenueSignal(
        signal_id=signal_id,
        merchant_id="merchant_correlation",
        customer_id=customer_id,
        signal_type=SignalType.PAYMENT_FAILURE,
        status=SignalStatus.FAILED,
        amount_minor=10_000,
        currency="INR",
        provider="razorpay",
        provider_event_id=f"evt_{signal_id}",
        provider_entity_id=f"pay_{signal_id}",
        failure_source="bank",
        failure_step="payment_authorization",
        occurred_at=datetime.now(timezone.utc),
        raw_event_type="payment.failed",
        metadata=metadata,
    )


class _Agent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, set[str] | None]] = []
        self.signal_customer_ids: list[str | None] = []

    def decide(self, signal, case, *, pending_payment_link_id=None, excluded_capability_ids=None):
        self.calls.append((case.case_id, pending_payment_link_id, excluded_capability_ids))
        self.signal_customer_ids.append(signal.customer_id)
        is_recovery_attempt = pending_payment_link_id is not None
        capability_id = (
            "payment_link_reminder"
            if is_recovery_attempt
            else "payment_link_recovery"
        )
        action_type = (
            ActionType.SEND_PAYMENT_LINK_REMINDER
            if is_recovery_attempt
            else ActionType.CREATE_PAYMENT_LINK
        )
        return AgentDecision(
            decision_id=f"dec_{case.case_id}_{len(self.calls)}",
            case_id=case.case_id,
            selected_capability_id=capability_id,
            selected_action_type=action_type,
            reason="Selected existing eligible capability.",
            candidate_action_ids=["payment_link_recovery", "payment_link_reminder"],
            decision_source=DecisionSource.CONTEXTUAL_BANDIT,
        )


class _Executor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.customer_ids: list[str | None] = []

    def execute(self, decision, case):
        self.calls.append((case.case_id, decision.selected_capability_id))
        self.customer_ids.append(case.customer_id)
        return ExecutionResult(
            execution_id=f"exec_{len(self.calls)}",
            case_id=case.case_id,
            decision_id=decision.decision_id,
            capability_id=decision.selected_capability_id,
            action_type=decision.selected_action_type.value,
            status=ExecutionStatus.EXECUTED,
            provider_reference=(
                "inv_origin_001"
                if decision.selected_capability_id == "invoice_recovery"
                else "plink_origin_001"
            ),
        )


def _unknown_outcome(**kwargs) -> VerifiedOutcome:
    return VerifiedOutcome(
        case_id=kwargs["case_id"],
        execution_id=kwargs["execution_result"].execution_id,
        capability_id=kwargs["execution_result"].capability_id,
        status=VerificationStatus.UNKNOWN,
        amount_at_risk_minor=kwargs["amount_at_risk_minor"],
        currency=kwargs["currency"],
    )


def test_payment_link_failure_reuses_original_case_and_unrelated_failure_creates_new_case(monkeypatch) -> None:
    audit = AuditService(AuditStore())
    pending_store = PendingRecoveryStore()
    agent = _Agent()
    executor = _Executor()
    verify = Mock(side_effect=_unknown_outcome)
    learning = Mock()
    learning.record_outcome.return_value = False
    monkeypatch.setattr(signal_service, "_audit_service", audit)
    monkeypatch.setattr(signal_service, "_pending_store", pending_store)
    monkeypatch.setattr(signal_service, "_agent", agent)
    monkeypatch.setattr(signal_service, "_executor", executor)
    monkeypatch.setattr(signal_service, "_verify_with_retries", verify)
    monkeypatch.setattr(signal_service, "_learning_service", learning)

    # Original ₹100 failure creates the only at-risk case and its Payment Link.
    original = _signal("sig_original")
    original_result = signal_service._run_pipeline(original)
    assert original_result is not None
    original_case_id = original_result.case_id
    pending = pending_store.get_by_payment_link_id("plink_origin_001")
    assert pending is not None
    assert pending.case_id == original_case_id
    assert pending.amount_at_risk_minor == 10_000
    assert pending.customer_id is None

    # A failed payment through that link belongs to the original case.
    recovery_attempt = _signal("sig_recovery_attempt", payment_link_id="plink_origin_001")
    recovery_result = signal_service._run_pipeline(recovery_attempt)
    assert recovery_result is not None
    assert recovery_result.case_id == original_case_id
    assert agent.calls[1] == (
        original_case_id,
        "plink_origin_001",
        {"payment_link_recovery"},
    )

    case_created_events = [
        event for event in audit.get_all()
        if event.event_type == AuditEventType.CASE_CREATED
    ]
    assert len(case_created_events) == 1
    assert case_created_events[0].case_id == original_case_id
    assert case_created_events[0].data["amount_at_risk_minor"] == 10_000
    assert any(
        event.event_type == AuditEventType.SIGNAL_RECEIVED
        and event.case_id == original_case_id
        and event.signal_id == recovery_attempt.signal_id
        and event.data["payment_link_id"] == "plink_origin_001"
        for event in audit.get_all()
    )

    # An unrelated payment failure still creates its own case.
    unrelated_result = signal_service._run_pipeline(_signal("sig_unrelated"))
    assert unrelated_result is not None
    assert unrelated_result.case_id != original_case_id
    assert agent.signal_customer_ids[2] is None
    assert len([
        event for event in audit.get_all()
        if event.event_type == AuditEventType.CASE_CREATED
    ]) == 2


class _InvoiceAfterRecoveryAttemptAgent(_Agent):
    def decide(self, signal, case, *, pending_payment_link_id=None, excluded_capability_ids=None):
        self.calls.append((case.case_id, pending_payment_link_id, excluded_capability_ids))
        self.signal_customer_ids.append(signal.customer_id)
        selected_invoice = excluded_capability_ids == {"payment_link_recovery"}
        capability_id = "invoice_recovery" if selected_invoice else "payment_link_recovery"
        return AgentDecision(
            decision_id=f"dec_{case.case_id}_{len(self.calls)}",
            case_id=case.case_id,
            selected_capability_id=capability_id,
            selected_action_type=(
                ActionType.CREATE_INVOICE
                if selected_invoice
                else ActionType.CREATE_PAYMENT_LINK
            ),
            reason="Selected the available recovery capability.",
            candidate_action_ids=["payment_link_recovery", "invoice_recovery"],
            decision_source=DecisionSource.CONTEXTUAL_BANDIT,
        )


def test_correlated_recovery_attempt_restores_customer_for_invoice_execution(monkeypatch) -> None:
    audit = AuditService(AuditStore())
    pending_store = PendingRecoveryStore()
    agent = _InvoiceAfterRecoveryAttemptAgent()
    executor = _Executor()
    verify = Mock(side_effect=_unknown_outcome)
    learning = Mock()
    learning.record_outcome.return_value = False
    monkeypatch.setattr(signal_service, "_audit_service", audit)
    monkeypatch.setattr(signal_service, "_pending_store", pending_store)
    monkeypatch.setattr(signal_service, "_agent", agent)
    monkeypatch.setattr(signal_service, "_executor", executor)
    monkeypatch.setattr(signal_service, "_verify_with_retries", verify)
    monkeypatch.setattr(signal_service, "_learning_service", learning)

    original_result = signal_service._run_pipeline(
        _signal("sig_original_customer", customer_id="cust_real_test_mode")
    )
    assert original_result is not None
    original_pending = pending_store.get_by_payment_link_id("plink_origin_001")
    assert original_pending is not None
    assert original_pending.customer_id == "cust_real_test_mode"

    recovery_result = signal_service._run_pipeline(
        _signal("sig_recovery_customer", payment_link_id="plink_origin_001")
    )

    assert recovery_result is not None
    assert recovery_result.case_id == original_result.case_id
    assert recovery_result.capability_id == "invoice_recovery"
    assert agent.calls[1] == (
        original_result.case_id,
        "plink_origin_001",
        {"payment_link_recovery"},
    )
    assert agent.signal_customer_ids == ["cust_real_test_mode", "cust_real_test_mode"]
    assert executor.customer_ids == ["cust_real_test_mode", "cust_real_test_mode"]
    assert pending_store.get_by_invoice_id("inv_origin_001") is not None

    unrelated_result = signal_service._run_pipeline(_signal("sig_unrelated_customer"))

    assert unrelated_result is not None
    assert unrelated_result.case_id != original_result.case_id
    assert agent.signal_customer_ids[-1] is None
    assert executor.customer_ids[-1] is None


class _InvoiceClient:
    def __init__(self) -> None:
        self.customer_id: str | None = None

    def create_invoice(self, **kwargs) -> InvoiceResponse:
        self.customer_id = kwargs["customer_id"]
        return InvoiceResponse(success=False, error_message="provider rejected request")


def test_invoice_capability_passes_restored_customer_to_provider() -> None:
    client = _InvoiceClient()
    capability = InvoiceRecoveryCapability(client)
    context = ExecutionContext(
        case_id="case_customer_context",
        decision_id="dec_customer_context",
        merchant_id="merchant_correlation",
        customer_id="cust_real_test_mode",
        amount_minor=10_000,
        currency="INR",
        capability_id="invoice_recovery",
        action_type="create_invoice",
        signal_id="sig_customer_context",
    )

    result = capability.execute(context)

    assert client.customer_id == "cust_real_test_mode"
    assert result.status == ExecutionStatus.FAILED
    assert result.provider_reference is None


def test_invoice_id_finds_pending_recovery() -> None:
    pending_store = PendingRecoveryStore()
    pending = PendingRecovery(
        payment_link_id=None,
        invoice_id="inv_test_failed001",
        provider_reference="inv_test_failed001",
        provider_type="invoice",
        case_id="case_original_invoice",
        execution_id="exec_invoice",
        decision_id="dec_invoice",
        merchant_id="merchant_correlation",
        capability_id="invoice_recovery",
        signal_id="sig_original_invoice",
        amount_at_risk_minor=10_000,
        currency="INR",
    )
    pending_store.store(pending)

    original_store = signal_service._pending_store
    try:
        signal_service._pending_store = pending_store
        found = signal_service._pending_recovery_for_signal(
            _signal("sig_failed_invoice", invoice_id="inv_test_failed001")
        )
    finally:
        signal_service._pending_store = original_store

    assert found is pending


def test_failed_invoice_payment_reuses_case_without_detecting_a_new_one(monkeypatch) -> None:
    audit = AuditService(AuditStore())
    pending_store = PendingRecoveryStore()
    original_case_id = "case_original_invoice_failure"
    invoice_id = "inv_test_failed002"
    audit.record(
        event_type=AuditEventType.CASE_CREATED,
        case_id=original_case_id,
        merchant_id="merchant_correlation",
        actor="risk_detector",
        signal_id="sig_original_invoice_failure",
        data={
            "amount_at_risk_minor": 10_000,
            "currency": "INR",
            "risk_status": "at_risk",
            "recoverability": "likely",
            "urgency": "medium",
            "reason_codes": ["payment_failed"],
        },
    )
    for capability_id in ("payment_link_recovery", "invoice_recovery"):
        audit.record(
            event_type=AuditEventType.CAPABILITY_EXECUTED,
            case_id=original_case_id,
            merchant_id="merchant_correlation",
            actor=capability_id,
            signal_id="sig_original_invoice_failure",
            data={"capability_id": capability_id, "status": "executed"},
        )
    pending_store.store(
        PendingRecovery(
            payment_link_id=None,
            invoice_id=invoice_id,
            provider_reference=invoice_id,
            provider_type="invoice",
            case_id=original_case_id,
            execution_id="exec_invoice_failure",
            decision_id="dec_invoice_failure",
            merchant_id="merchant_correlation",
            capability_id="invoice_recovery",
            signal_id="sig_original_invoice_failure",
            amount_at_risk_minor=10_000,
            currency="INR",
        )
    )
    escalated = RecoveryPipelineResult(
        case_id=original_case_id,
        decision_id="dec_escalation",
        execution_id="exec_escalation",
        capability_id="recovery_escalation",
        execution_status=ExecutionStatus.RECOVERY_ESCALATED,
        amount_at_risk_minor=10_000,
        currency="INR",
    )
    detect = Mock(side_effect=AssertionError("recovery attempt must not create a case"))
    escalate = Mock(return_value=escalated)
    monkeypatch.setattr(signal_service, "_audit_service", audit)
    monkeypatch.setattr(signal_service, "_pending_store", pending_store)
    monkeypatch.setattr(signal_service, "detect_recovery_case", detect)
    monkeypatch.setattr(signal_service, "_escalate_recovery", escalate)

    result = signal_service._run_pipeline(
        _signal("sig_failed_invoice_attempt", invoice_id=invoice_id)
    )

    assert result is not None
    assert result.case_id == original_case_id
    detect.assert_not_called()
    escalate.assert_called_once()
    case_events = [
        event for event in audit.get_all()
        if event.event_type == AuditEventType.CASE_CREATED
    ]
    assert len(case_events) == 1
    assert case_events[0].case_id == original_case_id
