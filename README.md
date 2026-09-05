# RecoveryLab

### Adaptive revenue recovery from signal to verified money

Revenue doesn't disappear in one clean event.

A checkout fails. A subscription payment doesn't go through. An invoice becomes overdue. A customer abandons a payment halfway through. Each of these is a different kind of revenue risk, and treating all of them with the same retry or recovery action is not necessarily the best approach.

**RecoveryLab is an adaptive revenue recovery system that closes that loop:**

> **Detect → Diagnose → Decide → Authorize → Execute → Verify → Learn**

For the MVP, we focused deeply on one of those revenue-risk paths: **failed checkout/payment events**.

Instead of simply retrying a failed payment or immediately creating another payment request, RecoveryLab turns the failure into a recovery case, diagnoses the situation, selects an intervention using merchant/contextual information, passes the decision through a policy boundary, executes the recovery action, independently verifies whether money was actually recovered, and feeds only verified outcomes back into learning.

---

# What the agent does

In brief:

**RecoveryLab receives a payment failure → determines whether it represents meaningful revenue at risk → diagnoses the failure → chooses the most appropriate recovery capability for that merchant/context → gets the action policy-authorized → executes it → verifies the financial outcome → learns from the verified result.**

The important part is that the AI/decision layer **does not get to execute arbitrary actions**.

It decides.

A separate execution layer executes.

A separate verification layer determines what actually happened.

And the learning system learns from what actually happened, not what the agent expected to happen.

---

# The problem as we understood it

Revenue loss can happen through several different paths:

* Checkout/payment failure
* Checkout abandonment
* Subscription payment failure
* Overdue invoices
* Expired payment attempts
* Failed mandates and retries
* Other payment degradation events

Our initial idea was to build a broader revenue recovery system covering:

```text
Checkout abandonment
Subscription failure
Invoice failure / overdue receivables
Payment expiration
```

But we deliberately scoped this down for the MVP.

Instead of building four shallow integrations, we wanted to build **one complete recovery loop** and prove that the architecture works end-to-end.

So the MVP focuses on:

```text
Payment / checkout failure
        ↓
Revenue risk detection
        ↓
Diagnosis
        ↓
Adaptive recovery decision
        ↓
Policy
        ↓
Recovery execution
        ↓
Verification
        ↓
Learning
```

The architecture is intentionally designed so that additional revenue-risk signals can be added later without rebuilding the recovery engine.

---

# Architecture

```text
                         RAZORPAY
                            │
                            │ payment.failed
                            ▼
                  ┌─────────────────────┐
                  │   Signal Ingestion  │
                  └──────────┬──────────┘
                             │
                             ▼
                  ┌─────────────────────┐
                  │    Risk Engine      │
                  │                     │
                  │ Is this actually    │
                  │ revenue at risk?    │
                  └──────────┬──────────┘
                             │
                             ▼
                  ┌─────────────────────┐
                  │   Recovery Case     │
                  └──────────┬──────────┘
                             │
                             ▼
                  ┌─────────────────────┐
                  │      Diagnosis      │
                  │                     │
                  │ reason              │
                  │ stage               │
                  │ confidence          │
                  └──────────┬──────────┘
                             │
                             ▼
             ┌────────────────────────────────┐
             │     CONTEXTUAL BANDIT          │
             │                                │
             │ merchant context               │
             │ historical outcomes            │
             │ failure context                │
             │ previous strategy performance  │
             │ other learned signals           │
             └────────────────┬───────────────┘
                              │
                              ▼
                    Strategy / Capability
                         selection
                              │
                              ▼
                  ┌─────────────────────┐
                  │   Policy Engine     │
                  │                     │
                  │ Is this action      │
                  │ allowed?            │
                  └──────────┬──────────┘
                             │
                         ALLOW
                             │
                             ▼
                  ┌─────────────────────┐
                  │  Execution Layer    │
                  │                     │
                  │ Capability Registry │
                  │ → Razorpay API      │
                  └──────────┬──────────┘
                             │
                             ▼
                  ┌─────────────────────┐
                  │     Verifier        │
                  │                     │
                  │ Did money actually  │
                  │ move?               │
                  └──────────┬──────────┘
                             │
                             ▼
                  ┌─────────────────────┐
                  │  Learning Layer     │
                  │                     │
                  │ verified outcome    │
                  │ → strategy update   │
                  └─────────────────────┘

                  ┌─────────────────────┐
                  │    Audit Trail      │
                  │                     │
                  │ every important     │
                  │ decision + outcome  │
                  └─────────────────────┘
```

---

# Why the architecture is built this way

## 1. Signal ingestion is separate from recovery

A payment failure is only a **signal**.

