"""Capability registry — maps capability IDs to implementations.

Simple explicit registry.  No plugin marketplace, no dynamic module loading.

    capability_id  →  RecoveryCapability instance

Usage:
    registry = CapabilityRegistry()
    registry.register(PaymentLinkRecoveryCapability(...))
    capability = registry.get("payment_link_recovery")  # or None
"""

from __future__ import annotations

import logging

from app.recovery.capabilities.models import RecoveryCapability

logger = logging.getLogger(__name__)


class CapabilityRegistry:
    """Maps capability_id strings to RecoveryCapability instances.

    The registry is intentionally simple.  Adding a new capability requires:
    1. Implementing the RecoveryCapability interface.
    2. Calling registry.register(instance).

    No configuration file parsing, no dynamic discovery.
    """

    def __init__(self) -> None:
        self._capabilities: dict[str, RecoveryCapability] = {}

    def register(self, capability: RecoveryCapability) -> None:
        """Register a capability instance.

        Raises ValueError if a capability with the same ID is already
        registered (prevents accidental shadowing).
        """
        cid = capability.capability_id
        if cid in self._capabilities:
            raise ValueError(
                f"Capability '{cid}' is already registered. "
                f"Duplicate registration is not allowed."
            )
        self._capabilities[cid] = capability

        logger.info(
            "capability_registered",
            extra={
                "capability_id": cid,
                "action_type": capability.action_type,
            },
        )

    def get(self, capability_id: str) -> RecoveryCapability | None:
        """Retrieve a registered capability by ID, or None if not found."""
        return self._capabilities.get(capability_id)

    @property
    def registered_ids(self) -> frozenset[str]:
        """Return the set of all registered capability IDs."""
        return frozenset(self._capabilities.keys())
