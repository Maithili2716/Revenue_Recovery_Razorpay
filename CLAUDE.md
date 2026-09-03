# CLAUDE.md — Adaptive Revenue Recovery Agent

## 1. Project Identity

This repository implements a **merchant-specific Adaptive Revenue Recovery Agent** for the Razorpay buildathon Track 3.

The product detects revenue at risk, determines an appropriate recovery intervention, executes a bounded recovery workflow, independently verifies the financial outcome, records an audit trail, and learns from verified outcomes.

The system is intentionally designed as a modular, event-driven architecture.

### Core product loop

```text
Revenue Signal
    ↓
Revenue Risk Detection
    ↓
Recovery Case
    ↓
Adaptive Recovery Agent
    ↓
Capability Selection
    ↓
Policy / Guardrails
    ↓
Recovery Execution
    ↓
Independent Verification
    ↓
Verified Financial Outcome
    ├── Audit Trail
    └── Learning Signal
            ↓
      Merchant-specific adaptation
```

A separate evaluation layer compares the adaptive agent against a deterministic baseline on held-out data.

---

# 2. Architecture Ownership

The human developer owns the architecture.

Claude Code is an implementation assistant, not the architecture owner.

Claude Code MAY:
- identify bugs;
- suggest simpler implementations;
- identify inconsistencies;
- suggest architectural improvements;
- explain trade-offs.

Claude Code MUST NOT:
- silently change architectural boundaries;
- introduce new major frameworks without approval;
- create new subsystems merely because they may be useful later;
- merge unrelated responsibilities into an existing module;
- implement future blocks while working on the current block.

If an architectural change appears necessary, STOP and explain the proposed change before implementing it.

---

# 3. Product Architecture

The target architecture is:

```text
                         MERCHANT / RAZORPAY
                                │
                                ▼
                    ┌───────────────────────┐
                    │ REVENUE SIGNAL LAYER  │
                    │                       │
                    │ Payments              │
                    │ Subscriptions         │
                    │ Checkout-related      │
                    │ Invoices              │
                    │ Payment Links         │
                    │ Refunds / disputes    │
                    │ Other relevant events │
                    └───────────┬───────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │ REVENUE RISK DETECTOR │
                    │                       │
                    │ Normalize              │
                    │ Classify               │
                    │ Enrich context         │
                    │ Assess recoverability  │
                    │ Create Recovery Case   │
                    └───────────┬───────────┘
                                │
                                ▼
                 ┌──────────────────────────────┐
                 │ ADAPTIVE REVENUE RECOVERY   │
                 │            AGENT             │
                 │                              │
                 │ Context                     │
                 │ Diagnosis                   │
                 │ Prioritization              │
                 │ Candidate generation        │
                 │ Contextual bandit           │
                 │ Merchant-specific policy   │
                 └──────────────┬───────────────┘
                                │
                                ▼
                       CAPABILITY REGISTRY
                                │
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                 ▼
       Payment Recovery   Subscription       Receivables
          Capability        Recovery           Recovery
                            Capability           Agent
              │                 │                 │
              ▼                 ▼                 ▼
         Razorpay API       Razorpay API      Our workflow
         / MCP /             / MCP /
         Agent Studio*       Agent Studio*
              └─────────────────┼─────────────────┘
                                ▼
                       POLICY / GUARDRAILS
                                │
                         ┌──────┴──────┐
                         ▼             ▼
                      ALLOW          BLOCK
                         │             │
                         ▼             ▼
                      EXECUTE      STOP / ESCALATE
                         │
                         ▼
                   RAZORPAY / TOOLS
                         │
                         ▼
                 VERIFICATION ENGINE
                         │
                         ▼
                 VERIFIED OUTCOME
                    │          │
                    ▼          ▼
                AUDIT TRAIL  LEARNING
                                │
                                ▼
                       MERCHANT POLICY
                                │
                                └──────► NEXT CASE

* Agent Studio integration is only used when a programmatically accessible
  interface is actually verified. Never assume internal Agent Studio access.
```

