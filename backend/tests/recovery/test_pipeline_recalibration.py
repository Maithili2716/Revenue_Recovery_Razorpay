"""Focused orchestration tests for bounded execution-failure recalibration."""

from datetime import datetime, timezone
from unittest.mock import Mock

from app.recovery.agent.models import ActionType, AgentDecision, DecisionSource
from app.recovery.audit.service import AuditService
from app.recovery.audit.store import AuditStore
from app.recovery.capabilities.models import ExecutionResult, ExecutionStatus
from app.recovery.pending_store import PendingRecoveryStore
from app.recovery.verification.models import VerificationStatus, VerifiedOutcome
from app.signals import service as signal_service
from app.signals.models import RevenueSignal, SignalStatus, SignalType


def _signal() -> RevenueSignal:
    return RevenueSignal(
        signal_id="sig_recalibration",
        merchant_id="merchant_recalibration",
        signal_type=SignalType.PAYMENT_FAILURE,
        status=SignalStatus.FAILED,
        amount_minor=10_000,
        currency="INR",
        provider="razorpay",
        provider_event_id="evt_recalibration",
        provider_entity_id="pay_recalibration",
        failure_source="bank",
        failure_step="payment_authorization",
        occurred_at=datetime.now(timezone.utc),
        raw_event_type="payment.failed",
    )


def _decision(capability_id: str) -> AgentDecision:
    action_type = {
        "payment_link_recovery": ActionType.CREATE_PAYMENT_LINK,
        "invoice_recovery": ActionType.CREATE_INVOICE,
        "payment_link_reminder": ActionType.SEND_PAYMENT_LINK_REMINDER,
    }[capability_id]
    return AgentDecision(
        decision_id=f"dec_{capability_id}",
        case_id="case_placeholder",
        selected_capability_id=capability_id,
        selected_action_type=action_type,
        reason=f"Selected {capability_id}.",
        candidate_action_ids=["payment_link_recovery", "payment_link_reminder"],
        decision_source=DecisionSource.CONTEXTUAL_BANDIT,
    )


class _Agent:
    def __init__(self, decisions: list[AgentDecision]) -> None:
        self._decisions = iter(decisions)
        self.calls = 0
        self.exclusions: list[set[str] | None] = []

    def decide(self, signal, case, **kwargs):
        self.calls += 1
        self.exclusions.append(kwargs.get("excluded_capability_ids"))
        decision = next(self._decisions)
        return decision.model_copy(update={"case_id": case.case_id})


class _Executor:
    def __init__(self, statuses: list[ExecutionStatus]) -> None:
        self._statuses = iter(statuses)
        self.calls: list[str] = []

    def execute(self, decision, case):
        self.calls.append(decision.selected_capability_id)
        status = next(self._statuses)
        return ExecutionResult(
            execution_id=f"exec_{len(self.calls)}",
            case_id=case.case_id,
            decision_id=decision.decision_id,
            capability_id=decision.selected_capability_id,
            action_type=decision.selected_action_type.value,
            status=status,
            provider_reference=("plink_recalibrated" if status == ExecutionStatus.EXECUTED else None),
            error_message=("provider unavailable" if status == ExecutionStatus.FAILED else None),
        )


def _outcome(case_id: str) -> VerifiedOutcome:
    return VerifiedOutcome(
        case_id=case_id,
        execution_id="exec_verified",
        capability_id="payment_link_reminder",
        status=VerificationStatus.RECOVERED,
        amount_at_risk_minor=10_000,
        amount_recovered_minor=10_000,
        currency="INR",
    )


def _wire(monkeypatch, agent, executor, verify, learning) -> None:
    monkeypatch.setattr(signal_service, "_audit_service", AuditService(AuditStore()))
    monkeypatch.setattr(signal_service, "_pending_store", PendingRecoveryStore())
    monkeypatch.setattr(signal_service, "_agent", agent)
    monkeypatch.setattr(signal_service, "_executor", executor)
    monkeypatch.setattr(signal_service, "_verify_with_retries", verify)
    monkeypatch.setattr(signal_service, "_learning_service", learning)


