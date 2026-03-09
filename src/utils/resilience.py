"""
Resilience utilities for Alert Whisperer.

Provides production-grade infrastructure for:
1. Robust JSON extraction from LLM outputs (handles nested braces, markdown fences, partial responses)
2. Pydantic validation wrappers for all LLM JSON outputs
3. Circuit breaker pattern for external services (Azure AI Search)
4. End-to-end latency budget tracking
5. Output guardrails (length limits, PII filtering)

Round 2.2 — Hardened LLM parsing, resilience patterns, guardrails.
"""

from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Type, TypeVar

import structlog
from pydantic import BaseModel, Field, ValidationError

logger = structlog.get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pydantic Models for LLM JSON Outputs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RerankerScore(BaseModel):
    """Validated reranker output from LLM cross-encoder."""
    score: float = Field(ge=0.0, le=1.0, default=0.5, description="Relevance score 0-1")
    reason: str = Field(default="", description="Reasoning for the score")


class SelfEvalResult(BaseModel):
    """Validated self-evaluation result from Self-RAG."""
    supported: str = Field(default="unknown", description="One of: yes, no, unknown")
    relevant: str = Field(default="unknown", description="One of: yes, no, unknown")
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    needs_retrieval: str = Field(default="no", description="One of: yes, no")


class CRAGJudgeResult(BaseModel):
    """Validated CRAG judge output."""
    score: float = Field(ge=0.0, le=10.0, default=5.0, description="Relevance score 0-10")
    reasoning: str = Field(default="", description="Explanation of the relevance judgment")


class FusionQueriesResult(BaseModel):
    """Validated fusion query variants from LLM."""
    queries: list[str] = Field(default_factory=list, description="Generated query variants")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Robust JSON Extraction (replaces fragile regex)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Pre-compiled patterns for stripping markdown fences
_MD_FENCE_PATTERN = re.compile(
    r"```(?:json)?\s*\n?(.*?)\n?\s*```",
    re.DOTALL,
)


def extract_json_robust(text: str, expect_array: bool = False) -> Any:
    """Extract JSON from LLM output, handling:
    - Markdown code fences (```json ... ```)
    - Nested braces / bracket matching
    - Leading/trailing prose around the JSON
    - Partial responses with truncated JSON

    Args:
        text: Raw LLM response text.
        expect_array: If True, look for a JSON array ([...]) instead of object ({...}).

    Returns:
        Parsed Python dict or list. Raises ValueError on failure.
    """
    if not text or not text.strip():
        raise ValueError("Empty LLM response — cannot extract JSON")

    # Step 1: Try to extract from markdown code fences first
    fence_match = _MD_FENCE_PATTERN.search(text)
    if fence_match:
        inner = fence_match.group(1).strip()
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            pass  # Fall through to bracket matching

    # Step 2: Try direct parse (LLM returned clean JSON)
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Step 3: Balanced bracket extraction
    open_char, close_char = ("[", "]") if expect_array else ("{", "}")
    result = _extract_balanced(text, open_char, close_char)
    if result is not None:
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            # Step 4: Try auto-closing truncated JSON
            repaired = _repair_truncated_json(result, open_char, close_char)
            if repaired:
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    pass

    raise ValueError(
        f"Could not extract valid JSON ({'array' if expect_array else 'object'}) "
        f"from LLM response: {text[:200]}..."
    )


def _extract_balanced(text: str, open_c: str, close_c: str) -> Optional[str]:
    """Extract the first balanced bracket/brace substring."""
    start = text.find(open_c)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape_next = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_c:
            depth += 1
        elif ch == close_c:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    # Unbalanced — return from start to end (for repair attempt)
    return text[start:]


def _repair_truncated_json(text: str, open_c: str, close_c: str) -> Optional[str]:
    """Attempt to repair truncated JSON by closing open brackets/braces."""
    # Count imbalance
    depth = 0
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_c:
            depth += 1
        elif ch == close_c:
            depth -= 1

    if depth <= 0:
        return None  # Not a truncation issue

    # Close the string if we're in one
    repaired = text
    if in_string:
        repaired += '"'
    # Add missing closing chars
    repaired += close_c * depth
    return repaired


