"""In-memory correlation between a demo checkout and recovery state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.signals.models import RevenueSignal


@dataclass
class DemoSession:
    """State needed to connect a demo checkout to its recovery case."""

    demo_id: str
    order_id: str
    amount_minor: int
    currency: str
    status: str = "payment_ready"
    case_id: str | None = None
    signal: RevenueSignal | None = None
    demo_customer_id: str | None = None
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class DemoSessionStore:
    """Process-local demo-session store, keyed by demo and Razorpay order IDs."""

    def __init__(self) -> None:
        self._by_demo_id: dict[str, DemoSession] = {}
        self._demo_id_by_order_id: dict[str, str] = {}

    def store(self, session: DemoSession) -> None:
        self._by_demo_id[session.demo_id] = session
        self._demo_id_by_order_id[session.order_id] = session.demo_id

    def get(self, demo_id: str) -> DemoSession | None:
        return self._by_demo_id.get(demo_id)

    def link_payment_failure(
        self, *, order_id: str | None, signal: RevenueSignal
    ) -> RevenueSignal:
        """Associate a normalized payment.failed signal with its demo order."""
        if not order_id:
            return signal
        demo_id = self._demo_id_by_order_id.get(order_id)
        if demo_id is None:
            return signal
        session = self._by_demo_id[demo_id]
        if session.demo_customer_id:
            # This is a demo-session-only handoff of explicitly configured
            # Test Mode data; the normal payment signal normalizer remains PII-free.
            signal = signal.model_copy(
                update={"customer_id": session.demo_customer_id}
            )
        session.signal = signal
        session.status = "payment_failed"
        return signal

    def link_recovery_case(self, *, signal_id: str, case_id: str) -> None:
        """Associate the detector-produced case with its already-linked signal."""
        for session in self._by_demo_id.values():
            if session.signal is not None and session.signal.signal_id == signal_id:
                session.case_id = case_id
                session.status = "recovery_case_created"
                return


_demo_session_store = DemoSessionStore()


def get_demo_session_store() -> DemoSessionStore:
    """Return the process-local demo session store."""
    return _demo_session_store
