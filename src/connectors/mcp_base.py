"""
Base MCP (Model Context Protocol) client for all connectors.
Implements the JSON-RPC 2.0 transport layer with retry logic,
connection health checks, and structured error handling.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
import uuid
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Optional

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.models import MCPRequest, MCPResponse, MCPToolDefinition

logger = structlog.get_logger(__name__)

# ─── M5: Error Classification ──────────────────────

class MCPErrorClass(str, Enum):
    """Classification of MCP errors for retry/handling decisions."""
    TRANSIENT = "TRANSIENT"          # Safe to retry (network blips, 503s)
    PERMANENT = "PERMANENT"          # Do not retry (bad request, not found)
    AUTH_EXPIRED = "AUTH_EXPIRED"    # Need re-authentication
    RATE_LIMITED = "RATE_LIMITED"    # Back off before retrying
    UNKNOWN = "UNKNOWN"              # Unclassified — retry conservatively


_TRANSIENT_STATUS_CODES = {500, 502, 503, 504, 507, 599}
_AUTH_STATUS_CODES = {401, 403}
_RATE_LIMITED_STATUS_CODES = {429}
_PERMANENT_STATUS_CODES = {400, 404, 405, 409, 410, 422}

# Regex patterns (case-insensitive) that indicate transient conditions
_TRANSIENT_MESSAGE_PATTERNS = re.compile(
    r"(connection\s*reset|connection\s*refused|broken\s*pipe"
    r"|temporarily\s*unavailable|service\s*unavailable"
    r"|upstream\s*connect\s*error|gateway\s*timeout|timeout)",
    re.IGNORECASE,
)


def classify_error(exc: BaseException) -> MCPErrorClass:
    """Classify an exception to guide retry/back-off logic."""
    # HTTP status-based classification
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in _RATE_LIMITED_STATUS_CODES:
            return MCPErrorClass.RATE_LIMITED
        if code in _AUTH_STATUS_CODES:
            return MCPErrorClass.AUTH_EXPIRED
        if code in _TRANSIENT_STATUS_CODES:
            return MCPErrorClass.TRANSIENT
        if code in _PERMANENT_STATUS_CODES:
            return MCPErrorClass.PERMANENT

    # Network / IO level transient conditions
    if isinstance(exc, (
        httpx.ConnectError,
        httpx.TimeoutException,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        ConnectionResetError,
        OSError,
    )):
        return MCPErrorClass.TRANSIENT

    # MCPClientError sub-types
    if isinstance(exc, MCPConnectionError):
        return MCPErrorClass.TRANSIENT

    # Message-content heuristics for generic exceptions
    msg = str(exc)
    if _TRANSIENT_MESSAGE_PATTERNS.search(msg):
        return MCPErrorClass.TRANSIENT

    return MCPErrorClass.UNKNOWN


# ─── M5/M7: Retryable exception predicate ─────────

def _is_retryable(exc: BaseException) -> bool:
    """Return True if this exception should be retried."""
    cls = classify_error(exc)
    return cls in (MCPErrorClass.TRANSIENT, MCPErrorClass.RATE_LIMITED, MCPErrorClass.UNKNOWN)


# ─── M7: Circuit Breaker ───────────────────────────

class CircuitBreakerOpen(Exception):
    """Raised when the circuit breaker is open and calls are blocked."""
    pass


class CircuitBreaker:
    """
    Simple half-open circuit breaker.

    States:
    - CLOSED  : Normal operation; failures are counted.
    - OPEN    : Calls fail immediately; waits for cooldown.
    - HALF_OPEN: One probe request is allowed through.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: float = 60.0):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._failures = 0
        self._state = self.CLOSED
        self._opened_at: Optional[float] = None

    @property
    def state(self) -> str:
        if self._state == self.OPEN:
            if time.monotonic() - (self._opened_at or 0) >= self.cooldown_seconds:
                self._state = self.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        self._failures = 0
        self._state = self.CLOSED
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = self.OPEN
            self._opened_at = time.monotonic()

    def allow_request(self) -> bool:
        s = self.state  # triggers OPEN→HALF_OPEN transition if cooldown elapsed
        return s in (self.CLOSED, self.HALF_OPEN)

    def reset(self) -> None:
        self._failures = 0
        self._state = self.CLOSED
        self._opened_at = None


# ─── M6: Sensitive data masking ───────────────────

_SENSITIVE_PATTERNS = re.compile(
    r'(?i)(api[_\s-]?key|token|secret|password|auth|bearer|client[_\s-]?secret)',
)


