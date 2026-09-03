# Razorpay Revenue Recovery Agent — Continuation Brief

**Read this together with the repository's `CLAUDE.md`.**

`CLAUDE.md` is the project constitution. This document is the current implementation/state handoff for a fresh coding agent. Do not rediscover the project from scratch. Inspect only the relevant files needed for the current block.

## 1. Product

We are building a **merchant-specific adaptive revenue recovery agent** for Razorpay's AI Revenue Recovery hackathon track.

The product must:
1. Detect revenue at risk.
2. Diagnose the failure/context.
3. Determine the best recovery intervention for that merchant/context.
4. Execute the intervention through a bounded capability.
5. Verify whether money was actually recovered.
6. Record an audit trail.
7. Feed verified outcomes back into a merchant-specific contextual bandit so the agent improves future choices.
8. Demonstrate measured money recovered across a batch.

**Merchant-specific contextual bandit is a CORE FEATURE, not optional polish.**

The LLM is a bounded diagnosis/enrichment component. It is NOT the financial authority and must never directly execute or authorize financial actions.

## 2. Hackathon bar

The central claim is:

> For each merchant/context, the agent learns which recovery intervention works best, executes it within policy, verifies the actual financial outcome, and learns from the verified result.

Submission priorities:
- working repository that starts reliably;
- convincing 5-minute demo;
- measured recovered money;
- compliant escalation/stopping rules;
- audit trail;
- merchant-specific adaptation.

Optimize for reliability and observable proof.

## 3. Canonical architecture

```text
REAL RAZORPAY EVENTS
        |
        v
REVENUE SIGNAL LAYER
  Payments / Subscriptions / Checkout / Invoices / etc.
        |
        v
REVENUE RISK DETECTOR
  normalize -> classify -> enrich -> RecoveryCase
        |
        v
ADAPTIVE REVENUE RECOVERY AGENT
  context
  diagnosis
  candidate generation
  merchant-specific contextual bandit
        |
        v
POLICY / GUARDRAILS
  ALLOW / BLOCK
        |
        v
CAPABILITY REGISTRY
  capability_id -> executable capability
        |
        v
CAPABILITY EXECUTION
        |
        v
VERIFICATION ENGINE
  independent source of truth
        |
        v
VERIFIED OUTCOME
    |                |
    v                v
AUDIT TRAIL        LEARNING
                       |
                       v
               MERCHANT POLICY / BANDIT STATE
```

LLM is inside diagnosis:

```text
Agent Context
    |
    +--> LLM diagnosis (optional bounded reasoning)
    |
    +--> deterministic fallback
    |
    v
Diagnosis
    v
Candidate Actions
    v
Merchant-specific Contextual Bandit
```

The LLM must never directly call Razorpay APIs or decide that money was recovered.

## 4. Responsibility boundaries

**Agent:** What should we do?

**Policy:** Are we allowed to do it?

**Capability:** How do we execute it?

**Verifier:** Did it actually work financially?

**Learning:** What did this verified outcome teach us about this merchant/context?

Keep these concepts separate even if implementations are small.

## 5. Current implementation status

### DONE — Webhook ingestion

Implemented:
- FastAPI Razorpay webhook endpoint
- raw-body handling
- HMAC-SHA256 signature verification
- event extraction
- idempotency
- structured logging
- real Razorpay Test Mode webhook observed

Relevant areas:
```text
backend/app/api/razorpay_webhooks.py
backend/app/integrations/razorpay/signature.py
backend/app/integrations/razorpay/idempotency.py
backend/app/integrations/razorpay/events.py
```

### DONE — Revenue signal normalization

`payment.failed` -> `RevenueSignal`.

Fields include:
- signal_id
- merchant_id
- customer_id
- signal_type
- status
- amount_minor
- currency
- provider
- provider_entity_id
- provider_event_id
- reason
- failure_source
- failure_step
- occurred_at
- metadata

Merchant identity comes from Razorpay `account_id`; never fabricate merchant IDs.

### DONE — Recovery risk detection

`RecoveryCase` exists with:
- case_id
- signal_id
- merchant_id
- customer_id
- amount_at_risk_minor
- currency
- risk_status
- recoverability
- urgency
- reason_codes
- created_at

Risk logic is intentionally conservative.

### DONE — Agent skeleton

Existing concepts:
```text
AgentContext
Diagnosis
CandidateAction
AgentDecision
```

Existing areas:
```text
backend/app/recovery/agent/context.py
backend/app/recovery/agent/diagnosis.py
backend/app/recovery/agent/candidates.py
backend/app/recovery/agent/bandit.py
backend/app/recovery/agent/service.py
backend/app/recovery/agent/models.py
```

Current flow:
```text
RevenueSignal
 -> RecoveryCase
 -> AgentContext
 -> Diagnosis
 -> Candidate Actions
 -> Contextual Bandit
 -> AgentDecision
```

