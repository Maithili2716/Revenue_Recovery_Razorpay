"""Policy decision models.

The output of the policy/guardrails engine.  Every capability execution
must pass through this boundary before the Razorpay API (or any other
external action) is called.

    AgentDecision  →  PolicyEngine  →  PolicyDecision(ALLOW | BLOCK)
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class PolicyVerdict(str, Enum):
    """Binary outcome of the policy evaluation."""

    ALLOW = "allow"
    BLOCK = "block"


class PolicyDecision(BaseModel):
    """Result of evaluating an agent decision against policy/guardrails."""

    verdict: PolicyVerdict
    case_id: str
    decision_id: str
    capability_id: str

    reasons: list[str] = Field(
        default_factory=list,
        description="Human-readable reasons explaining why the verdict was reached.",
    )

    evaluated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