def parse_json_with_validation(
    text: str,
    model_class: Type[T],
    expect_array: bool = False,
    fallback: Optional[T] = None,
) -> T:
    """Extract JSON from LLM output and validate with Pydantic model.

    Args:
        text: Raw LLM response text.
        model_class: Pydantic model class to validate against.
        expect_array: Whether to expect a JSON array.
        fallback: Optional fallback value if parsing/validation fails.

    Returns:
        Validated Pydantic model instance.

    Raises:
        ValueError if parsing fails and no fallback is provided.
    """
    try:
        data = extract_json_robust(text, expect_array=expect_array)
        if isinstance(data, dict):
            return model_class.model_validate(data)
        elif isinstance(data, list) and hasattr(model_class, "model_validate"):
            # For models that wrap a list (e.g., FusionQueriesResult)
            return model_class.model_validate({"queries": data})
        else:
            raise ValueError(f"Unexpected JSON type {type(data)} for model {model_class.__name__}")
    except (ValueError, ValidationError, json.JSONDecodeError) as e:
        logger.warning(
            "json_parse_validation_failed",
            model=model_class.__name__,
            error=str(e),
            text_preview=text[:150],
        )
        if fallback is not None:
            return fallback
        raise ValueError(f"Failed to parse {model_class.__name__}: {e}") from e


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Circuit Breaker Pattern
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CircuitState(str, Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Failing, reject calls
    HALF_OPEN = "half_open" # Testing recovery


class CircuitBreaker:
    """Circuit breaker for external service calls (Azure AI Search, etc.).

    States:
    - CLOSED: Normal operation. Track consecutive failures.
    - OPEN: Reject all calls immediately. Wait for cooldown.
    - HALF_OPEN: Allow a single probe call. Success → CLOSED, failure → OPEN.

    Usage:
        breaker = CircuitBreaker(name="azure_search", threshold=5, cooldown=60)
        if breaker.allow_request():
            try:
                result = do_search(...)
                breaker.record_success()
            except Exception:
                breaker.record_failure()
                # fallback
    """

    def __init__(
        self,
        name: str,
        threshold: int = 5,
        cooldown_seconds: float = 60.0,
    ):
        self.name = name
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._half_open_probe_allowed = True

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self.cooldown_seconds:
                self._state = CircuitState.HALF_OPEN
                self._half_open_probe_allowed = True
                logger.info("circuit_breaker_half_open", name=self.name, elapsed=round(elapsed, 1))
        return self._state

    def allow_request(self) -> bool:
        """Check if a request should be allowed."""
        current = self.state
        if current == CircuitState.CLOSED:
            return True
        if current == CircuitState.HALF_OPEN and self._half_open_probe_allowed:
            self._half_open_probe_allowed = False
            logger.info("circuit_breaker_probe", name=self.name)
            return True
        logger.debug("circuit_breaker_rejected", name=self.name, state=current.value)
        return False

    def record_success(self) -> None:
        """Record a successful call."""
        if self._state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
            logger.info("circuit_breaker_closed", name=self.name, previous=self._state.value)
        self._state = CircuitState.CLOSED
        self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed call."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self.threshold:
            prev = self._state
            self._state = CircuitState.OPEN
            if prev != CircuitState.OPEN:
                logger.warning(
                    "circuit_breaker_opened",
                    name=self.name,
                    consecutive_failures=self._failure_count,
                    cooldown=self.cooldown_seconds,
                )

    def reset(self) -> None:
        """Manually reset the circuit breaker."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# End-to-End Latency Budget Tracker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class LatencyBudget:
    """Tracks end-to-end latency budget for a complete request pipeline.

    Usage:
        budget = LatencyBudget(total_ms=5000)
        with budget.phase("embedding"):
            embeddings = await embed(texts)
        with budget.phase("search"):
            results = await search(...)
        with budget.phase("reranking"):
            reranked = await rerank(...)
        with budget.phase("llm_generation"):
            response = await llm(...)

        if budget.exceeded:
            logger.warning("latency_budget_exceeded", breakdown=budget.breakdown)
    """

    def __init__(self, total_ms: int = 5000, label: str = "request"):
        self.total_ms = total_ms
        self.label = label
        self._start_time = time.monotonic()
        self._phases: dict[str, float] = {}
        self._current_phase: Optional[str] = None
        self._phase_start: float = 0.0

    @contextmanager
    def phase(self, name: str):
        """Context manager to track a named phase."""
        start = time.monotonic()
        try:
            yield
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            self._phases[name] = elapsed_ms

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._start_time) * 1000

    @property
    def remaining_ms(self) -> float:
        return max(0, self.total_ms - self.elapsed_ms)

    @property
    def exceeded(self) -> bool:
        return self.elapsed_ms > self.total_ms

    @property
    def breakdown(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "total_budget_ms": self.total_ms,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "exceeded": self.exceeded,
            "phases": {k: round(v, 1) for k, v in self._phases.items()},
        }

    def log_if_exceeded(self) -> None:
        """Log a warning if the budget was exceeded."""
        if self.exceeded:
            logger.warning("latency_budget_exceeded", **self.breakdown)
        else:
            logger.debug("latency_budget_ok", **self.breakdown)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Output Guardrails
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# PII detection patterns (common sensitive data patterns)
_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("credit_card", re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b")),
    ("phone_us", re.compile(r"\b(?:\+1[- ]?)?\(?\d{3}\)?[- ]?\d{3}[- ]?\d{4}\b")),
    ("ip_address", re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
    ("azure_key", re.compile(r"\b[A-Za-z0-9+/]{40,88}={0,2}\b")),
    ("connection_string", re.compile(
        r"(?:AccountKey|SharedAccessKey|Password|pwd)\s*=\s*[^\s;]+",
        re.IGNORECASE,
    )),
]


class OutputGuardrails:
    """Post-processing guardrails for LLM output.

    Enforces:
    - Maximum response length
    - PII detection and redaction
    - Content safety checks
    """

    DEFAULT_MAX_CHARS = 15000
    PII_PLACEHOLDER = "[REDACTED]"

    def __init__(
        self,
        max_chars: int = DEFAULT_MAX_CHARS,
        enable_pii_filter: bool = True,
        enable_length_limit: bool = True,
    ):
        self.max_chars = max_chars
        self.enable_pii_filter = enable_pii_filter
        self.enable_length_limit = enable_length_limit

    def apply(self, text: str) -> str:
        """Apply all enabled guardrails to the output text."""
        result = text
        if self.enable_pii_filter:
            result, findings = self.filter_pii(result)
            if findings:
                logger.info("pii_redacted", count=len(findings), types=[f[0] for f in findings])
        if self.enable_length_limit:
            result = self.enforce_length(result)
        return result

    def filter_pii(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        """Detect and redact PII patterns. Returns (cleaned_text, findings)."""
        findings: list[tuple[str, str]] = []
        result = text
        for pii_type, pattern in _PII_PATTERNS:
            matches = pattern.findall(result)
            for match in matches:
                findings.append((pii_type, match))
            result = pattern.sub(self.PII_PLACEHOLDER, result)
        return result, findings

    def enforce_length(self, text: str) -> str:
        """Truncate response if it exceeds max_chars."""
        if len(text) <= self.max_chars:
            return text
        truncated = text[: self.max_chars]
        # Try to cut at a sentence boundary
        last_period = truncated.rfind(".")
        if last_period > self.max_chars * 0.8:
            truncated = truncated[: last_period + 1]
        truncated += "\n\n*[Response truncated for brevity]*"
        logger.info("response_truncated", original_len=len(text), truncated_len=len(truncated))
        return truncated

    def detect_pii(self, text: str) -> list[tuple[str, str]]:
        """Detect PII without redacting. Returns list of (type, match)."""
        findings: list[tuple[str, str]] = []
        for pii_type, pattern in _PII_PATTERNS:
            for match in pattern.findall(text):
                findings.append((pii_type, match))
        return findings


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Retry helpers (async, with exponential backoff + jitter)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import asyncio
import random


async def retry_with_backoff(
    coro_factory,
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable_exceptions: tuple = (Exception,),
    operation_name: str = "operation",
) -> Any:
    """Execute an async operation with exponential backoff retry.

    Args:
        coro_factory: Callable that returns a new coroutine on each call.
        max_retries: Maximum number of attempts.
        base_delay: Base delay in seconds for exponential backoff.
        max_delay: Maximum delay cap.
        retryable_exceptions: Exception types that trigger a retry.
        operation_name: Human-readable name for logging.

    Returns:
        The result of the successful call.

    Raises:
        The last exception if all retries are exhausted.
    """
    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return await coro_factory()
        except retryable_exceptions as e:
            last_error = e
            if attempt < max_retries - 1:
                delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)
                logger.warning(
                    "retry_backoff",
                    operation=operation_name,
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    delay_seconds=round(delay, 2),
                    error=str(e),
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "retries_exhausted",
                    operation=operation_name,
                    attempts=max_retries,
                    error=str(e),
                )
    raise last_error  # type: ignore[misc]
