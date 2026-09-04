"""Audit trail models.

Defines:
- AuditEventType — lifecycle event types
- AuditEvent — a single audit trail entry
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AuditEventType(str, Enum):
    """Recovery lifecycle event types."""

    SIGNAL_RECEIVED = "signal_received"
    CASE_CREATED = "case_created"
    DIAGNOSIS_CREATED = "diagnosis_created"
    CANDIDATES_GENERATED = "candidates_generated"
    DECISION_CREATED = "decision_created"
    POLICY_DECISION = "policy_decision"
    CAPABILITY_EXECUTED = "capability_executed"
    VERIFICATION_SKIPPED = "verification_skipped"
    VERIFICATION_PENDING = "verification_pending"
    VERIFICATION_STARTED = "verification_started"
    VERIFICATION_COMPLETED = "verification_completed"
    LEARNING_UPDATED = "learning_updated"
    LEARNING_SKIPPED = "learning_skipped"

    # Event-driven verification lifecycle
    RECOVERY_PENDING = "recovery_pending"
    RECOVERY_WEBHOOK_RECEIVED = "recovery_webhook_received"
    RECOVERY_RECOVERED = "recovery_recovered"
    RECOVERY_NOT_RECOVERED = "recovery_not_recovered"
    RECOVERY_ESCALATED = "recovery_escalated"

    # Reminder lifecycle
    REMINDER_SENT = "reminder_sent"


def _generate_audit_event_id() -> str:
    return "audit_" + uuid.uuid4().hex[:24]


class AuditEvent(BaseModel):
    """A single audit trail entry.

    Contains sanitized data — never API keys, secrets, or full raw
    provider payloads.
    """

    audit_event_id: str = Field(default_factory=_generate_audit_event_id)
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    event_type: AuditEventType

    case_id: str
    signal_id: str | None = None
    decision_id: str | None = None
    execution_id: str | None = None

    merchant_id: str

    actor: str = Field(
        description=(
            "The component/source that produced this event, "
            "e.g. 'signal_normalizer', 'agent', 'policy_engine', "
            "'payment_link_capability', 'verification_service', "
            "'learning_service'."
        )
    )

    data: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Sanitized event data. Must NEVER contain API keys, "
            "secrets, full customer PII, or raw provider payloads."
        ),
    )
