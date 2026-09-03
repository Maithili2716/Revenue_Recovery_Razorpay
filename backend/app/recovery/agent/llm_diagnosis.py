"""LLM-powered diagnosis provider using Grok (xAI).

Provides an LLM-based diagnosis of a recovery case using the OpenAI-compatible
xAI API.  The LLM is a *reasoning component only* — it does NOT execute
payments, select recovery actions, or bypass the contextual bandit.

Architecture:
    AgentContext → GrokDiagnosisProvider.diagnose() → validated Diagnosis

Fallback:
    If Grok is unavailable, the API key is missing, the response cannot be
    parsed, or validation fails, the provider falls back to the existing
    deterministic diagnosis engine transparently.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from openai import OpenAI

from app.recovery.agent.models import (
    AgentContext,
    Diagnosis,
    DiagnosisCategory,
    FailureStage,
)

logger = logging.getLogger(__name__)

# ── Timeout for Grok requests (seconds) ────────────────────────────────────
_REQUEST_TIMEOUT_SECONDS = 10

# ── xAI API base URL ──────────────────────────────────────────────────────
_XAI_BASE_URL = "https://api.x.ai/v1"

# ── System instruction ─────────────────────────────────────────────────────
_SYSTEM_INSTRUCTION = """\
You are the diagnosis component of a revenue recovery system.

Your ONLY job is to diagnose WHY a revenue event is at risk.

Rules:
- You are NOT an execution agent.
- You must reason ONLY from the supplied evidence.
- Do NOT invent facts.  If a piece of information is absent, say "unknown".
- Do NOT recommend or execute financial actions.
- Do NOT make authorization decisions.
- Do NOT fabricate customer information or transaction details.
- Return ONLY the requested structured diagnosis as valid JSON.

You MUST respond with a JSON object containing exactly these fields:
- "category": string, one of "payment_failure" or "unknown"
- "primary_reason": string, machine-readable reason e.g. "bank_decline"
- "failure_stage": string, one of "payment_authorization", "payment_processing", "payment_capture", or "unknown"
- "confidence": float between 0.0 and 1.0
- "reason_codes": array of strings, machine-readable reason codes
- "details": string or null, short human-readable explanation

Confidence guidelines:
- 0.8–1.0 : Strong evidence supports the diagnosis.
- 0.5–0.8 : Reasonable inference from partial evidence.
- 0.0–0.5 : Limited evidence; diagnosis is speculative.

Evidence discipline:
- Clearly distinguish between OBSERVED evidence and REASONABLE INFERENCE.
- If the failure reason is unknown, set primary_reason to "unknown_failure".
- Never claim specific causes (e.g. "insufficient funds") unless that
  information is explicitly present in the input.
