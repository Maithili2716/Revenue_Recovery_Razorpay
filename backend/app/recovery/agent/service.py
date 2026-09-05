"""Adaptive Recovery Agent — orchestration service.

Clean service boundary:

    AdaptiveRecoveryAgent.decide(signal, recovery_case) → AgentDecision

Internally orchestrates:
1. Build context
2. Diagnose (LLM with deterministic fallback)
3. Generate candidate actions
4. Ask the bandit to select
5. Produce AgentDecision

Each responsibility is delegated to a modular component.
This service is the orchestrator, not the entire application.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.recovery.agent.bandit import ContextualBandit
from app.recovery.agent.candidates import generate_candidates_with_context
from app.recovery.agent.context import build_agent_context
from app.recovery.agent.diagnosis import diagnose as deterministic_diagnose
from app.recovery.agent.models import (
    AgentContext,
    AgentDecision,
    DecisionSource,
    Diagnosis,
    build_decision_id,
)
from app.recovery.models import RecoveryCase
from app.signals.models import RevenueSignal

logger = logging.getLogger(__name__)


def _build_llm_provider():
    """Create a GrokDiagnosisProvider if the API key is configured.

    Returns None when the key is absent — the agent will use the
    deterministic engine instead.
    """
    api_key = settings.grok_api_key
    if not api_key:
        logger.info("agent_llm_provider_disabled", extra={"reason": "grok_api_key_not_configured"})
        return None

    from app.recovery.agent.llm_diagnosis import GrokDiagnosisProvider

    return GrokDiagnosisProvider(api_key=api_key, model=settings.grok_model)


class AdaptiveRecoveryAgent:
    """Orchestrates the agent decision loop.

    Stateless per-call: each invocation of ``decide()`` runs through the
    full pipeline and produces an independent AgentDecision.
    """

    def __init__(self, learning_store=None) -> None:
        self._bandit = ContextualBandit(learning_store=learning_store)
        self._llm_provider = _build_llm_provider()

    def _diagnose(self, context: AgentContext) -> Diagnosis:
        """Produce a diagnosis — LLM when available, deterministic otherwise."""
        if self._llm_provider is not None:
            return self._llm_provider.diagnose(context)

        return deterministic_diagnose(context)

    def decide(
        self,
        signal: RevenueSignal,
        recovery_case: RecoveryCase,
        *,
        pending_payment_link_id: str | None = None,
        excluded_capability_ids: set[str] | None = None,
    ) -> AgentDecision | None:
        """Run the full agent decision pipeline.

        Args:
            signal: The incoming revenue signal.
            recovery_case: The recovery case derived from the signal.
            pending_payment_link_id: If an existing pending (unpaid) Payment
                Link is associated with this case, pass its ID.  When present,
                a ``payment_link_reminder`` candidate is added alongside
                ``payment_link_recovery`` and both compete via the bandit.
            excluded_capability_ids: Existing capability IDs that must not be
                selected for a recalibration attempt.

        Returns:
            AgentDecision if a recovery action is recommended.
            None if no eligible action exists.
        """
        # 1. Build context
        context = build_agent_context(signal, recovery_case)

        # 2. Diagnose (LLM → deterministic fallback)
        diagnosis_result = self._diagnose(context)

        # 3. Generate candidate actions (including reminder when pending link exists)
        candidates = generate_candidates_with_context(
            context,
            diagnosis_result,
            pending_payment_link_id=pending_payment_link_id,
        )
        if excluded_capability_ids:
            candidates = [
                candidate
                for candidate in candidates
                if candidate.capability_id not in excluded_capability_ids
            ]

        if not candidates:
            logger.warning(
                "agent_no_candidates",
                extra={
                    "case_id": recovery_case.case_id,
                    "diagnosis_category": diagnosis_result.category.value,
                },
            )
            return None

        # 4. Ask the bandit to select
        selection = self._bandit.select(candidates, context)

        if selection is None:
            logger.warning(
                "agent_no_selection",
                extra={"case_id": recovery_case.case_id},
            )
            return None

        # 5. Produce AgentDecision. The bounded exclusion is supplied only by
        # pipeline recalibration after a real prior recovery attempt; preserve
        # it in the existing decision reason so the audit trail explains why a
        # previously attempted capability was not selected again.
        decision_reason = selection.selection_reason
        if excluded_capability_ids:
            excluded = ", ".join(sorted(excluded_capability_ids))
            decision_reason = (
                f"{decision_reason} Previously attempted capability excluded: "
                f"{excluded}."
            )
        decision = AgentDecision(
            decision_id=build_decision_id(recovery_case.case_id),
            case_id=recovery_case.case_id,
            selected_capability_id=selection.selected.capability_id,
            selected_action_type=selection.selected.action_type,
            reason=decision_reason,
            candidate_action_ids=[c.capability_id for c in candidates],
            decision_context={
                "signal_type": context.signal_type,
                "amount_at_risk_minor": context.amount_at_risk_minor,
                "currency": context.currency,
                "failure_source": context.failure_source,
                "failure_step": context.failure_step,
                "recoverability": context.recoverability,
                "urgency": context.urgency,
                "diagnosis_category": diagnosis_result.category.value,
                "diagnosis_primary_reason": diagnosis_result.primary_reason,
                "diagnosis_confidence": diagnosis_result.confidence,
                "diagnosis_source": diagnosis_result.diagnosis_source,
            },
            decision_source=DecisionSource.CONTEXTUAL_BANDIT,
            diagnosis=diagnosis_result,
        )

        logger.info(
            "agent_decision_created",
            extra={
                "case_id": decision.case_id,
                "decision_id": decision.decision_id,
                "selected_capability_id": decision.selected_capability_id,
                "selected_action_type": decision.selected_action_type.value,
                "candidate_action_ids": decision.candidate_action_ids,
                "diagnosis_category": diagnosis_result.category.value,
                "diagnosis_primary_reason": diagnosis_result.primary_reason,
                "diagnosis_source": diagnosis_result.diagnosis_source,
                "decision_source": decision.decision_source.value,
                "reason": decision.reason,
            },
        )

        return decision