It shouldn't automatically mean:

> "Create a Payment Link."

The signal first enters the system and is normalized into a form that the recovery engine can understand.

This is important because tomorrow the input might be:

```text
payment.failed
checkout.abandoned
subscription.payment_failed
invoice.overdue
```

The recovery engine shouldn't need to understand every provider-specific event format.

---

## 2. The Risk Engine decides whether there is actually revenue at risk

Not every event deserves the same recovery treatment.

The risk layer turns an incoming signal into a recovery case only when it represents meaningful revenue exposure.

This gives us a separation between:

```text
Something happened
```

and

```text
We need to recover money because of what happened
```

---

# 3. Diagnosis comes before intervention

Once a recovery case exists, the system diagnoses it.

The diagnosis captures things such as:

* failure category
* primary reason
* failure stage
* confidence
* diagnosis source

The point is simple:

**different failure contexts can require different interventions.**

The agent shouldn't choose an action before understanding the case.

---

# 4. The Contextual Bandit chooses the intervention

This is the adaptive part of RecoveryLab.

The decision layer doesn't simply contain:

```text
if payment_failed:
    create_payment_link()
```

Instead, the contextual bandit considers the context available to it, including:

* merchant identity/context
* historical strategy outcomes
* failure context
* previously learned strategy effectiveness
* the current recovery case

It then selects a recovery capability.

The important architectural decision is that the **strategy decision and strategy execution are separate**.

The bandit chooses.

It does not execute.

---

# 5. Capability Registry

The decision layer selects from registered capabilities rather than generating arbitrary actions.

For the MVP, the recovery strategy space includes concepts such as:

```text
payment_link_recovery
payment_link_reminder
invoice_recovery
```

The live Razorpay recovery path demonstrated in the MVP executes **Payment Link Recovery**. `invoice_recovery` is currently represented in the isolated evaluation environment rather than being presented as a live production capability.

This distinction is intentional.

We wanted the evaluation to test whether adaptive strategy selection works across different recovery strategies without pretending that every simulated capability is already connected to production infrastructure.

---

# 6. The agent does NOT execute the decision

This is one of the most important design decisions in RecoveryLab.

The decision layer produces something equivalent to:

```text
Selected capability:
payment_link_recovery
```

That decision then moves into a completely separate execution layer.

The contextual bandit/LLM does **not** get direct access to Razorpay APIs.

The execution layer resolves the selected capability through the capability registry.

This gives us:

```text
Decision
    ↓
Policy
    ↓
Executor
    ↓
Capability
    ↓
Provider
```

instead of:

```text
LLM
 ↓
do whatever seems appropriate
 ↓
Razorpay
```

That boundary is deliberate because financial actions need bounded execution.

---

# 7. Policy Engine

Before an action is executed, it passes through the policy layer.

The policy engine determines whether the proposed action is allowed.

This gives us another hard boundary:

```text
Agent says:
"Use payment_link_recovery"

          ↓

Policy says:
"ALLOW"

          ↓

Executor:
"Execute payment_link_recovery"
```

The agent cannot bypass this boundary.

---

# 8. Execution

Only after policy authorization does the executor call the actual capability.

For the live MVP:

```text
payment_link_recovery
        ↓
Razorpay Payment Links API
        ↓
Payment Link created
```

The generated Payment Link is then surfaced in the RecoveryLab UI so the recovery can be completed through Razorpay Test Mode.

---

# 9. Verification is the source of truth

Creating a Payment Link is **not** revenue recovered.

This distinction is critical.

RecoveryLab doesn't say:

> Payment Link created → ₹100 recovered

Instead:

```text
Payment Link created
        ↓
Customer action
        ↓
Razorpay payment state
        ↓
Verification
        ↓
actual recovered amount
```

Only when the verifier has evidence that money was actually recovered do we count it as recovered revenue.

For example:

```text
Execution:
SUCCESS

Verification:
RECOVERED

Amount:
₹100
```

is a real recovery outcome.

Whereas:

```text
Execution:
SUCCESS

Verification:
PENDING
```

means:

> We created the recovery action, but we don't know yet whether it recovered money.

---

# 10. Learning only consumes verified outcomes

The contextual bandit learns from outcomes.

But it shouldn't learn from assumptions.

For example, this should **not** happen:

```text
Payment Link creation failed because
Razorpay API rejected the request

        ↓

Bandit learns:
"Payment Link strategy is bad"
```

That is an execution/provider failure, not a strategy outcome.

The system distinguishes:

```text
Execution failure
        ≠
Recovery strategy failure
```

Only legitimate verified recovery outcomes are allowed to influence strategy learning.

This prevents infrastructure failures from contaminating the adaptive decision system.