---

# 4. Non-Negotiable Architecture Rules

## 4.1 Modular boundaries

Keep these responsibilities separate:

```text
Webhook Receiver
    ≠
Signal Normalizer
    ≠
Risk Detector
    ≠
Adaptive Agent
    ≠
Capability
    ≠
Policy Engine
    ≠
Execution
    ≠
Verification
    ≠
Learning
    ≠
Evaluation
    ≠
Audit
```

One module should have one primary responsibility.

## 4.2 No monolithic agent file

Do NOT create a giant `agent.py` containing:
- webhook handling;
- database access;
- Razorpay calls;
- risk detection;
- LLM calls;
- policy checks;
- recovery execution;
- verification;
- learning;
- evaluation.

The agent is an orchestration component, not the entire application.

## 4.3 Dependency direction

Prefer:

```text
API
 ↓
Application/service layer
 ↓
Domain logic
 ↓
Infrastructure adapters
```

Domain logic should not depend directly on FastAPI request objects.

Razorpay-specific details should remain behind integration/adaptor boundaries where practical.

## 4.4 Stable interfaces

Define explicit data contracts between major components.

Prefer typed models such as:
- `WebhookEvent`
- `RevenueSignal`
- `RecoveryCase`
- `RecoveryDecision`
- `PolicyDecision`
- `ExecutionResult`
- `VerificationResult`
- `VerifiedOutcome`
- `LearningSignal`

Avoid passing unstructured dictionaries throughout the entire application.

Raw external payloads may remain dictionaries at the integration boundary, but they should be converted into typed internal representations before entering domain logic.

---

# 5. Financial Safety Rules

This is a financial recovery system.

Safety is architectural, not cosmetic.

## 5.1 No unrestricted money actions

No agent, LLM, or model may directly execute a financial action without passing through the policy/guardrail layer.

Required pattern:

```text
Decision
   ↓
Policy / Guardrails
   ↓
Allowed?
 ├── NO → STOP / ESCALATE
 └── YES
       ↓
    Execute
```

## 5.2 LLMs do not authorize financial actions

An LLM may:
- interpret context;
- diagnose;
- generate candidate interventions;
- explain a decision;
- draft communication.

An LLM may NOT independently authorize:
- unrestricted retries;
- refunds;
- discounts;
- repeated customer contact;
- arbitrary payment actions;
- bypassing merchant policies.

Authorization belongs to deterministic policy/guardrail logic.

## 5.3 Explicit stopping rules

Every automated recovery workflow must have explicit stopping conditions.

Examples:
- maximum retry count;
- maximum contact count;
- maximum incentive;
- maximum monetary threshold;
- unknown financial state;
- successful recovery;
- permanent failure;
- human-approval threshold.

## 5.4 Unknown is not success

Never treat:
- timeout;
- missing response;
- malformed response;
- uncertain state;
- partial execution;
- unverified agent claim

as successful recovery.

Use explicit states such as:

```text
VERIFIED
FAILED
UNKNOWN
```

---

# 6. Verification Rules

Verification is independent from recovery execution.

Do NOT design:

```text
Capability.execute()
    → "success"
    → automatically treat as recovered revenue
```

Instead:

```text
Capability
    ↓
ExecutionResult
    ↓
VerificationEngine
    ↓
VerifiedOutcome
```

The verification engine should use authoritative Razorpay state wherever possible.

Only verified outcomes may become financial learning signals.

---

# 7. Learning Rules

The contextual bandit is part of the product, but it must learn from verified outcomes.

Required conceptual flow:

```text
Context
    +
Selected Action
    +
Verified Outcome
        ↓
    Reward / Learning Signal
        ↓
Merchant-specific policy update
```

Never update the learning policy solely from:
- LLM confidence;
- predicted recovery;
- an execution response saying "success";
- an unverified webhook;
- synthetic data presented as real financial recovery.