### DONE / IN PROGRESS — LLM diagnosis

Gemini was initially integrated but had unacceptable availability/latency.

The diagnosis provider was replaced with Grok.

Configuration uses:
```text
GROK_API_KEY
```

The LLM provider remains behind the diagnosis provider abstraction.

The Grok account currently reports exhausted credits/monthly spending limit. This is an external account limitation, not an architectural failure.

**Do not redesign the agent because Grok is unavailable.**

Deterministic diagnosis fallback must remain.

Diagnosis source distinguishes:
```text
llm
deterministic_fallback
```

### DONE — Webhook responsiveness

The webhook previously waited synchronously for the LLM. This was corrected.

Current intended flow:
```text
HTTP webhook
 -> verify
 -> parse
 -> idempotency
 -> dispatch background recovery processing
 -> HTTP 200 immediately

background:
 -> normalize
 -> RecoveryCase
 -> AgentContext
 -> diagnosis
 -> candidates
 -> bandit
 -> decision
```

Never reintroduce synchronous LLM/capability execution into the HTTP request.

The webhook must remain fast even if an LLM or Razorpay API is slow.

## 6. Known limitation

The bandit can currently show:
```text
candidate_count=1
strategy=deterministic_priority
```

This is NOT the final desired behavior.

The bandit boundary already exists, but we must create genuinely competing recovery capabilities/candidates and implement merchant-specific adaptive learning.

Do not remove the bandit because its current MVP strategy is simple.

Target evolution:
```text
deterministic priority
        ->
merchant/context-specific learned action selection
```

using verified outcomes as rewards.

## 7. LLM behavior

Grok is only diagnosis.

If it:
- times out;
- returns 4xx/5xx;
- returns malformed output;
- is unavailable;
- has no credits;

then:
```text
LLM failure
 -> deterministic diagnosis
 -> agent continues
```

The core adaptive system must work without live LLM availability.

Do not spend major time on the LLM unless it blocks a required demo.

## 8. IMMEDIATE NEXT BLOCK — Capability Execution

Implement a small but real execution layer while preserving the architecture.

Target:
```text
AgentDecision
    |
    v
Policy / Guardrails
    |
    v
Capability Registry
    |
    v
payment_link_recovery
    |
    v
Razorpay Payment Links API
    |
    v
ExecutionResult
```

### Policy

Minimum checks:
- case is AT_RISK;
- amount_at_risk_minor > 0;
- merchant_id exists;
- currency exists;
- capability is registered;
- action is eligible;
- stopping-rule conditions are not violated.

Preserve a stopping-rule boundary for future:
- max attempts;
- cooldown;
- max exposure/recovery amount;
- already-recovered cases;
- merchant-specific restrictions.

Do not build a huge policy engine.

### Capability interface

Use a generic contract conceptually:
```text
RecoveryCapability
    capability_id
    action_type
    execute(execution_context) -> ExecutionResult
```

Do not hard-code payment-link behavior into a generic executor.

### Capability registry

Simple explicit registry:
```text
payment_link_recovery
    -> PaymentLinkRecoveryCapability
```

It must later allow:
```text
payment_retry
payment_link_recovery
```

Do NOT build a plugin marketplace or dynamic module loader.

### ExecutionResult

Distinguish:
```text
executed
failed
blocked
```

A successful Payment Link API call means:
```text
action executed
```

It does NOT mean:
```text
money recovered
```

Include enough for future verification/audit:
- execution_id
- case_id
- decision_id if available
- capability_id
- action_type
- status
- provider
- provider_reference
- executed_at
- metadata

### Payment-link capability

Reuse existing Razorpay configuration/authentication. Do not duplicate credentials.

Use recovery-case amount/currency.

Call Razorpay Payment Links API.

Capture the Payment Link ID/reference.

Do not claim recovered revenue.

### Webhook constraint

Capability execution remains in the background workflow, never before webhook acknowledgement.

## 9. AFTER CAPABILITY EXECUTION — remaining blocks

Do not skip these because implementation is small.

### Block 6 — Verification

Independent verification engine:
```text
ExecutionResult
      |
      v
Verification
      |
      +--> payment actually succeeded?
      +--> amount actually recovered?
      +--> provider reference/status?
      |
      v
VerifiedOutcome
```

Verifier is the source of truth.

Important:
```text
Unknown != Success
Execution != Recovery
LLM claim != Financial fact
```

### Block 7 — Audit trail

Record/trace:
```text
event
signal
case
context
diagnosis
candidate actions
bandit decision
policy decision
execution
verification
verified recovery amount
learning update
```

Trace with:
```text
case_id
decision_id
execution_id
```

Demo must answer:
- Why did the agent choose this?
- What did it execute?
- Did it work?
- How much was actually recovered?
- What did it learn?

### Block 8 — Merchant-specific contextual bandit learning

This is a CORE FEATURE.

Do not replace with global success rates.