---

# Evaluation

The evaluation system is deliberately separate from the live recovery agent.

It does **not** execute real Razorpay recovery actions.

Instead, it evaluates the decision-making system on a deterministic synthetic held-out batch.

Current evaluation:

```text
50 synthetic held-out cases
5 merchants
multiple recovery contexts
```

The same cases are presented to:

### Baseline

A fixed strategy:

```text
payment_link_recovery
```

### Adaptive

The contextual bandit chooses from the available evaluation strategies based on the case context and learned merchant-specific information.

The result from our current evaluation:

| Metric           |  Baseline |      Adaptive |
| ---------------- | --------: | ------------: |
| Cases            |        50 |            50 |
| Amount at risk   | ₹1,38,250 |     ₹1,38,250 |
| Amount recovered |   ₹81,400 | **₹1,06,500** |
| Recovery rate    |    58.88% |    **77.03%** |
| Recovered cases  |        32 |        **38** |

### Improvement

**₹25,100 additional simulated recovery**

**30.84% relative improvement**

The evaluation is held out from the learning process so that the adaptive strategy cannot simply learn from the outcomes it is being tested on.

It also does not invoke live Razorpay capabilities.

> **"Does adaptive intervention selection recover more money than a fixed strategy on cases it hasn't learned from?"**

---

# What the live demo shows

The live RecoveryLab demo follows a real Test Mode recovery flow:

```text
1. Create test payment
          ↓
2. Payment fails
          ↓
3. Razorpay sends payment.failed
          ↓
4. RecoveryLab receives the signal
          ↓
5. Revenue risk is identified
          ↓
6. Recovery case is created
          ↓
7. Diagnosis
          ↓
8. Adaptive strategy decision
          ↓
9. Policy authorization
          ↓
10. Payment Link execution
          ↓
11. Customer completes recovery payment
          ↓
12. Razorpay confirms payment
          ↓
13. RecoveryLab verifies recovered amount
          ↓
14. Learning is updated
          ↓
15. Audit trail records the complete flow
```

The UI is not simulating these stages with timers.

It polls the backend demo snapshot and renders the actual state returned by the recovery system.

---

# Demo setup

## Requirements

You will need:

* Python 3.11+
* Node.js / npm
* Razorpay Test Mode account
* Razorpay API credentials
* `ngrok`
* Git

---

## 1. Clone the repository

```bash
git clone <YOUR_REPOSITORY_URL>
cd razorpay-revenue-recovery
```

---

# 2. Backend setup

```bash
cd backend

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

---

# 3. Configure environment variables

Create:

```text
backend/.env
```

Example:

```env
RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxx
RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxx
```

Use your own Razorpay **Test Mode** credentials.

Do not commit `.env`.

The repository should contain an `.env.example` with placeholders rather than real credentials.

---

# 4. Start the backend

From `backend/`:

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

The API should now be available at:

```text
http://127.0.0.1:8000
```

---

# 5. Start ngrok

Razorpay needs a publicly reachable webhook URL.

In another terminal:

```bash
ngrok http 8000
```

ngrok will give you a public URL similar to:

```text
https://xxxx-xxxx.ngrok-free.app
```

---

# 6. Configure the Razorpay webhook

In the Razorpay Test Mode dashboard, configure the webhook endpoint using your ngrok URL plus the application's webhook path.

For example:

```text
https://YOUR-NGROK-DOMAIN/<WEBHOOK_PATH>
```

Use the exact webhook route implemented by the backend rather than copying this placeholder literally.

Configure the events required by the demo, including the payment failure event and the successful payment event used by the verification flow.

The important events for the demonstrated recovery loop are:

```text
payment.failed
payment_link.paid
```

If your current Razorpay configuration exposes additional payment-link events that the verifier consumes, configure those according to the implementation.

---

# 7. Start the frontend

Open another terminal:

```bash
cd frontend
npm install
npm run dev
```

Vite will provide the local frontend URL, normally:

```text
http://localhost:5173
```

Open it in your browser.

---

# Running the demo

## Step 1 — Run Test Recovery

Click:

**Run Test Recovery**

RecoveryLab creates the test payment/order flow.

---

## Step 2 — Trigger the failure

Use Razorpay Test Mode to intentionally fail the payment.

The important event is:

```text
payment.failed
```

Razorpay sends the event through the ngrok webhook to the backend.

---

## Step 3 — Watch RecoveryLab

The UI should move through:

```text
Signal
  ↓
Revenue Risk
  ↓
Diagnosis
  ↓
Adaptive Decision
  ↓
Policy
  ↓
