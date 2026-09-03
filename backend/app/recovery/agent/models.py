"""Data models for the Adaptive Recovery Agent decision loop.

All models in this module are provider-agnostic Pydantic models representing
the internal state of the agent as it progresses through its decision pipeline:

    AgentContext → Diagnosis → CandidateAction → AgentDecision

No execution, no database, no LLM calls — just structured data contracts.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Agent Context
# ---------------------------------------------------------------------------


class AgentContext(BaseModel):
    """Information available to the agent for making a recovery decision.

    Built from a RevenueSignal and RecoveryCase.  Does NOT fabricate customer
    identity or historical information — unknown data is explicitly empty.
    """

    case_id: str
    signal_id: str
    merchant_id: str
    customer_id: str | None = None

    # Financial
    amount_at_risk_minor: int
    currency: str

    # Signal classification
    signal_type: str
    failure_reason: str | None = None
    failure_source: str | None = None
    failure_step: str | None = None
    payment_method: str | None = None

    # Recovery history — explicitly empty until history tracking is built.
    previous_attempts: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Previous recovery attempts for this case.  Empty list when no "
            "history is available — never fabricated."
        ),
    )

    # Time / context
    signal_occurred_at: datetime
    context_built_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # Risk assessment from the RecoveryCase
    recoverability: str
    urgency: str
    reason_codes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------


class DiagnosisCategory(str, Enum):
    PAYMENT_FAILURE = "payment_failure"
    UNKNOWN = "unknown"


class FailureStage(str, Enum):
    PAYMENT_AUTHORIZATION = "payment_authorization"
    PAYMENT_PROCESSING = "payment_processing"
    PAYMENT_CAPTURE = "payment_capture"
    UNKNOWN = "unknown"


class Diagnosis(BaseModel):
    """Structured diagnosis of a recovery case.

    Produced by deterministic logic or LLM reasoning inside the agent.
    """

    category: DiagnosisCategory
    primary_reason: str = Field(
        description="Machine-readable primary reason for the failure."
    )
    failure_stage: FailureStage
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Confidence in the diagnosis (0.0–1.0).  Deterministic rules use 1.0 or explicit lower values."
    )
    reason_codes: list[str] = Field(default_factory=list)
    details: str | None = Field(
        default=None,
        description="Optional human-readable explanation."
    )
    diagnosis_source: str = Field(
        default="deterministic",
        description="Origin of this diagnosis: 'llm', 'deterministic', or 'deterministic_fallback'."
    )


# ---------------------------------------------------------------------------
# Candidate Action
# ---------------------------------------------------------------------------


class ActionType(str, Enum):
    CREATE_PAYMENT_LINK = "create_payment_link"


class EligibilityStatus(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"


class CandidateAction(BaseModel):
    """A single candidate recovery action proposed by the agent."""

    capability_id: str = Field(
        description="Unique identifier for this candidate, e.g. 'payment_link_recovery'."
    )
    action_type: ActionType
    priority: int = Field(
        ge=1,
        description="Priority rank (1 = highest priority)."
    )
    rationale: str = Field(
        description="Why this action is being proposed."
    )
    eligibility: EligibilityStatus = Field(
        default=EligibilityStatus.ELIGIBLE,
        description="Whether this action is currently eligible for execution."
    )


# ---------------------------------------------------------------------------
# Agent Decision
# ---------------------------------------------------------------------------


class DecisionSource(str, Enum):
    """Where the decision came from."""

    CONTEXTUAL_BANDIT = "contextual_bandit"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"


def build_decision_id(case_id: str) -> str:
    """Return a deterministic decision identifier from a case_id.

    Using a hash ensures the same case always produces the same decision_id
    within a single decision cycle.
    """
    return "dec_" + hashlib.sha256(case_id.encode()).hexdigest()[:24]


class AgentDecision(BaseModel):
    """The final output of the Adaptive Recovery Agent decision loop.

    Represents: "What should we do for this recovery case?"
    Does NOT execute the action.
    """

    decision_id: str
    case_id: str

    # Selected action
    selected_capability_id: str
    selected_action_type: ActionType

    # Reasoning
    reason: str = Field(
        description="Human-readable explanation of why this action was selected."
    )

    # Candidate info
    candidate_action_ids: list[str] = Field(
        description="All capability_ids that were considered."
    )

    # Context snapshot
    decision_context: dict[str, Any] = Field(
        default_factory=dict,
        description="Summarized context used for the decision (no PII, no raw payloads)."
    )

    decision_source: DecisionSource

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
