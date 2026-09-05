"""Recovery pipeline result — end-to-end outcome of a recovery workflow.

Aggregates execution, verification, and learning outcomes into a single
structure that the dashboard/batch evaluation can consume.

This does NOT claim that execution == recovery.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.recovery.capabilities.models import ExecutionStatus
from app.recovery.verification.models import VerificationStatus


class RecoveryPipelineResult(BaseModel):
    """End-to-end result of a recovery pipeline run."""

    case_id: str
    decision_id: str
    execution_id: str
    capability_id: str

    execution_status: ExecutionStatus
    verification_status: VerificationStatus | None = None

    amount_at_risk_minor: int
    amount_recovered_minor: int = 0
    currency: str

    provider_reference: str | None = None
    payment_link_url: str | None = None

    verification_reason: str | None = None
    learning_updated: bool = False

    completed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    metadata: dict[str, Any] = Field(default_factory=dict)