"""


# ── SDK response schema ────────────────────────────────────────────────────
# A lightweight Pydantic model used ONLY to validate the LLM's JSON output.
# We then convert the parsed result to our canonical Diagnosis model.


class _LLMDiagnosisSchema(BaseModel):
    """Schema for the LLM's structured JSON output.

    Does NOT replace our canonical Diagnosis model.
    """

    category: str = Field(description="Diagnosis category: 'payment_failure' or 'unknown'.")
    primary_reason: str = Field(description="Machine-readable primary reason for the failure, e.g. 'bank_decline'.")
    failure_stage: str = Field(description="Stage where failure occurred: 'payment_authorization', 'payment_processing', 'payment_capture', or 'unknown'.")
    confidence: float = Field(description="Confidence in this diagnosis, 0.0 to 1.0.")
    reason_codes: list[str] = Field(description="List of machine-readable reason codes.")
    details: Optional[str] = Field(default=None, description="Short human-readable explanation.")


def _build_user_prompt(context: AgentContext) -> str:
    """Build a user prompt with normalized evidence from the AgentContext.

    Only includes information actually available — never sends secrets,
    webhook signatures, or raw payloads.
    """
    evidence: dict[str, object] = {
        "signal_type": context.signal_type,
        "amount_at_risk_minor": context.amount_at_risk_minor,
        "currency": context.currency,
        "failure_source": context.failure_source,
        "failure_step": context.failure_step,
        "failure_reason": context.failure_reason,
        "payment_method": context.payment_method,
        "recoverability": context.recoverability,
        "urgency": context.urgency,
        "reason_codes": context.reason_codes,
    }

    # Include optional context only when present.
    if context.customer_id:
        evidence["customer_id_present"] = True  # Don't send actual ID.
    if context.previous_attempts:
        evidence["previous_attempt_count"] = len(context.previous_attempts)

    return (
        "Diagnose the following revenue recovery case based on the evidence below.\n\n"
        f"Evidence:\n{json.dumps(evidence, indent=2, default=str)}\n\n"
        "Return the structured diagnosis as a JSON object."
    )


def _schema_to_diagnosis(data: dict) -> Diagnosis:
    """Convert a parsed LLM response dict into our canonical Diagnosis.

    Safely handles unknown enum values and out-of-range confidence.
    """
    # Validate enum values before feeding to Pydantic.
    category_str = data.get("category", "unknown")
    try:
        DiagnosisCategory(category_str)
    except ValueError:
        data["category"] = "unknown"

    stage_str = data.get("failure_stage", "unknown")
    try:
        FailureStage(stage_str)
    except ValueError:
        data["failure_stage"] = "unknown"

    # Clamp confidence.
    conf = float(data.get("confidence", 0.5))
    data["confidence"] = max(0.0, min(1.0, conf))

    # Mark source as LLM.
    data["diagnosis_source"] = "llm"

    return Diagnosis.model_validate(data)


def _parse_llm_response(raw_text: str) -> Diagnosis:
    """Parse and validate the LLM response text into a Diagnosis.

    Raises ValueError if the response cannot be parsed or fails validation.
    """
    text = raw_text.strip()
    # Strip markdown code fences if present.
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3].strip()

    data = json.loads(text)
    return _schema_to_diagnosis(data)


# ── Protocol ────────────────────────────────────────────────────────────────


@runtime_checkable
class LLMDiagnosisProvider(Protocol):
    """Interface for LLM-based diagnosis providers."""

    def diagnose(self, context: AgentContext) -> Diagnosis:
        """Produce a validated Diagnosis from an AgentContext."""
        ...


# ── Concrete Grok implementation ───────────────────────────────────────────


class GrokDiagnosisProvider:
    """Diagnosis provider backed by xAI Grok (OpenAI-compatible API).

    Uses the OpenAI SDK pointed at xAI's endpoint.  The LLM is instructed
    to return JSON matching our schema.  Falls back to deterministic
    diagnosis on any failure.
    """

    def __init__(self, api_key: str, model: str) -> None:
        self._client = OpenAI(
            api_key=api_key,
            base_url=_XAI_BASE_URL,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        self._model = model

    def diagnose(self, context: AgentContext) -> Diagnosis:
        """Call Grok to diagnose the recovery case.

        Returns a validated Diagnosis with diagnosis_source='llm'.
        Falls back to deterministic diagnosis on any error.
        """
        from app.recovery.agent.diagnosis import diagnose as deterministic_diagnose

        start_time = time.monotonic()
        try:
            user_prompt = _build_user_prompt(context)

            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SYSTEM_INSTRUCTION},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=512,
                response_format={"type": "json_object"},
            )

            latency_ms = round((time.monotonic() - start_time) * 1000)

            content = response.choices[0].message.content
            if not content:
                raise ValueError("Grok returned empty content")

            # Parse the JSON response and validate against our schema.
            data = json.loads(content)
            # Validate against our Pydantic schema for structure.
            _LLMDiagnosisSchema.model_validate(data)

            logger.debug(
                "agent_llm_response_parsed",
                extra={
                    "case_id": context.case_id,
                    "llm_provider": "grok",
                    "model": self._model,
                    "latency_ms": latency_ms,
                },
            )
            diagnosis = _schema_to_diagnosis(data)

            logger.info(
                "agent_llm_diagnosis_created",
                extra={
                    "diagnosis_source": "llm",
                    "llm_provider": "grok",
                    "model": self._model,
                    "latency_ms": latency_ms,
                    "category": diagnosis.category.value,
                    "primary_reason": diagnosis.primary_reason,
                    "failure_stage": diagnosis.failure_stage.value,
                    "confidence": diagnosis.confidence,
                    "case_id": context.case_id,
                },
            )
            return diagnosis

        except Exception as exc:
            latency_ms = round((time.monotonic() - start_time) * 1000)
            logger.warning(
                "agent_llm_diagnosis_fallback",
                extra={
                    "case_id": context.case_id,
                    "llm_provider": "grok",
                    "model": self._model,
                    "latency_ms": latency_ms,
                    "fallback_reason": type(exc).__name__,
                    "error": str(exc)[:200],
                },
            )

            fallback = deterministic_diagnose(context)
            fallback.diagnosis_source = "deterministic_fallback"
            return fallback