def test_successful_execution_keeps_verification_and_learning_path(monkeypatch) -> None:
    agent = _Agent([_decision("payment_link_recovery")])
    executor = _Executor([ExecutionStatus.EXECUTED])
    verify = Mock(side_effect=lambda **kwargs: _outcome(kwargs["case_id"]))
    learning = Mock()
    learning.record_outcome.return_value = False
    _wire(monkeypatch, agent, executor, verify, learning)

    result = signal_service._run_pipeline(_signal())

    assert agent.calls == 1
    assert executor.calls == ["payment_link_recovery"]
    verify.assert_called_once()
    learning.record_outcome.assert_called_once()
    assert result is not None
    assert result.execution_status == ExecutionStatus.EXECUTED
    assert result.verification_status == VerificationStatus.RECOVERED


def test_failed_execution_enters_recalibration_and_executes_second_decision(monkeypatch) -> None:
    agent = _Agent([_decision("payment_link_recovery"), _decision("payment_link_reminder")])
    executor = _Executor([ExecutionStatus.FAILED, ExecutionStatus.EXECUTED])
    verify = Mock(side_effect=lambda **kwargs: _outcome(kwargs["case_id"]))
    learning = Mock()
    learning.record_outcome.return_value = False
    _wire(monkeypatch, agent, executor, verify, learning)

    result = signal_service._run_pipeline(_signal())

    assert agent.calls == 2
    assert executor.calls == ["payment_link_recovery", "payment_link_reminder"]
    verify.assert_called_once()
    learning.record_outcome.assert_called_once()
    assert result is not None
    assert result.execution_status == ExecutionStatus.EXECUTED


def test_failed_execution_without_second_candidate_terminates_without_verification(monkeypatch) -> None:
    first = _decision("payment_link_recovery")
    no_alternative = first.model_copy(update={"candidate_action_ids": ["payment_link_recovery"]})
    agent = _Agent([first, no_alternative])
    executor = _Executor([ExecutionStatus.FAILED])
    verify = Mock(side_effect=AssertionError("failed execution must not verify"))
    learning = Mock()
    learning.record_outcome.side_effect = AssertionError("failed execution must not learn")
    _wire(monkeypatch, agent, executor, verify, learning)

    result = signal_service._run_pipeline(_signal())

    assert agent.calls == 2
    assert executor.calls == ["payment_link_recovery"]
    verify.assert_not_called()
    learning.record_outcome.assert_not_called()
    assert result is not None
    assert result.execution_status == ExecutionStatus.FAILED
    assert result.verification_status is None


def test_maximum_attempts_stops_after_two_failed_executions(monkeypatch) -> None:
    agent = _Agent([_decision("payment_link_recovery"), _decision("payment_link_reminder")])
    executor = _Executor([ExecutionStatus.FAILED, ExecutionStatus.FAILED])
    verify = Mock(side_effect=AssertionError("failed execution must not verify"))
    learning = Mock()
    learning.record_outcome.side_effect = AssertionError("failed execution must not learn")
    _wire(monkeypatch, agent, executor, verify, learning)

    result = signal_service._run_pipeline(_signal())

    assert agent.calls == 2
    assert executor.calls == ["payment_link_recovery", "payment_link_reminder"]
    verify.assert_not_called()
    learning.record_outcome.assert_not_called()
    assert result is not None
    assert result.execution_status == ExecutionStatus.FAILED


class _RecoveryAttemptAgent:
    def __init__(self) -> None:
        self.exclusions: list[set[str] | None] = []
        self.calls = 0

    def decide(self, signal, case, *, excluded_capability_ids=None, **_):
        self.calls += 1
        self.exclusions.append(excluded_capability_ids)
        if self.calls == 1:
            return _decision("payment_link_recovery").model_copy(
                update={"case_id": case.case_id}
            )
        if self.calls == 2:
            return _decision("invoice_recovery").model_copy(
                update={"case_id": case.case_id}
            )
        return None


