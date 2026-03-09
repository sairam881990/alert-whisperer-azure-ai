"""
Base MCP (Model Context Protocol) client for all connectors.
Implements the JSON-RPC 2.0 transport layer with retry logic,
connection health checks, and structured error handling.
"""

from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
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
    - Connection health checks
    - Automatic retries with exponential backoff
    - Request/response logging
    """

    def __init__(
        self,
        server_url: str,
        client_name: str = "alert-whisperer",
        timeout: float = 60.0,
    ):
        self.server_url = server_url.rstrip("/")
        self.client_name = client_name
        self.timeout = timeout
        self._http_client: Optional[httpx.AsyncClient] = None
        self._initialized = False
        self._available_tools: list[MCPToolDefinition] = []

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

    @retry(
        retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
    )
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """
        Invoke an MCP tool by name with provided arguments.
        Includes automatic retry on transient failures.
        """
        logger.info("mcp_tool_call", tool=tool_name, server=self.server_url)

        response = await self._send_request(
            method="tools/call",
            params={
                "name": tool_name,
                "arguments": arguments,
            },
        )

        if response.is_error:
            raise MCPToolError(
                message=f"Tool '{tool_name}' failed: {response.error}",
                code=response.error.get("code", -1) if response.error else -1,
                data=response.error.get("data") if response.error else None,
            )

        # Extract content from MCP tool response
        result = response.result
        if isinstance(result, dict) and "content" in result:
            contents = result["content"]
            # Return text content concatenated
            text_parts = [
                c["text"] for c in contents if c.get("type") == "text"
            ]
            return "\n".join(text_parts) if text_parts else result

        return result

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

        try:
            http_response = await client.post(
                "/mcp",
                json=request.model_dump(exclude_none=True),
            )
            http_response.raise_for_status()
            data = http_response.json()
            return MCPResponse(**data)
        except httpx.ConnectError as e:
            logger.error("mcp_connection_failed", server=self.server_url, error=str(e))
            raise MCPConnectionError(f"Cannot connect to MCP server at {self.server_url}: {e}")
        except httpx.HTTPStatusError as e:
            logger.error("mcp_http_error", status=e.response.status_code, body=e.response.text[:500])
            raise MCPClientError(f"HTTP {e.response.status_code}: {e.response.text[:200]}")

    async def health_check(self) -> bool:
        """Check if the MCP server is reachable and responding."""
        try:
            client = await self._get_client()
            resp = await client.get("/health", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

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