def _mask_sensitive(data: Any, depth: int = 0) -> Any:
    """Recursively mask sensitive values in dicts/strings (max 5 levels deep)."""
    if depth > 5:
        return data
    if isinstance(data, dict):
        return {
            k: "***MASKED***" if _SENSITIVE_PATTERNS.search(str(k)) else _mask_sensitive(v, depth + 1)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_mask_sensitive(item, depth + 1) for item in data]
    if isinstance(data, str) and len(data) > 200:
        return data[:200] + "...<truncated>"
    return data


class MCPClientError(Exception):
    """Base exception for MCP client errors."""

    def __init__(self, message: str, code: int = -1, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class MCPConnectionError(MCPClientError):
    """Raised when MCP server is unreachable."""
    pass


class MCPToolError(MCPClientError):
    """Raised when an MCP tool invocation fails."""
    pass


class BaseMCPClient(ABC):
    """
    Abstract base class for MCP server clients.

    Each connector (Kusto, Confluence, ICM, Log Analytics) extends this
    to implement source-specific tool invocations. The base handles:
    - JSON-RPC transport over HTTP
    - Connection health checks with per-connector health_check()
    - Automatic retries with exponential backoff + jitter (M7)
    - Circuit breaker to stop hammering a failing server (M7)
    - Structured request/response logging with sensitive data masking (M6)
    - Error classification for smart retry decisions (M5)
    """

    def __init__(
        self,
        server_url: str,
        client_name: str = "alert-whisperer",
        timeout: float = 60.0,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_cooldown: float = 60.0,
    ):
        self.server_url = server_url.rstrip("/")
        self.client_name = client_name
        self.timeout = timeout
        self._http_client: Optional[httpx.AsyncClient] = None
        self._initialized = False
        self._available_tools: list[MCPToolDefinition] = []
        # M7: Circuit breaker per client instance
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=circuit_breaker_threshold,
            cooldown_seconds=circuit_breaker_cooldown,
        )

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-initialize the HTTP client."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                base_url=self.server_url,
                timeout=httpx.Timeout(self.timeout),
                headers={
                    "Content-Type": "application/json",
                    "X-Client-Name": self.client_name,
                },
            )
        return self._http_client

    async def initialize(self) -> dict[str, Any]:
        """
        Initialize the MCP session.
        Sends the `initialize` method per MCP spec.
        """
        response = await self._send_request(
            method="initialize",
            params={
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {
                    "name": self.client_name,
                    "version": "1.0.0",
                },
            },
        )
        self._initialized = True
        logger.info("mcp_initialized", server=self.server_url, result=response.result)
        return response.result or {}

    async def list_tools(self) -> list[MCPToolDefinition]:
        """Discover available tools on the MCP server."""
        response = await self._send_request(method="tools/list")
        tools_data = response.result.get("tools", []) if response.result else []
        self._available_tools = [
            MCPToolDefinition(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
            )
            for t in tools_data
        ]
        logger.info(
            "mcp_tools_listed",
            server=self.server_url,
            tool_count=len(self._available_tools),
            tools=[t.name for t in self._available_tools],
        )
        return self._available_tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """
        Invoke an MCP tool by name with provided arguments.

        Includes:
        - M6: Structured request/response logging with masking
        - M7: Retry with exponential backoff + jitter; circuit breaker
        - M5: Error classification to skip retries on permanent failures
        """
        # M7: Circuit breaker check
        if not self._circuit_breaker.allow_request():
            raise CircuitBreakerOpen(
                f"Circuit breaker OPEN for {self.server_url} — "
                f"cooldown {self._circuit_breaker.cooldown_seconds}s not elapsed"
            )

        # M6: Log the outgoing call (mask arguments)
        request_id = str(uuid.uuid4())[:8]
        logger.info(
            "mcp_tool_call",
            tool=tool_name,
            server=self.server_url,
            request_id=request_id,
            arguments=_mask_sensitive(arguments),
        )

        max_attempts = 3
        base_delay = 2.0
        last_exc: Optional[BaseException] = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = await self._send_request(
                    method="tools/call",
                    params={
                        "name": tool_name,
                        "arguments": arguments,
                    },
                )

                if response.is_error:
                    err = MCPToolError(
                        message=f"Tool '{tool_name}' failed: {response.error}",
                        code=response.error.get("code", -1) if response.error else -1,
                        data=response.error.get("data") if response.error else None,
                    )
                    # M5: Permanent tool errors should not be retried
                    raise err

                # Extract content from MCP tool response
                result = response.result
                if isinstance(result, dict) and "content" in result:
                    contents = result["content"]
                    text_parts = [
                        c["text"] for c in contents if c.get("type") == "text"
                    ]
                    result = "\n".join(text_parts) if text_parts else result

                # M6: Log the response summary
                logger.info(
                    "mcp_tool_response",
                    tool=tool_name,
                    server=self.server_url,
                    request_id=request_id,
                    result_type=type(result).__name__,
                    result_length=len(result) if isinstance(result, (str, list)) else None,
                )

                self._circuit_breaker.record_success()
                return result

            except CircuitBreakerOpen:
                raise

            except MCPToolError:
                # Tool-level errors are permanent — don't retry
                self._circuit_breaker.record_failure()
                raise

            except BaseException as exc:  # noqa: BLE001
                last_exc = exc
                error_class = classify_error(exc)

                # M6: Log the failure
                logger.warning(
                    "mcp_tool_call_failed",
                    tool=tool_name,
                    server=self.server_url,
                    request_id=request_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error_class=error_class.value,
                    error=str(exc),
                )

                # M5: Permanent / auth errors — do not retry
                if error_class in (MCPErrorClass.PERMANENT, MCPErrorClass.AUTH_EXPIRED):
                    self._circuit_breaker.record_failure()
                    raise

                if attempt >= max_attempts:
                    self._circuit_breaker.record_failure()
                    break

                # M7: Exponential backoff with jitter
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                # Rate-limited: honour back-off more aggressively
                if error_class == MCPErrorClass.RATE_LIMITED:
                    delay = max(delay, 10.0)
                logger.info(
                    "mcp_retry_backoff",
                    tool=tool_name,
                    attempt=attempt,
                    delay_seconds=round(delay, 2),
                )
                await asyncio.sleep(delay)

        self._circuit_breaker.record_failure()
        raise last_exc  # type: ignore[misc]

    async def _send_request(
        self, method: str, params: Optional[dict[str, Any]] = None
    ) -> MCPResponse:
        """Send a JSON-RPC 2.0 request to the MCP server."""
        request = MCPRequest(
            method=method,
            params=params or {},
            id=str(uuid.uuid4()),
        )

        client = await self._get_client()

        # M6: Log outgoing request
        logger.debug(
            "mcp_request",
            method=method,
            server=self.server_url,
            request_id=request.id,
            params=_mask_sensitive(params or {}),
        )

        try:
            http_response = await client.post(
                "/mcp",
                json=request.model_dump(exclude_none=True),
            )
            http_response.raise_for_status()
            data = http_response.json()

            # M6: Log successful response
            logger.debug(
                "mcp_response",
                method=method,
                server=self.server_url,
                request_id=request.id,
                status_code=http_response.status_code,
                has_error="error" in data,
            )
            return MCPResponse(**data)
        except httpx.ConnectError as e:
            logger.error("mcp_connection_failed", server=self.server_url, error=str(e))
            raise MCPConnectionError(f"Cannot connect to MCP server at {self.server_url}: {e}")
        except httpx.HTTPStatusError as e:
            # M5/M6: Log with classification
            error_class = classify_error(e)
            logger.error(
                "mcp_http_error",
                status=e.response.status_code,
                body=e.response.text[:500],
                error_class=error_class.value,
            )
            raise MCPClientError(
                f"HTTP {e.response.status_code}: {e.response.text[:200]}",
                code=e.response.status_code,
            )
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
            # M7: Explicit coverage of httpx timeout sub-types
            logger.error("mcp_timeout", server=self.server_url, error=str(e), exc_type=type(e).__name__)
            raise
        except (ConnectionResetError, OSError) as e:
            # M7: OS-level network errors
            logger.error("mcp_network_error", server=self.server_url, error=str(e))
            raise

    async def health_check(self) -> dict[str, Any]:
        """
        M4: Verify MCP server connectivity and return a structured status.

        Returns a dict with keys:
        - healthy (bool)
        - latency_ms (float | None)
        - circuit_breaker (str) — CLOSED / OPEN / HALF_OPEN
        - detail (str) — human-readable message
        """
        status: dict[str, Any] = {
            "healthy": False,
            "latency_ms": None,
            "circuit_breaker": self._circuit_breaker.state,
            "detail": "",
        }
        start = time.monotonic()
        try:
            client = await self._get_client()
            resp = await client.get("/health", timeout=5.0)
            latency = (time.monotonic() - start) * 1000
            status["latency_ms"] = round(latency, 1)
            if resp.status_code == 200:
                status["healthy"] = True
                status["detail"] = f"OK ({resp.status_code})"
            else:
                status["detail"] = f"Unexpected status {resp.status_code}"
        except Exception as exc:
            status["latency_ms"] = round((time.monotonic() - start) * 1000, 1)
            status["detail"] = f"{type(exc).__name__}: {exc}"
        logger.info(
            "mcp_health_check",
            server=self.server_url,
            **status,
        )
        return status

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    @abstractmethod
    async def validate_connection(self) -> bool:
        """Validate that the connector can reach its data source."""
        ...

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} server={self.server_url}>"
