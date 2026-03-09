"""
Configuration management using Pydantic Settings.
Loads from .env file and environment variables with validation.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
import time as _time

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
    # M2: Streaming query defaults
    stream_batch_size: int = Field(default=1000, description="Rows per batch when streaming query results (M2)")
    stream_max_batches: int = Field(default=20, description="Maximum batches to fetch when streaming (M2)")
    # M7: Circuit breaker
    circuit_breaker_threshold: int = Field(default=5, description="Consecutive failures before circuit opens (M7)")
    circuit_breaker_cooldown_seconds: float = Field(default=60.0, description="Cooldown before circuit half-opens (M7)")


class ConfluenceSettings(BaseSettings):
    """Confluence MCP Server configuration for runbook retrieval."""

    model_config = SettingsConfigDict(env_prefix="CONFLUENCE_")

    base_url: str = Field(description="Confluence base URL, e.g. https://mycompany.atlassian.net/wiki")
    username: str = Field(description="Confluence username / email")
    api_token: SecretStr = Field(description="Confluence API token")
    mcp_server_url: str = Field(default="http://localhost:3002", description="Confluence MCP Server endpoint")
    space_keys: list[str] = Field(default=["RUNBOOKS", "OPS", "DE"], description="Confluence space keys to search")
    # M1: Pagination defaults
    search_max_pages: int = Field(default=5, description="Max API pages to fetch per search call (M1)")
    search_page_size: int = Field(default=25, description="Results per API page for search (M1)")
    # M7: Circuit breaker
    circuit_breaker_threshold: int = Field(default=5, description="Consecutive failures before circuit opens (M7)")
    circuit_breaker_cooldown_seconds: float = Field(default=60.0, description="Cooldown before circuit half-opens (M7)")


class ICMSettings(BaseSettings):
    """ICM (Incident Management) MCP Server configuration."""

    model_config = SettingsConfigDict(env_prefix="ICM_")

    mcp_server_url: str = Field(default="http://localhost:3003", description="ICM MCP Server endpoint")
    api_base_url: str = Field(default="", description="ICM REST API base URL")
    api_key: SecretStr = Field(default=SecretStr(""), description="ICM API key if required")
    poll_interval_seconds: int = Field(default=30, description="Polling interval for new incidents")
    # M7: Circuit breaker
    circuit_breaker_threshold: int = Field(default=5, description="Consecutive failures before circuit opens (M7)")
    circuit_breaker_cooldown_seconds: float = Field(default=60.0, description="Cooldown before circuit half-opens (M7)")


class LogAnalyticsSettings(BaseSettings):
    """Azure Log Analytics MCP Server configuration."""

    model_config = SettingsConfigDict(env_prefix="LOG_ANALYTICS_")

    workspace_id: str = Field(description="Log Analytics Workspace ID")
    mcp_server_url: str = Field(default="http://localhost:3004", description="Log Analytics MCP Server endpoint")
    default_timespan: str = Field(default="P1D", description="Default query timespan (ISO 8601 duration)")
    # M7: Circuit breaker
    circuit_breaker_threshold: int = Field(default=5, description="Consecutive failures before circuit opens (M7)")
    circuit_breaker_cooldown_seconds: float = Field(default=60.0, description="Cooldown before circuit half-opens (M7)")
    indexing_batch_size: int = Field(
        default=10000,
        description=(
            "Number of log records to fetch per batch during RAG indexing. "
            "Production default is 10000; increase for larger workspaces. "
            "Controlled by LOG_ANALYTICS_INDEXING_BATCH_SIZE env var."
        ),
    )
    indexing_max_batches: int = Field(
        default=10,
        description=(
            "Maximum number of pagination batches during a single indexing run. "
            "Total records indexed = indexing_batch_size * indexing_max_batches. "
            "Controlled by LOG_ANALYTICS_INDEXING_MAX_BATCHES env var."
        ),
    )
    volume_warn_threshold: int = Field(
        default=50000,
        description=(
            "If a count query estimates more than this many rows, emit a warning "
            "before fetching to prevent overwhelming the indexing pipeline. "
            "Controlled by LOG_ANALYTICS_VOLUME_WARN_THRESHOLD env var."
        ),
    )
    volume_hard_limit: int = Field(
        default=500000,
        description=(
            "Hard cap on rows returned from any single query. Results are "
            "truncated to this value to protect downstream memory. "
            "Controlled by LOG_ANALYTICS_VOLUME_HARD_LIMIT env var."
        ),
    )


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
    # P8: Single configurable temperature for DSPy optimizer — overrides all per-signature
    # hardcoded values. Set via LLM_DSPY_OPTIMIZATION_TEMPERATURE environment variable.
    dspy_optimization_temperature: float = Field(
        default=0.1,
        description=(
            "P8: Normalized temperature used by the DSPy prompt optimizer for all technique "
            "signatures. Eliminates the 0.1/0.2/0.3 inconsistency. Should match `temperature` "
            "unless you intentionally want lower variance during optimization."
        ),
    )
    # P12: LLM call timeout in seconds. Set via LLM_TIMEOUT_SECONDS.
    timeout_seconds: float = Field(
        default=30.0,
        description=(
            "P12: Maximum seconds to wait for an LLM API response before raising TimeoutError. "
            "Set to 0 to disable timeout enforcement."
        ),
    )
    # P11: Maximum token budget for conversation history in prompts.
    conversation_token_budget: int = Field(
        default=8000,
        description=(
            "P11: Maximum number of tokens (approx. chars/4) to keep in conversation history "
            "when building conversational prompts. Older messages are dropped first."
        ),
    )
    # PE3: Minimum number of feedback samples before DSPy optimization is triggered.
    dspy_min_optimization_samples: int = Field(
        default=20,
        description=(
            "PE3: Minimum feedback samples required before DSPy optimizer updates a "
            "prompt signature. Prevents premature optimization on sparse data."
        ),
    )
    # PE5: Prompt template cache TTL in seconds.
    prompt_cache_ttl_seconds: int = Field(
        default=3600,
        description="PE5: Prompt template cache TTL in seconds. Set to 0 to disable caching.",
    )


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
    use_dedicated_reranker: bool = Field(
        default=False,
        description="Use dedicated cross-encoder model (ms-marco-MiniLM-L-12-v2) instead of LLM-as-reranker. "
                    "Lower latency (~150-300ms vs 2-5s for 10 chunks) but requires sentence-transformers.",
    )
    dedicated_reranker_model: str = Field(
        default="cross-encoder/ms-marco-MiniLM-L-12-v2",
        description="HuggingFace model ID or local path for the dedicated cross-encoder",
    )
    query_rewriting_enabled: bool = Field(default=True, description="Enable query rewriting")
    rag_fusion_enabled: bool = Field(default=True, description="Enable RAG Fusion")
    self_rag_enabled: bool = Field(default=True, description="Enable Self-RAG evaluation")
    semantic_reranking_enabled: bool = Field(default=True, description="Enable Azure AI Search semantic reranking")
    semantic_chunking_enabled: bool = Field(
        default=True,
        description="Enable semantic chunking (embedding-similarity boundary detection). "
                    "Falls back to token-based chunking when embedding client is unavailable.",
    )
    semantic_chunking_percentile: float = Field(
        default=25.0,
        description="Percentile cutoff for semantic chunking breakpoint detection (lower = fewer, larger chunks)",
    )
    rag_fusion_k: int = Field(
        default=60,
        description="Reciprocal Rank Fusion constant k (higher = less rank sensitivity). "
                    "Standard value is 60 (original RRF paper). Increase for large result sets.",
    )
    cross_encoder_latency_budget_ms: int = Field(
        default=3000,
        description="Max latency budget in milliseconds for LLM-as-reranker cross-encoder path. "
                    "Calls exceeding this limit fall back to heuristic reranking.",
    )
    self_rag_runbook_threshold: float = Field(
        default=0.4,
        description="Self-RAG relevance threshold for runbook (confluence) source type. "
                    "Lower than default since partial runbook matches are valuable.",
    )
    self_rag_incident_threshold: float = Field(
        default=0.7,
        description="Self-RAG relevance threshold for incident (icm) source type. "
                    "Stricter than default to avoid false positive incident matches.",
    )
    parent_overlap_tokens: int = Field(
        default=100,
        description="Token overlap between parent chunks to avoid losing context at boundaries.",
    )
    # Round 2.2: Resilience settings
    embedding_retry_max_attempts: int = Field(
        default=3,
        description="Maximum retry attempts for embedding API calls.",
    )
    embedding_retry_base_delay: float = Field(
        default=1.0,
        description="Base delay (seconds) for exponential backoff on embedding retries.",
    )
    search_retry_max_attempts: int = Field(
        default=3,
        description="Maximum retry attempts for Azure AI Search calls.",
    )
    search_retry_base_delay: float = Field(
        default=0.5,
        description="Base delay (seconds) for exponential backoff on search retries.",
    )
    search_circuit_breaker_threshold: int = Field(
        default=5,
        description="Consecutive search failures before circuit breaker opens.",
    )
    search_circuit_breaker_cooldown: float = Field(
        default=60.0,
        description="Seconds before circuit breaker transitions from OPEN to HALF_OPEN.",
    )
    end_to_end_latency_budget_ms: int = Field(
        default=5000,
        description="End-to-end latency budget (ms) for a complete RAG pipeline request. "
                    "Tracked across embedding, search, reranking, and LLM generation phases.",
    )
    # Round 2.2: Guardrail settings
    max_response_chars: int = Field(
        default=15000,
        description="Maximum characters in a single LLM response before truncation.",
    )
    enable_pii_filter: bool = Field(
        default=True,
        description="Enable PII detection and redaction in LLM outputs.",
    )
    llm_json_max_retries: int = Field(
        default=2,
        description="Additional retries when LLM returns malformed JSON (on top of base retries).",
    )


class RoutingSettings(BaseSettings):
    """Alert routing configuration."""

    model_config = SettingsConfigDict(env_prefix="ROUTING_")

    ownership_file: str = Field(default="config/pipeline_ownership.json", description="Pipeline ownership mapping file")
    escalation_timeout_minutes: int = Field(default=30, description="Minutes before auto-escalation")
    severity_thresholds: dict = Field(
        default={"critical": 0, "high": 5, "medium": 30, "low": 120},
        description="Response time thresholds by severity (minutes)",
    )


class ChatEngineSettings(BaseSettings):
    """Chat engine session and routing configuration."""

    model_config = SettingsConfigDict(env_prefix="CHAT_")

    response_cache_ttl_seconds: int = Field(default=300, description="TTL for response cache entries")
    response_cache_max_size: int = Field(default=200, description="Maximum cached responses")
    session_ttl_seconds: int = Field(default=3600, description="Session TTL in seconds")
    max_sessions: int = Field(default=100, description="Maximum concurrent sessions")
    min_routing_confidence: float = Field(default=0.3, description="Minimum confidence for intent routing")
    context_window_size: int = Field(default=20, description="Number of recent messages to include in context")
    cove_enabled: bool = Field(default=True, description="Enable Chain-of-Verification on responses")


class AlertTriageSettings(BaseSettings):
    """Alert triage engine configuration."""

    model_config = SettingsConfigDict(env_prefix="ALERT_TRIAGE_")

    max_alert_history: int = Field(default=10000, description="Maximum alerts to keep in memory")
    cascade_window_multiplier: float = Field(default=2.0, description="Multiplier on time window for cascade detection")
    severity_decay_minutes: int = Field(default=60, description="Minutes before severity can decay")


class DataRefreshSettings(BaseSettings):
    """Data refresh engine configuration."""

    model_config = SettingsConfigDict(env_prefix="DATA_REFRESH_")

    max_seen_alerts: int = Field(default=50000, description="Maximum seen-alert fingerprints to track")
    parallel_polling: bool = Field(default=True, description="Poll MCP sources in parallel")


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
    chat_engine: ChatEngineSettings = Field(default_factory=ChatEngineSettings)
    alert_triage: AlertTriageSettings = Field(default_factory=AlertTriageSettings)
    data_refresh: DataRefreshSettings = Field(default_factory=DataRefreshSettings)


# O7: Module-level cache for hot-reload support
_settings_cache: Optional[AppSettings] = None
_settings_mtime: float = 0.0


def get_settings(force_reload: bool = False) -> AppSettings:
    """O7: Factory with hot-reload support.

    Returns a cached AppSettings instance. Checks the .env file mtime on
    every call — if the file has changed since the last load, the settings
    are reloaded automatically. Pass force_reload=True to always reload.
    """
    global _settings_cache, _settings_mtime

    if force_reload or _settings_cache is None:
        _settings_cache = AppSettings()
        _settings_mtime = _time.time()
        return _settings_cache

    # Hot-reload: check if .env file has changed
    import os
    env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
    try:
        current_mtime = os.path.getmtime(env_path)
        if current_mtime > _settings_mtime:
            _settings_cache = AppSettings()
            _settings_mtime = current_mtime
    except OSError:
        pass  # .env not found or not readable — keep cached settings

    return _settings_cache