Synthetic data may be used for controlled evaluation, but it must be clearly identified as synthetic.

---

# 8. Evidence Discipline

Never claim an integration exists unless it has been:
1. documented by Razorpay and/or
2. actually tested by this project.

Maintain the distinction:

```text
PROVEN
DOCUMENTED / AVAILABLE
UNTESTED
SIMULATED
UNAVAILABLE
```

In particular, do not assume that Razorpay Agent Studio specialists are externally invokable.

If an Agent Studio programmatic interface is not verified, implement a capability adapter using the verified Razorpay API/MCP surface or our own workflow.

Never name an internal capability as "Razorpay Agent Studio agent" unless the project genuinely invokes it.

---

# 9. Razorpay Integration Rules

## 9.1 Test Mode first

Development and demonstration must use Razorpay Test Mode.

Never use live credentials during development.

## 9.2 Secrets

Never:
- print API secrets;
- commit secrets;
- place secrets in source code;
- include secrets in tests;
- send secrets to the user or another agent unnecessarily.

Use environment variables.

## 9.3 Webhooks

Webhook handling must eventually include:
- raw request body handling;
- signature verification;
- event identification;
- idempotency;
- structured logging;
- safe acknowledgement;
- separation from downstream business logic.

The webhook receiver must not contain recovery logic.

## 9.4 Raw payload preservation

When discovering a new Razorpay event schema, preserve the real payload before designing the normalizer around assumptions.

Real captured payloads may become sanitized test fixtures.

Do not invent Razorpay payload structures when an actual/documented structure is available.

---

# 10. Two-Mode Development Rule

The system must support two conceptually equivalent paths:

## Real mode

```text
Razorpay Test Mode
    ↓
Real webhook
    ↓
Webhook Receiver
    ↓
Same internal pipeline
```

## Test mode

```text
Captured Razorpay fixture
    ↓
HTTP test request
    ↓
Same Webhook Receiver
    ↓
Same internal pipeline
```

Do not create a separate fake business pipeline for tests.

Fixtures should exercise the same application boundary as real events.

---

# 11. Code Quality Standards

## 11.1 Simplicity

Prefer the simplest implementation that satisfies the current requirement.

Do not add:
- unnecessary abstractions;
- speculative frameworks;
- premature distributed systems;
- complex dependency injection;
- unnecessary queues;
- unnecessary microservices.

Build the architecture so it CAN evolve without building every future feature now.

## 11.2 Readability

Code should be understandable by another engineer opening the repository for the first time.

Prefer:
- descriptive names;
- small functions;
- explicit control flow;
- typed inputs/outputs;
- short focused modules.

Avoid clever code.

## 11.3 Reusability

Reusable logic belongs in reusable modules.

Do not duplicate:
- Razorpay API calls;
- signature verification;
- event parsing;
- policy checks;
- verification logic;
- reward calculations.

## 11.4 Configuration

Environment-specific configuration belongs in configuration/environment files, not scattered throughout source code.

Do not hard-code:
- API credentials;
- webhook secrets;
- URLs;
- monetary thresholds;
- merchant IDs;
- model credentials.

## 11.5 Comments

Comments should explain:
- why a non-obvious decision exists;
- important external API constraints;
- safety assumptions;
- temporary buildathon compromises.

Do not comment obvious code.

---

# 12. File Organization

Prefer a structure similar to:

```text
app/
├── api/
├── signals/
├── risk/
├── recovery/
│   └── capabilities/
├── policy/
├── verification/
├── learning/
├── audit/
├── evaluation/
├── integrations/
│   └── razorpay/
├── db/
├── config/
└── main.py

tests/
├── fixtures/
│   └── razorpay/
├── api/
├── signals/
├── risk/
├── recovery/
├── policy/
├── verification/
├── learning/
└── evaluation/
```

Do not create every directory immediately.

