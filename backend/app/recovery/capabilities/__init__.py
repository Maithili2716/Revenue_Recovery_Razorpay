"""Recovery capabilities package.

Provides the capability execution layer:

    AgentDecision → Policy → Registry → Capability → ExecutionResult

Public API:

    build_capability_executor()  — creates a fully wired executor
                                   with all registered capabilities.
"""

from __future__ import annotations

from app.config import settings
from app.integrations.razorpay.client import RazorpayPaymentLinkClient
from app.policy.engine import PolicyEngine
from app.recovery.capabilities.executor import CapabilityExecutor
from app.recovery.capabilities.payment_link import PaymentLinkRecoveryCapability
from app.recovery.capabilities.payment_link_reminder import PaymentLinkReminderCapability
from app.recovery.capabilities.registry import CapabilityRegistry


def build_capability_executor() -> CapabilityExecutor:
    """Create a fully wired CapabilityExecutor with all registered capabilities.

    This is the single factory function that the rest of the application
    uses to obtain an executor.  It wires:
    - Razorpay API client (from settings)
    - PaymentLinkRecoveryCapability
    - PaymentLinkReminderCapability
    - CapabilityRegistry
    - PolicyEngine

    Never exposes API credentials beyond this factory.
    """
    # 1. Build the Razorpay client using existing config.
    razorpay_client = RazorpayPaymentLinkClient(
        key_id=settings.razorpay_key_id,
        key_secret=settings.razorpay_key_secret,
    )

    # 2. Create and register capabilities.
    registry = CapabilityRegistry()
    registry.register(PaymentLinkRecoveryCapability(razorpay_client))
    registry.register(PaymentLinkReminderCapability(razorpay_client))

    # 3. Create the policy engine with the registered capability IDs.
    policy_engine = PolicyEngine(
        registered_capability_ids=registry.registered_ids,
    )

    # 4. Assemble the executor.
    return CapabilityExecutor(
        registry=registry,
        policy_engine=policy_engine,
    )