Concept:
```text
Merchant A + context
    retry:        70%
    payment_link: 30%

Merchant B + context
    retry:        20%
    payment_link: 80%
```

The implementation may use simple merchant/context buckets and action statistics.

Required properties:
1. action choice depends on merchant/context;
2. verified outcomes update estimates;
3. exploration exists;
4. unverified/unknown outcomes do not become positive rewards;
5. learning state persists;
6. decision is inspectable for demo.

Reward must be based on verified financial outcome, not LLM confidence or execution success.

### Required competing capabilities

At least:
```text
payment_retry
payment_link_recovery
```

Potential later:
```text
escalation
```

Do not fake Razorpay API behavior.

If a real operation cannot safely be demonstrated in Test Mode, use a clearly labeled test/simulation adapter rather than falsely claiming a real execution.

Distinguish:
```text
REAL
TEST
SIMULATED
UNAVAILABLE
```

### Block 9 — Batch evaluation

Held-out dataset:
```text
HELD-OUT DATASET
       |
       +--> baseline
       |
       +--> adaptive agent
                 |
                 v
             execute
                 |
                 v
             verify
                 |
                 v
        evaluation metrics
```

Show:
- cases
- revenue at risk
- baseline recovered
- adaptive recovered
- incremental recovered
- recovery rate
- failures
- escalations
- policy violations
- unknown/unverified outcomes

Synthetic/test data must be labeled as such. Do not manufacture real-world claims.

## 10. Demo target

5-minute story:

### 0:00–0:30
Problem + product.

### 0:30–1:15
Architecture.

### 1:15–2:00
Real Razorpay Test Mode `payment.failed`:
```text
failure
-> amount at risk
-> RecoveryCase
```

### 2:00–3:15
Merchant-specific adaptation:
```text
Merchant A:
retry historically better
-> bandit chooses retry

Merchant B:
payment link historically better
-> bandit chooses payment link
```

Show verified outcome updating learned state.

### 3:15–4:15
Actual capability execution + independent verification.

### 4:15–5:00
Batch results:
```text
Cases
Revenue at risk
Baseline recovery
Adaptive recovery
Incremental recovered ₹
Recovery rate
Policy violations
Audit trail
```

Central narrative:
```text
detect -> diagnose -> adapt per merchant -> act -> verify -> learn
```

## 11. Reliability invariants

### Webhook fast path
Never wait for:
- LLM
- downstream Razorpay API
- verification
- learning

before acknowledging webhook.

### LLM failure tolerance
LLM unavailable -> deterministic diagnosis.

### Capability failure tolerance
Execution failure -> structured failure result, not process crash.

### Verification conservatism
Unknown -> unknown.

### Financial safety
No LLM direct financial authority. Policy precedes execution.

### Idempotency
Repeated provider events must not cause duplicate processing/actions.

### Auditability
Every decision/execution/outcome must be traceable.

## 12. Coding-agent operating rules

Do NOT:
- redesign architecture;
- collapse layers for convenience;
- remove contextual bandit;
- replace merchant-specific learning with global averages;
- make LLM responsible for financial decisions;
- put LLM calls in webhook fast path;
- invent Razorpay API behavior;
- claim execution means recovery;
- add Redis/Celery/Kafka unless explicitly requested;
- build speculative infrastructure;
- rewrite unrelated modules;
- repeatedly run the entire test suite;
- create abstractions without a current use;
- proceed to the next block without reporting the current block.

For every block:
1. inspect only relevant existing files;
2. identify smallest necessary changes;
3. implement;
4. run focused tests/checks;
5. perform manual observable check when useful;
6. report changed files and results;
7. stop.

The human/architect decides when to move to the next block.

## 13. Strong enough means

We do NOT need enterprise-scale infrastructure.

We DO need strong boundaries:
```text
signal
case
agent
policy
capability
execution
verification
audit
learning
evaluation
```

A small implementation behind a good boundary is preferable to a huge fragile implementation.

Goal:
> small enough to finish, strong enough to demonstrate, modular enough to survive the demo.

## 14. Execution order

```text
CURRENT
Webhook + signal + case + agent + bandit boundary
        |
        v
NOW
Capability execution
        |
        v
Verification
        |
        v
Audit trail
        |
        v
Merchant-specific bandit learning
        |
        v
Second competing capability
        |
        v
Batch evaluation
        |
        v
Dashboard/demo
        |
        v
Clean repo verification
        |
        v
5-minute submission video
```

Do not skip verification, audit, or learning.

## 15. Final invariant

```text
LLM:
    reasons/diagnoses

BANDIT:
    chooses among eligible interventions
    using merchant/context

POLICY:
    authorizes or blocks

CAPABILITY:
    executes selected intervention

VERIFIER:
    determines whether money was actually recovered

LEARNING:
    updates merchant-specific action preference
    using VERIFIED outcomes

AUDIT:
    explains what happened end-to-end
```

This is the system we are building.