Create a directory when its corresponding block is actually being implemented.

---

# 13. Testing Rules

Every implementation block must include appropriate tests.

At minimum, test:
- happy path;
- invalid input;
- failure path;
- boundary conditions;
- duplicate/idempotent behavior where applicable.

For external integrations:
- use fixtures/mocks for deterministic automated tests;
- perform real Test Mode integration checks separately;
- do not fake an integration test and call it an end-to-end test.

Tests must test behavior, not implementation details.

---

# 14. Error Handling

Errors must be explicit.

Do not:
- swallow exceptions silently;
- return success after partial failure;
- treat unknown state as success;
- use broad exception handlers without meaningful handling/logging.

External failures should be converted into controlled application states.

Examples:

```text
EXECUTION_FAILED
VERIFICATION_FAILED
VERIFICATION_UNKNOWN
POLICY_BLOCKED
INVALID_EVENT
DUPLICATE_EVENT
```

---

# 15. Observability and Auditability

Every meaningful recovery workflow should eventually be traceable.

Audit events should capture, where appropriate:

```text
timestamp
case_id
merchant_id
event_id
actor/component
decision
reason
policy result
action
execution result
verification result
amount at risk
amount recovered
learning update
```

Never put secrets or sensitive credentials into audit logs.

---

# 16. Vertical Slice Development Rule

The project is developed in vertical slices.

A vertical slice must travel through enough of the system to demonstrate real value.

Preferred progression:

```text
Slice 1
Webhook
→ Signal
→ Risk Case

Slice 2
Risk Case
→ Recovery Decision
→ Payment Link
→ Verification
→ Audit

Slice 3
Add second capability

Slice 4
Capability Registry

Slice 5
Adaptive decision layer

Slice 6
Guardrails / escalation

Slice 7
Contextual bandit

Slice 8
Merchant-specific adaptation

Slice 9
Batch evaluation

Slice 10
Dashboard / demo hardening
```

Each completed slice should leave the previous workflow working.

---

# 17. Current Block Discipline

When Claude Code receives a block specification:

1. Read this file.
2. Read the block specification.
3. Inspect the existing repository before changing files.
4. State the files it expects to create/change.
5. Implement ONLY the requested block.
6. Do not implement future architecture.
7. Run the relevant tests.
8. Report changed files.
9. Report test results.
10. Report any assumptions or unresolved external dependencies.

If implementation requires changing an architectural rule in this file, STOP and ask for approval.

---

# 18. Definition of Done

A block is not complete because code was generated.

A block is complete when:

- the intended behavior is implemented;
- module boundaries are respected;
- tests exist;
- tests pass;
- errors are handled;
- no secrets are exposed;
- the implementation is understandable;
- the actual workflow has been manually inspected when applicable;
- external integration behavior has been verified when applicable;
- the user has reviewed and accepted the result.

---

# 19. Claude Code Must Never

Never:

- invent Razorpay API behavior;
- invent webhook payloads when real/documented data can be obtained;
- claim Agent Studio integration without evidence;
- bypass policy/guardrails;
- let an LLM directly authorize money movement;
- treat an unverified outcome as recovered revenue;
- modify unrelated modules;
- create a monolithic agent;
- introduce a framework without justification;
- silently rewrite architecture;
- build future blocks without instruction;
- remove tests to make a failing implementation pass;
- weaken validation to hide an integration problem;
- commit secrets;
- fabricate evaluation metrics;
- present simulated recovery as real recovered money.

---

# 20. Working Philosophy

Build **small, real, observable increments**.

Prefer:

```text
Real integration
+
small scope
+
strong boundaries
+
visible workflow
+
tests
```

over:

```text
large theoretical architecture
+
many unfinished components
```

The goal is not maximum code.

The goal is a reliable, demonstrable system that proves:

> Detect revenue at risk → determine the right intervention → execute a bounded recovery workflow → verify actual financial outcome → measure recovery → learn from verified outcomes.