Recovery Action
```

At this point, the system should have generated the recovery Payment Link.

---

## Step 4 — Open the Payment Link

Use the generated **Open Payment Link** action.

Complete the recovery payment in Razorpay Test Mode.

---

## Step 5 — Verification

The verifier checks the provider state rather than assuming that the execution succeeded.

The system should eventually show:

```text
Verification
RECOVERED

Amount recovered:
₹100
```

or whatever amount was used for the demo.

---

## Step 6 — Learning

Only after the recovery outcome is verified does the learning layer receive the outcome.

The audit trail records the recovery lifecycle.

---

# Evaluation

The evaluation can be run independently from the live recovery system.

Backend:

```bash
curl -X POST http://127.0.0.1:8000/evaluation/run
```

Or use the **Evaluation** page in the frontend.

The evaluation runs the synthetic held-out batch and reports:

* cases evaluated
* total revenue at risk
* baseline recovered amount
* adaptive recovered amount
* baseline recovery rate
* adaptive recovery rate
* absolute improvement
* relative improvement
* strategy-level performance
* escalation count
* policy safety violations

The evaluation does not execute real Razorpay recovery actions.

---

# Auditability

RecoveryLab records the important decisions and outcomes throughout the recovery lifecycle.

A typical case can be traced through:

```text
CASE_CREATED
      ↓
DIAGNOSIS_CREATED
      ↓
DECISION_CREATED
      ↓
POLICY_DECISION
      ↓
CAPABILITY_EXECUTED
      ↓
VERIFICATION_COMPLETED
      ↓
LEARNING_UPDATED
```

This makes it possible to answer:

* Why was this case considered risky?
* What did the system diagnose?
* Which strategy did it choose?
* Why was the action allowed?
* What capability was executed?
* Did the provider actually execute it?
* Did money actually move?
* What amount was recovered?
* Did the outcome influence learning?

The audit trail is not just application logging.

It is part of the recovery system's accountability boundary.

---

# Important safety boundaries

RecoveryLab intentionally separates:

```text
Decision
Execution
Verification
Learning
```

The AI/decision layer does not directly call payment APIs.

The policy layer authorizes execution.

The executor invokes registered capabilities.

The verifier determines the actual outcome.

The learning layer receives verified outcomes.

This makes the system easier to reason about and prevents a model's decision from automatically becoming a financial action.

---

# Current MVP scope

### Implemented

* Razorpay Test Mode payment failure detection
* Revenue-risk case creation
* Failure diagnosis
* Adaptive strategy selection
* Merchant/contextual learning
* Policy authorization
* Razorpay Payment Link recovery
* Independent verification
* Recovery amount tracking
* Learning from verified outcomes
* Audit trail
* Recovery dashboard
* Live recovery timeline
* Held-out evaluation
* Baseline vs adaptive comparison

### Evaluation-only

The evaluation environment currently models additional recovery strategies, including:

```text
payment_link_recovery
payment_link_reminder
invoice_recovery
```

These allow us to test whether adaptive selection can outperform a fixed baseline without pretending that every capability is already connected to live Razorpay infrastructure.

### Future scope

The same signal/recovery architecture can be extended with additional revenue-risk sources:

```text
Checkout abandonment
        ↓
Subscription failure
        ↓
Overdue invoices
        ↓
Expired payment attempts
        ↓
Mandate failures
        ↓
...
```

The goal is not to create a collection of disconnected automations.

The goal is to build a common recovery engine that can take different revenue-risk signals and route them through the same:

**detect → diagnose → decide → authorize → execute → verify → learn**

loop.

---

# Tech stack

### Backend

* Python
* FastAPI
* Pydantic
* Pytest

### Frontend

* React
* Vite
* CSS

### Payments

* Razorpay Test Mode
* Razorpay Payment Links
* Razorpay webhooks

### Infrastructure

* ngrok for local webhook exposure

### Intelligence

* Contextual Bandit
* Merchant/context-specific strategy learning
* Deterministic held-out evaluation

---

# The core idea

RecoveryLab is not trying to be another:

> **"AI that sends a payment link when payment fails."**

The Payment Link is only one capability.

The interesting system is the loop around it:

```text
              WHAT HAPPENED?
                    │
                    ▼
               DETECT RISK
                    │
                    ▼
              WHY DID IT HAPPEN?
                    │
                    ▼
              WHAT SHOULD WE DO?
                    │
                    ▼
             IS IT ALLOWED?
                    │
                    ▼
             EXECUTE ACTION
                    │
                    ▼
             DID MONEY MOVE?
                    │
                    ▼
              LEARN FROM TRUTH
```

**The agent makes the decision.
The system controls the action.
The provider provides the evidence.
The verifier determines the truth.
The learner improves the next decision.**

That is RecoveryLab.
