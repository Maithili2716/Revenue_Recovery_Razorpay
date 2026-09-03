"""Audit service — convenient helpers for recording lifecycle events.

Provides typed methods for each audit event type so callers don't need
to construct AuditEvent objects manually.

All data recorded here must be sanitized — no secrets, no raw payloads.
"""

from __future__ import annotations

from typing import Any

from app.recovery.audit.models import AuditEvent, AuditEventType
from app.recovery.audit.store import AuditStore


class AuditService:
    """Records structured audit events for the recovery lifecycle."""

    def __init__(self, store: AuditStore) -> None:
        self._store = store

    def record(
        self,
        event_type: AuditEventType,
        case_id: str,
        merchant_id: str,
        actor: str,
        data: dict[str, Any] | None = None,
        signal_id: str | None = None,
        decision_id: str | None = None,
        execution_id: str | None = None,
    ) -> AuditEvent:
        """Record an audit event and return it."""
        event = AuditEvent(
            event_type=event_type,
            case_id=case_id,
            merchant_id=merchant_id,
            actor=actor,
            data=data or {},
            signal_id=signal_id,
            decision_id=decision_id,
            execution_id=execution_id,
        )
        self._store.append(event)
        return event

    def get_case_audit(self, case_id: str) -> list[AuditEvent]:
        """Get chronological audit trail for a case."""
        return self._store.get_case_audit(case_id)

    def get_all(self) -> list[AuditEvent]:
        """Get all audit events (for demo/diagnostics)."""
        return self._store.get_all()
