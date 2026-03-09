"""
Configuration management using Pydantic Settings.
Loads from .env file and environment variables with validation.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class AzureSettings(BaseSettings):
    """Azure authentication and service configuration."""

    model_config = SettingsConfigDict(env_prefix="AZURE_")

    tenant_id: str = Field(description="Azure AD Tenant ID")
    client_id: str = Field(description="Azure AD App Registration Client ID")
    client_secret: SecretStr = Field(description="Azure AD App Registration Secret")
    subscription_id: str = Field(default="", description="Azure Subscription ID")


class KustoSettings(BaseSettings):
    """Azure Data Explorer (Kusto) MCP Server configuration."""

    model_config = SettingsConfigDict(env_prefix="KUSTO_")

    cluster_url: str = Field(description="Kusto cluster URL, e.g. https://mycluster.region.kusto.windows.net")
    database: str = Field(description="Default Kusto database name")
    mcp_server_url: str = Field(default="http://localhost:3001", description="Kusto MCP Server endpoint")
    query_timeout_seconds: int = Field(default=120, description="Query timeout in seconds")


class ConfluenceSettings(BaseSettings):
    """Confluence MCP Server configuration for runbook retrieval."""

    model_config = SettingsConfigDict(env_prefix="CONFLUENCE_")

    base_url: str = Field(description="Confluence base URL, e.g. https://mycompany.atlassian.net/wiki")
    username: str = Field(description="Confluence username / email")
    api_token: SecretStr = Field(description="Confluence API token")
    mcp_server_url: str = Field(default="http://localhost:3002", description="Confluence MCP Server endpoint")
    space_keys: list[str] = Field(default=["RUNBOOKS", "OPS", "DE"], description="Confluence space keys to search")


class ICMSettings(BaseSettings):
    """ICM (Incident Management) MCP Server configuration."""

    model_config = SettingsConfigDict(env_prefix="ICM_")

    mcp_server_url: str = Field(default="http://localhost:3003", description="ICM MCP Server endpoint")
    api_base_url: str = Field(default="", description="ICM REST API base URL")
    api_key: SecretStr = Field(default=SecretStr(""), description="ICM API key if required")
    poll_interval_seconds: int = Field(default=30, description="Polling interval for new incidents")


class LogAnalyticsSettings(BaseSettings):
    """Azure Log Analytics MCP Server configuration."""

    model_config = SettingsConfigDict(env_prefix="LOG_ANALYTICS_")

    workspace_id: str = Field(description="Log Analytics Workspace ID")
    mcp_server_url: str = Field(default="http://localhost:3004", description="Log Analytics MCP Server endpoint")
    default_timespan: str = Field(default="P1D", description="Default query timespan (ISO 8601 duration)")


class LLMSettings(BaseSettings):
    """LLM / OpenAI configuration."""

    model_config = SettingsConfigDict(env_prefix="LLM_")

    provider: str = Field(default="azure_openai", description="LLM provider: azure_openai | openai")
    api_key: SecretStr = Field(description="OpenAI or Azure OpenAI API key")
    api_base: str = Field(default="", description="Azure OpenAI endpoint URL")
    api_version: str = Field(default="2024-02-01", description="Azure OpenAI API version")
    deployment_name: str = Field(default="gpt-4o", description="Model deployment name")
    embedding_deployment: str = Field(default="text-embedding-3-large", description="Embedding model deployment (3072 dims)")
    embedding_dimensions: int = Field(default=3072, description="Embedding vector dimensions")
    temperature: float = Field(default=0.1, description="Default temperature for completions")
    max_tokens: int = Field(default=4096, description="Max tokens for completions")


class TeamsSettings(BaseSettings):
    """Microsoft Teams notification configuration."""

    model_config = SettingsConfigDict(env_prefix="TEAMS_")

    webhook_url: str = Field(default="", description="Teams incoming webhook URL for alerts")
    bot_app_id: str = Field(default="", description="Teams Bot App ID for interactive chat")
    bot_app_secret: SecretStr = Field(default=SecretStr(""), description="Teams Bot App Secret")


class AzureSearchSettings(BaseSettings):
    """Azure AI Search configuration."""

    model_config = SettingsConfigDict(env_prefix="AZURE_SEARCH_")

    endpoint: str = Field(default="", description="Azure AI Search endpoint URL")
    api_key: SecretStr = Field(default=SecretStr(""), description="Azure AI Search admin API key")
    index_name: str = Field(default="alert-whisperer-kb", description="Search index name")


class RAGSettings(BaseSettings):
    """RAG pipeline configuration."""

    model_config = SettingsConfigDict(env_prefix="RAG_")

    vector_store_path: str = Field(default="data/vector_store", description="Local fallback storage directory")
    collection_name: str = Field(default="alert_whisperer_kb", description="Collection/index name")
    child_chunk_tokens: int = Field(default=450, description="Child chunk size in tokens (400-512 range)")
    parent_chunk_tokens: int = Field(default=1200, description="Parent chunk size in tokens")
    chunk_overlap_tokens: int = Field(default=50, description="Overlap between child chunks in tokens")
    top_k: int = Field(default=5, description="Number of top results to retrieve")
    similarity_threshold: float = Field(default=0.3, description="Minimum similarity score for retrieval")
    hybrid_alpha: float = Field(default=0.6, description="Weight for vector vs BM25 (0.6 = 60% vector)")
    # Technique toggles
    rerank_enabled: bool = Field(default=True, description="Enable cross-encoder reranking")
    query_rewriting_enabled: bool = Field(default=True, description="Enable query rewriting")
    rag_fusion_enabled: bool = Field(default=True, description="Enable RAG Fusion")
    self_rag_enabled: bool = Field(default=True, description="Enable Self-RAG evaluation")
    semantic_reranking_enabled: bool = Field(default=True, description="Enable Azure AI Search semantic reranking")


class RoutingSettings(BaseSettings):
    """Alert routing configuration."""

    model_config = SettingsConfigDict(env_prefix="ROUTING_")

    ownership_file: str = Field(default="config/pipeline_ownership.json", description="Pipeline ownership mapping file")
    escalation_timeout_minutes: int = Field(default=30, description="Minutes before auto-escalation")
    severity_thresholds: dict = Field(
        default={"critical": 0, "high": 5, "medium": 30, "low": 120},
        description="Response time thresholds by severity (minutes)",
    )


class AppSettings(BaseSettings):
    """Root application settings composing all sub-configurations."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = Field(default="Alert Whisperer")
    environment: Environment = Field(default=Environment.DEV)
    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")

    # Sub-configurations
    azure: AzureSettings = Field(default_factory=AzureSettings)
    kusto: KustoSettings = Field(default_factory=KustoSettings)
    confluence: ConfluenceSettings = Field(default_factory=ConfluenceSettings)
    icm: ICMSettings = Field(default_factory=ICMSettings)
    log_analytics: LogAnalyticsSettings = Field(default_factory=LogAnalyticsSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    teams: TeamsSettings = Field(default_factory=TeamsSettings)
    rag: RAGSettings = Field(default_factory=RAGSettings)
    azure_search: AzureSearchSettings = Field(default_factory=AzureSearchSettings)
    routing: RoutingSettings = Field(default_factory=RoutingSettings)


def get_settings() -> AppSettings:
    """Factory to create settings instance. Cached at module level."""
    return AppSettings()
