"""Pending recovery store — in-memory correlation for event-driven verification.

Maps payment_link_id → recovery context so that incoming webhooks
(e.g. payment_link.paid) can be correlated to the pending recovery case.

Lifecycle:
    1. Capability execution creates a Payment Link → store pending entry
    2. Webhook or manual verification resolves the entry
    3. Entry is marked as resolved (not deleted — audit trail safety)

For this hackathon MVP, the store is in-memory.
A future implementation can persist to a database.

Thread safety: same as StrategyStore — single-process, GIL-protected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PendingRecovery:
    """A recovery action awaiting verification."""

    payment_link_id: str
    case_id: str
    execution_id: str
    decision_id: str
    merchant_id: str
    capability_id: str
    signal_id: str

    amount_at_risk_minor: int
    currency: str

    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # Terminal resolution tracking
    resolved: bool = False
    resolved_at: datetime | None = None
    resolution_status: str | None = None  # "recovered", "not_recovered", etc.
    resolution_source: str | None = None  # "webhook", "manual", "polling"


class PendingRecoveryStore:
    """In-memory store for pending recovery correlations.

    Keyed by payment_link_id (the Razorpay plink_... identifier).
    """

    def __init__(self) -> None:
        self._store: dict[str, PendingRecovery] = {}

    def store(self, entry: PendingRecovery) -> None:
        """Store a pending recovery entry."""
        self._store[entry.payment_link_id] = entry
        logger.info(
            "pending_recovery_stored",
            extra={
                "payment_link_id": entry.payment_link_id,
                "case_id": entry.case_id,
                "execution_id": entry.execution_id,
                "merchant_id": entry.merchant_id,
                "amount_at_risk_minor": entry.amount_at_risk_minor,
            },
        )

    def get_by_payment_link_id(
        self, payment_link_id: str
    ) -> PendingRecovery | None:
        """Look up a pending recovery by payment link ID."""
        return self._store.get(payment_link_id)

    def get_by_case_id(self, case_id: str) -> PendingRecovery | None:
        """Look up a pending recovery by case ID."""
        for entry in self._store.values():
            if entry.case_id == case_id:
                return entry
        return None

    def mark_resolved(
        self,
        payment_link_id: str,
        status: str,
        source: str,
    ) -> bool:
        """Mark a pending recovery as resolved.

        Returns True if the entry was found and updated, False otherwise.
        Idempotent: if already resolved, returns False (no duplicate updates).
        """
        entry = self._store.get(payment_link_id)
        if entry is None:
            return False
        if entry.resolved:
            logger.info(
                "pending_recovery_already_resolved",
                extra={
                    "payment_link_id": payment_link_id,
                    "case_id": entry.case_id,
                    "existing_status": entry.resolution_status,
                    "attempted_status": status,
                    "attempted_source": source,
                },
            )
            return False
        entry.resolved = True
        entry.resolved_at = datetime.now(timezone.utc)
        entry.resolution_status = status
        entry.resolution_source = source
        logger.info(
            "pending_recovery_resolved",
            extra={
                "payment_link_id": payment_link_id,
                "case_id": entry.case_id,
                "status": status,
                "source": source,
            },
        )
        return True

    def get_all_pending(self) -> list[PendingRecovery]:
        """Return all unresolved pending recoveries."""
        return [e for e in self._store.values() if not e.resolved]

    def get_all(self) -> list[PendingRecovery]:
        """Return all entries (for diagnostics/demo)."""
        return list(self._store.values())

    @property
    def count(self) -> int:
        return len(self._store)

    @property
    def pending_count(self) -> int:
        return sum(1 for e in self._store.values() if not e.resolved)
