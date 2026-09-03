"""Audit trail store — in-memory append-only event store.

For this hackathon MVP, events are stored in memory.
A future block can persist to a database.

The store is append-only: events can be added but never modified or deleted.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from app.recovery.audit.models import AuditEvent

logger = logging.getLogger(__name__)


class AuditStore:
    """In-memory, append-only audit event store."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._by_case: dict[str, list[AuditEvent]] = defaultdict(list)

    def append(self, event: AuditEvent) -> None:
        """Append an audit event to the store."""
        self._events.append(event)
        self._by_case[event.case_id].append(event)

        logger.info(
            "audit_event_recorded",
            extra={
                "audit_event_id": event.audit_event_id,
                "event_type": event.event_type.value,
                "case_id": event.case_id,
                "merchant_id": event.merchant_id,
                "actor": event.actor,
            },
        )

    def get_case_audit(self, case_id: str) -> list[AuditEvent]:
        """Return all events for a case, ordered chronologically."""
        events = self._by_case.get(case_id, [])
        return sorted(events, key=lambda e: e.timestamp)

    def get_all(self) -> list[AuditEvent]:
        """Return all events ordered chronologically (for demo/diagnostics)."""
        return sorted(self._events, key=lambda e: e.timestamp)

    @property
    def count(self) -> int:
        return len(self._events)
