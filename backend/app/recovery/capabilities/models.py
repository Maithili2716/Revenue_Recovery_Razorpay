"""Capability execution models.

Defines:
- ExecutionStatus — the outcome of a capability execution
- ExecutionContext — the information passed to a capability
- ExecutionResult — the structured result returned by a capability
- RecoveryCapability — abstract base class for all capabilities

CRITICAL DISTINCTION:

    ExecutionStatus.EXECUTED  ≠  "money recovered"

    Creating a payment link is an EXECUTION outcome.
    Actual financial recovery is only established by the Verification Engine.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Execution Status
# ---------------------------------------------------------------------------


class ExecutionStatus(str, Enum):
    """Outcome of a capability execution attempt.

    EXECUTED: The capability action was performed (e.g. payment link created).
              This does NOT mean money was recovered.
    FAILED:   The capability action failed (e.g. Razorpay API error).
    BLOCKED:  The capability was not executed due to policy/guardrail block.
    """

    EXECUTED = "executed"
    FAILED = "failed"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Execution Context
# ---------------------------------------------------------------------------


class ExecutionContext(BaseModel):
    """Information passed to a capability for execution.

    Contains everything a capability needs to perform its action,
    derived from the RecoveryCase and AgentDecision.
    """

    case_id: str
    decision_id: str
    merchant_id: str
    customer_id: str | None = None

    # Financial
    amount_minor: int = Field(
        description="Amount in minor currency units (e.g. paise for INR)."
    )
    currency: str

    # Action selection
    capability_id: str
    action_type: str

    # Signal context
    signal_id: str
    reason_codes: list[str] = Field(default_factory=list)

    # Extensible metadata from the decision
    decision_context: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Execution Result
# ---------------------------------------------------------------------------


def _generate_execution_id() -> str:
    """Generate a unique execution identifier."""
    return "exec_" + uuid.uuid4().hex[:24]


class ExecutionResult(BaseModel):
    """Structured result of a capability execution.

    Contains enough context for the future Verification and Audit layers.

    IMPORTANT: status=EXECUTED means the recovery *action* was performed
    (e.g. a payment link was created).  It does NOT mean money was recovered.
    The Verification Engine determines actual financial recovery.
    """

    execution_id: str = Field(default_factory=_generate_execution_id)
    case_id: str
    decision_id: str
    capability_id: str
    action_type: str

    status: ExecutionStatus

    # Provider information
    provider: str = Field(
        default="razorpay",
        description="The external provider used for execution.",
    )
    provider_reference: str | None = Field(
        default=None,
        description=(
            "Unique identifier from the provider (e.g. Razorpay payment link ID). "
            "None when the action was blocked or failed before reaching the provider."
        ),
    )
    payment_link_url: str | None = Field(
        default=None,
        description=(
            "The actual payment link URL (Razorpay short_url) that a customer "
            "can visit to complete payment. None for non-payment-link capabilities "
            "or when execution failed/was blocked."
        ),
    )

    # Timing
    executed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # Failure / block information
    error_message: str | None = Field(
        default=None,
        description="Human-readable error description when status is FAILED or BLOCKED.",
    )

    # Extensible metadata for audit / verification
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Additional execution details (e.g. payment link short_url, expiry). "
            "Never contains API keys or sensitive credentials."
        ),
    )


# ---------------------------------------------------------------------------
# Capability Interface  (Abstract Base Class)
# ---------------------------------------------------------------------------


class RecoveryCapability(ABC):
    """Abstract base class for recovery capabilities.

    Every capability must declare:
    - capability_id: unique identifier
    - action_type: what kind of action it performs

    And implement:
    - execute(context) → ExecutionResult

    Capabilities do NOT bypass the policy/guardrails layer.
    The executor service is responsible for enforcing the policy boundary.
    """

    @property
    @abstractmethod
    def capability_id(self) -> str:
        """Unique identifier for this capability."""
        ...

    @property
    @abstractmethod
    def action_type(self) -> str:
        """The type of action this capability performs."""
        ...

    @abstractmethod
    def execute(self, context: ExecutionContext) -> ExecutionResult:
        """Execute the recovery action.

        Args:
            context: The execution context containing case, merchant,
                     and financial details.

        Returns:
            An ExecutionResult describing what happened.
            status=EXECUTED means the action was performed,
            NOT that money was recovered.
        """
        ...
