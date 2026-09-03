"""Focused regression tests for bounded diagnosis and policy observability."""

from datetime import datetime, timezone

from app.policy.models import PolicyDecision, PolicyVerdict
from app.recovery.agent.models import (
    ActionType,
    AgentDecision,
    DecisionSource,
    Diagnosis,
    DiagnosisCategory,
    FailureStage,
)
from app.recovery.audit.models import AuditEventType
from app.recovery.audit.service import AuditService
from app.recovery.audit.store import AuditStore
from app.recovery.capabilities.models import ExecutionResult, ExecutionStatus
from app.recovery.models import Recoverability, RecoveryCase, RiskStatus, Urgency
from app.recovery.verification.models import VerificationStatus, VerifiedOutcome
from app.signals import service as signal_service
from app.signals.models import RevenueSignal, SignalStatus, SignalType


def _signal() -> RevenueSignal:
    return RevenueSignal(
        signal_id="sig_observe", merchant_id="merchant_observe",
        signal_type=SignalType.PAYMENT_FAILURE, status=SignalStatus.FAILED,
        amount_minor=10_000, currency="INR", provider="razorpay",
        provider_event_id="evt_observe", provider_entity_id="pay_observe",
        failure_source="bank", failure_step="payment_authorization",
        occurred_at=datetime.now(timezone.utc), raw_event_type="payment.failed",
    )


def test_pipeline_audits_existing_diagnosis_and_policy_once(monkeypatch) -> None:
    audit = AuditService(AuditStore())
    diagnosis = Diagnosis(
        category=DiagnosisCategory.PAYMENT_FAILURE, primary_reason="bank_decline",
        failure_stage=FailureStage.PAYMENT_AUTHORIZATION, confidence=0.9,
        diagnosis_source="deterministic",
    )

    class Agent:
        def decide(self, signal, case, **_):
            return AgentDecision(
                decision_id="dec_observe", case_id=case.case_id,
                selected_capability_id="payment_link_recovery",
                selected_action_type=ActionType.CREATE_PAYMENT_LINK,
                reason="Selected existing candidate.",
                candidate_action_ids=["payment_link_recovery"],
                decision_source=DecisionSource.CONTEXTUAL_BANDIT,
                diagnosis=diagnosis,
            )

    policy = PolicyDecision(
        verdict=PolicyVerdict.ALLOW, case_id="case_unused", decision_id="dec_observe",
        capability_id="payment_link_recovery", reasons=["All policy checks passed."],
    )

    class Executor:
        def execute(self, decision, case):
            return ExecutionResult(
                execution_id="exec_observe", case_id=case.case_id,
                decision_id=decision.decision_id, capability_id=decision.selected_capability_id,
                action_type=decision.selected_action_type.value, status=ExecutionStatus.FAILED,
                policy_decision=policy, error_message="Simulated capability failure.",
            )

    outcome = VerifiedOutcome(
        case_id="case_unused", execution_id="exec_observe", capability_id="payment_link_recovery",
        status=VerificationStatus.UNKNOWN, amount_at_risk_minor=10_000, currency="INR",
    )
    monkeypatch.setattr(signal_service, "_audit_service", audit)
    monkeypatch.setattr(signal_service, "_agent", Agent())
    monkeypatch.setattr(signal_service, "_executor", Executor())
    monkeypatch.setattr(signal_service, "_verify_with_retries", lambda **_: outcome)

    signal_service._run_pipeline(_signal())

    events = audit.get_all()
    diagnosis_event = next(event for event in events if event.event_type == AuditEventType.DIAGNOSIS_CREATED)
    policy_event = next(event for event in events if event.event_type == AuditEventType.POLICY_DECISION)
    assert diagnosis_event.data == {
        "category": "payment_failure", "primary_reason": "bank_decline",
        "failure_stage": "payment_authorization", "confidence": 0.9,
        "diagnosis_source": "deterministic",
    }
    assert policy_event.data["verdict"] == "allow"
    assert policy_event.data["reasons"] == ["All policy checks passed."]
    assert sum(event.event_type == AuditEventType.DIAGNOSIS_CREATED for event in events) == 1
