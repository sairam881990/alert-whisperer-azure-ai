"""
MCP Connector Layer.

Provides unified access to all data sources through the Model Context Protocol (MCP):
- Kusto / Azure Data Explorer: KQL queries, ingestion failures, cluster health
- Confluence: Runbook retrieval, operational procedures
- ICM: Incident management, historical incidents, resolution notes
- Log Analytics: Spark/Synapse logs, error detection, trend analysis
"""

from src.connectors.confluence_connector import ConfluenceMCPClient
from src.connectors.icm_connector import ICMMCPClient
from src.connectors.kusto_connector import KustoMCPClient
from src.connectors.loganalytics_connector import LogAnalyticsMCPClient
from src.connectors.mcp_base import BaseMCPClient, MCPClientError, MCPConnectionError, MCPToolError

__all__ = [
    "BaseMCPClient",
    "MCPClientError",
    "MCPConnectionError",
    "MCPToolError",
    "KustoMCPClient",
    "ConfluenceMCPClient",
    "ICMMCPClient",
    "LogAnalyticsMCPClient",
]