class _RecoveryAttemptExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, decision, case):
        self.calls.append(decision.selected_capability_id)
        if decision.selected_capability_id == "recovery_escalation":
            return ExecutionResult(
                execution_id=f"exec_{len(self.calls)}",
                case_id=case.case_id,
                decision_id=decision.decision_id,
                capability_id="recovery_escalation",
                action_type="escalate_recovery",
                status=ExecutionStatus.RECOVERY_ESCALATED,
                provider="internal",
                metadata=decision.decision_context,
            )
        failed = decision.selected_capability_id == "invoice_recovery"
        return ExecutionResult(
            execution_id=f"exec_{len(self.calls)}",
            case_id=case.case_id,
            decision_id=decision.decision_id,
            capability_id=decision.selected_capability_id,
            action_type=decision.selected_action_type.value,
            status=ExecutionStatus.FAILED if failed else ExecutionStatus.EXECUTED,
            provider_reference=None if failed else "plink_recalibration_origin",
            error_message="customer is required" if failed else None,
        )


def test_payment_link_failure_then_invoice_failure_escalates(monkeypatch) -> None:
    audit = AuditService(AuditStore())
    pending_store = PendingRecoveryStore()
    agent = _RecoveryAttemptAgent()
    executor = _RecoveryAttemptExecutor()
    verify = Mock(side_effect=lambda **kwargs: _outcome(kwargs["case_id"]))
    learning = Mock()
    learning.record_outcome.return_value = False
    monkeypatch.setattr(signal_service, "_audit_service", audit)
    monkeypatch.setattr(signal_service, "_pending_store", pending_store)
    monkeypatch.setattr(signal_service, "_agent", agent)
    monkeypatch.setattr(signal_service, "_executor", executor)
    monkeypatch.setattr(signal_service, "_verify_with_retries", verify)
    monkeypatch.setattr(signal_service, "_learning_service", learning)

    original = signal_service._run_pipeline(_signal())
    assert original is not None
    verify.reset_mock()
    learning.record_outcome.reset_mock()

    recovery_signal = _signal().model_copy(
        update={
            "signal_id": "sig_recalibration_recovery_attempt",
            "provider_event_id": "evt_recalibration_recovery_attempt",
            "metadata": {"payment_link_id": "plink_recalibration_origin"},
        }
    )
    result = signal_service._run_pipeline(recovery_signal)

    assert result is not None
    assert result.execution_status == ExecutionStatus.RECOVERY_ESCALATED
    assert result.capability_id == "recovery_escalation"
    assert result.amount_recovered_minor == 0
    assert executor.calls == [
        "payment_link_recovery",
        "invoice_recovery",
        "recovery_escalation",
    ]
    assert agent.exclusions[1] == {"payment_link_recovery"}
    assert agent.calls == 2
    verify.assert_not_called()
    learning.record_outcome.assert_not_called()
    assert pending_store.get_by_invoice_id("inv_never_created") is None
    assert not any(
        event.event_type.value == "recovery_recovered"
        for event in audit.get_all()
    )
    escalation_events = [
        event
        for event in audit.get_all()
        if event.event_type.value == "recovery_escalated"
    ]
    assert len(escalation_events) == 1
    assert escalation_events[0].data == {
        "attempted_capabilities": [
            "invoice_recovery",
            "payment_link_recovery",
        ],
        "escalation_reason": "automated_recovery_boundary_reached",
        "next_action": "merchant_follow_up",
    }

    repeated = signal_service._run_pipeline(recovery_signal)

    assert repeated is not None
    assert repeated.execution_status == ExecutionStatus.RECOVERY_ESCALATED
    assert agent.calls == 2
    assert executor.calls == [
        "payment_link_recovery",
        "invoice_recovery",
        "recovery_escalation",
    ]
    assert len([
        event for event in audit.get_all()
        if event.event_type.value == "case_created"
    ]) == 1


def test_escalation_capability_is_internal_and_does_not_call_razorpay() -> None:
    from app.recovery.capabilities.escalation import RecoveryEscalationCapability
    from app.recovery.capabilities.models import ExecutionContext

    capability = RecoveryEscalationCapability()
    result = capability.execute(
        ExecutionContext(
            case_id="case_escalation",
            decision_id="decision_escalation",
            merchant_id="merchant_escalation",
            amount_minor=10_000,
            currency="INR",
            capability_id="recovery_escalation",
            action_type="escalate_recovery",
            signal_id="signal_escalation",
            decision_context={
                "attempted_capabilities": [
                    "payment_link_recovery",
                    "invoice_recovery",
                ]
            },
        )
    )

    assert result.status == ExecutionStatus.RECOVERY_ESCALATED
    assert result.provider == "internal"
    assert result.provider_reference is None
    assert result.payment_link_url is None
