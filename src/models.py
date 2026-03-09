"""
Core Pydantic models for Alert Whisperer.
All domain objects, API contracts, and validation schemas.
Uses Pydantic V2 with strict typing and comprehensive validation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────

class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AlertSource(str, Enum):
    SPARK = "spark"
    SYNAPSE = "synapse"
    KUSTO = "kusto"
    ICM = "icm"
    LOG_ANALYTICS = "log_analytics"
    MANUAL = "manual"


class AlertStatus(str, Enum):
    NEW = "new"
    ACKNOWLEDGED = "acknowledged"
    INVESTIGATING = "investigating"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    CLOSED = "closed"


class PipelineType(str, Enum):
    SPARK_BATCH = "spark_batch"
    SPARK_STREAMING = "spark_streaming"
    SYNAPSE_PIPELINE = "synapse_pipeline"
    KUSTO_INGESTION = "kusto_ingestion"
    KUSTO_QUERY = "kusto_query"


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


# ─────────────────────────────────────────────────
# Alert & Incident Models
# ─────────────────────────────────────────────────

class RawLogEntry(BaseModel):
    """A single raw log line from any source."""
    timestamp: datetime
    source: AlertSource
    level: str = Field(description="Log level: ERROR, WARN, INFO, DEBUG")
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    pipeline_name: Optional[str] = None
    job_id: Optional[str] = None
    cluster_id: Optional[str] = None

    @field_validator("level")
    @classmethod
    def normalize_level(cls, v: str) -> str:
        return v.upper().strip()


class ParsedFailure(BaseModel):
    """Structured representation of a parsed pipeline failure."""
    failure_id: str = Field(description="Unique failure identifier")
    pipeline_name: str
    pipeline_type: PipelineType
    source: AlertSource
    timestamp: datetime
    severity: Severity = Severity.MEDIUM

    # Root cause analysis
    error_class: str = Field(description="Classified error type, e.g. OutOfMemoryError, TimeoutException")
    error_message: str = Field(description="Raw error message from logs")
    root_cause_summary: str = Field(description="Plain-English root cause explanation")
    affected_components: list[str] = Field(default_factory=list)

    # Context
    job_id: Optional[str] = None
    run_id: Optional[str] = None
    cluster_id: Optional[str] = None
    workspace_url: Optional[str] = None
    log_snippet: str = Field(default="", description="Relevant log excerpt (truncated)")

    # Actions
    rerun_url: Optional[str] = None
    log_url: Optional[str] = None
    runbook_url: Optional[str] = None

    # M11: Runbook metadata for enrichment by confluence connector
    runbook_metadata: Optional[dict[str, Any]] = Field(default=None, description="Runbook metadata from Confluence or other sources")

    @field_validator("log_snippet")
    @classmethod
    def truncate_log(cls, v: str) -> str:
        max_len = 5000
        return v[:max_len] + "..." if len(v) > max_len else v


class ICMTicket(BaseModel):
    """Incident from ICM system."""
    ticket_id: str
    title: str
    description: str
    severity: Severity
    status: AlertStatus
    created_at: datetime
    updated_at: Optional[datetime] = None
    assigned_to: Optional[str] = None
    owning_team: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    resolution_notes: Optional[str] = None
    related_pipeline: Optional[str] = None
    ttd_minutes: Optional[float] = Field(None, description="Time to detect in minutes")
    ttm_minutes: Optional[float] = Field(None, description="Time to mitigate in minutes")
    root_cause: Optional[str] = None


class HistoricalIncident(BaseModel):
    """Historical incident for RAG similarity matching."""
    incident_id: str
    title: str
    description: str
    root_cause: str
    resolution: str
    pipeline_name: str
    error_class: str
    severity: Severity
    occurred_at: datetime
    resolved_at: Optional[datetime] = None
    tags: list[str] = Field(default_factory=list)
    similarity_score: float = Field(default=0.0, ge=0.0, le=1.0)


# ─────────────────────────────────────────────────
# Routing Models
# ─────────────────────────────────────────────────

class PipelineOwnership(BaseModel):
    """Ownership metadata for a pipeline."""
    pipeline_name: str
    owner: str
    oncall_channel: str
    escalation_contacts: list[str] = Field(default_factory=list)
    severity_default: Severity = Severity.MEDIUM
    tags: list[str] = Field(default_factory=list)
    runbook_confluence_id: Optional[str] = None


class RoutingDecision(BaseModel):
    """Routing decision for an alert."""
    failure_id: str
    target_channel: str
    target_contacts: list[str]
    severity: Severity
    auto_escalate: bool = False
    escalation_deadline: Optional[datetime] = None
    routing_reason: str = Field(description="Why this routing was chosen")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in routing decision")


class TeamsNotification(BaseModel):
    """Structured Teams notification payload."""
    channel: str
    title: str
    summary: str
    severity: Severity
    sections: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[dict[str, str]] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────
# RAG & Knowledge Base Models
# ─────────────────────────────────────────────────

class DocumentChunk(BaseModel):
    """A chunk of text from a knowledge source."""
    chunk_id: str
    source_type: str = Field(description="confluence | icm | runbook | log")
    source_id: str = Field(description="Original document/page ID")
    source_title: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding: Optional[list[float]] = Field(default=None, exclude=True)


class RetrievalResult(BaseModel):
    """Result from RAG retrieval."""
    chunks: list[DocumentChunk]
    query: str
    total_found: int
    retrieval_time_ms: float
    reranked: bool = False
    metadata: Optional[dict[str, Any]] = Field(default=None, description="Pipeline metadata (e.g. latency budget breakdown)")


class TopicNode(BaseModel):
    """Node in a topic tree for hierarchical retrieval."""
    topic: str
    description: str
    children: list[TopicNode] = Field(default_factory=list)
    relevant_doc_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


TopicNode.model_rebuild()  # Resolve forward reference


# ─────────────────────────────────────────────────
# Conversation Models
# ─────────────────────────────────────────────────

class ChatMessage(BaseModel):
    """A single message in the conversation."""
    role: MessageRole
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)
    tool_calls: Optional[list[dict[str, Any]]] = None


class ConversationContext(BaseModel):
    """Full conversation context for the chat engine."""
    session_id: str
    messages: list[ChatMessage] = Field(default_factory=list)
    active_alert: Optional[ParsedFailure] = None
    similar_incidents: list[HistoricalIncident] = Field(default_factory=list)
    runbook_context: Optional[str] = None
    user_role: str = Field(default="support_engineer")
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def message_count(self) -> int:
        return len(self.messages)

    def add_message(self, role: MessageRole, content: str, **kwargs: Any) -> None:
        self.messages.append(ChatMessage(role=role, content=content, **kwargs))

    def get_recent_messages(self, n: int = 20) -> list[ChatMessage]:
        return self.messages[-n:]


# ─────────────────────────────────────────────────
# Prompt Engineering Models
# ─────────────────────────────────────────────────

class PromptTemplate(BaseModel):
    """A structured prompt template with metadata."""
    name: str
    description: str
    template: str
    variables: list[str] = Field(default_factory=list)
    technique: str = Field(description="Prompting technique: zero_shot, few_shot, cot, tree_of_thought, react")
    version: str = Field(default="1.0")

    def render(self, **kwargs: Any) -> str:
        """Render the template with provided variables."""
        rendered = self.template
        for key, value in kwargs.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
        return rendered


class FewShotExample(BaseModel):
    """A single few-shot example for prompting."""
    input_text: str
    output_text: str
    explanation: Optional[str] = None


class ThoughtBranch(BaseModel):
    """A branch in Tree-of-Thought reasoning."""
    hypothesis: str
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    children: list[ThoughtBranch] = Field(default_factory=list)
    is_solution: bool = False


ThoughtBranch.model_rebuild()


class ReActStep(BaseModel):
    """A single step in ReAct (Reason + Act) prompting."""
    step_number: int
    thought: str
    action: str
    action_input: str
    observation: str = ""


class ReflexionStep(BaseModel):
    """A single iteration of the Reflexion self-refinement loop."""
    iteration: int
    initial_response: str
    reflection: str = Field(description="Self-critique of the initial response")
    refined_response: str = Field(description="Improved response after reflection")
    improvements: list[str] = Field(default_factory=list)
    confidence_delta: float = Field(default=0.0, description="Change in confidence after reflection")


class VerificationClaim(BaseModel):
    """A single claim extracted for Chain-of-Verification."""
    claim: str
    evidence: str = Field(default="")
    verified: bool = False
    correction: Optional[str] = None


class CRAGRelevanceScore(BaseModel):
    """Relevance grading result for CRAG."""
    chunk_id: str
    query: str
    relevance: str = Field(description="One of: correct, incorrect, ambiguous")
    score: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(default="")


class ParentChildChunk(BaseModel):
    """Parent-child chunk relationship for hierarchical chunking."""
    parent_id: str
    parent_content: str
    children: list[DocumentChunk] = Field(default_factory=list)
    source_type: str = ""
    source_id: str = ""
    source_title: str = ""


class GraphNode(BaseModel):
    """A node in the failure graph for GraphRAG."""
    node_id: str
    node_type: str = Field(description="One of: pipeline, error, component, incident, runbook")
    name: str
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """An edge in the failure graph."""
    source_id: str
    target_id: str
    relationship: str = Field(description="e.g. CAUSED_BY, DEPENDS_ON, RESOLVED_BY, SIMILAR_TO")
    weight: float = Field(default=1.0, ge=0.0)
    properties: dict[str, Any] = Field(default_factory=dict)


class FailureGraph(BaseModel):
    """Graph structure for GraphRAG cascading failure analysis."""
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)

    def get_neighbors(self, node_id: str, relationship: Optional[str] = None) -> list[GraphNode]:
        """Get neighboring nodes, optionally filtered by relationship type."""
        neighbor_ids = set()
        for edge in self.edges:
            if edge.source_id == node_id:
                if relationship is None or edge.relationship == relationship:
                    neighbor_ids.add(edge.target_id)
            elif edge.target_id == node_id:
                if relationship is None or edge.relationship == relationship:
                    neighbor_ids.add(edge.source_id)
        return [n for n in self.nodes if n.node_id in neighbor_ids]

    def get_subgraph(self, node_id: str, depth: int = 2) -> "FailureGraph":
        """Extract a subgraph around a node up to given depth."""
        visited: set[str] = set()
        frontier = {node_id}
        relevant_nodes: set[str] = set()

        for _ in range(depth):
            next_frontier: set[str] = set()
            for nid in frontier:
                if nid in visited:
                    continue
                visited.add(nid)
                relevant_nodes.add(nid)
                for edge in self.edges:
                    if edge.source_id == nid:
                        next_frontier.add(edge.target_id)
                    elif edge.target_id == nid:
                        next_frontier.add(edge.source_id)
            frontier = next_frontier

        relevant_nodes |= frontier
        nodes = [n for n in self.nodes if n.node_id in relevant_nodes]
        edges = [e for e in self.edges if e.source_id in relevant_nodes and e.target_id in relevant_nodes]
        return FailureGraph(nodes=nodes, edges=edges)


class AnalysisResult(BaseModel):
    """Final analysis result from the engine."""
    failure: ParsedFailure
    root_cause_analysis: str
    similar_incidents: list[HistoricalIncident]
    recommended_actions: list[str]
    runbook_steps: list[str]
    routing: RoutingDecision
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning_trace: list[str] = Field(default_factory=list, description="Chain-of-thought trace")


# ─────────────────────────────────────────────────
# MCP Protocol Models
# ─────────────────────────────────────────────────

class MCPRequest(BaseModel):
    """Model Context Protocol request structure."""
    jsonrpc: str = "2.0"
    method: str
    params: dict[str, Any] = Field(default_factory=dict)
    id: Optional[str | int] = None


class MCPResponse(BaseModel):
    """Model Context Protocol response structure."""
    jsonrpc: str = "2.0"
    result: Optional[Any] = None
    error: Optional[dict[str, Any]] = None
    id: Optional[str | int] = None

    @property
    def is_error(self) -> bool:
        return self.error is not None


class MCPToolDefinition(BaseModel):
    """MCP tool definition for server capability advertisement."""
    name: str
    description: str
    input_schema: dict[str, Any]


# ─────────────────────────────────────────────────
# Alert Triage / Cluster Models
# ─────────────────────────────────────────────────

class CascadeStep(BaseModel):
    """A single step in a cascading failure chain."""
    pipeline_name: str
    error_class: str
    alert: Optional[ParsedFailure] = None
    relationship: str = Field(default="downstream_effect", description="e.g. downstream_effect, root_cause")


class CascadeCluster(BaseModel):
    """A cluster of alerts linked by a cascading failure chain."""
    root_cause_alert: ParsedFailure
    steps: list[CascadeStep] = Field(default_factory=list)

    def add_step(self, step: CascadeStep) -> None:
        self.steps.append(step)

    @property
    def depth(self) -> int:
        return 1 + len(self.steps)

    @property
    def affected_pipelines(self) -> set[str]:
        pipelines = {self.root_cause_alert.pipeline_name}
        for s in self.steps:
            pipelines.add(s.pipeline_name)
        return pipelines


class TriagedCluster(BaseModel):
    """
    A triaged cluster of related alerts with composite scoring.

    Central model used by AlertTriageEngine and ChatEngine for:
    - Alert grouping and deduplication
    - Composite severity ranking
    - Blast radius tracking
    - Acknowledgment state
    """
    model_config = {"arbitrary_types_allowed": True}

    cluster_id: str
    primary_alert: ParsedFailure
    all_alerts: list[ParsedFailure] = Field(default_factory=list)
    cascade: Optional[CascadeCluster] = None

    # Scoring
    composite_severity: Severity = Severity.MEDIUM
    business_impact_score: float = 0.0
    blast_radius_pipelines: list[str] = Field(default_factory=list)
    blast_radius_components: list[str] = Field(default_factory=list)

    # Assignment
    assigned_to: Optional[str] = None
    assigned_channel: Optional[str] = None

    # Timestamps
    first_seen: Optional[datetime] = None
    last_updated: Optional[datetime] = None

    # AT10: Acknowledgment tracking
    acknowledged: bool = False
    acknowledged_by: Optional[str] = None
    acknowledged_at: Optional[datetime] = None

    @property
    def alert_count(self) -> int:
        return len(self.all_alerts)

    @property
    def is_cascade(self) -> bool:
        return self.cascade is not None and self.cascade.depth > 1

    @property
    def severity_rank(self) -> float:
        """Numeric rank for sorting: severity weight + impact + cascade bonus."""
        sev_weight = {Severity.CRITICAL: 100, Severity.HIGH: 60, Severity.MEDIUM: 30, Severity.LOW: 10}
        rank = sev_weight.get(self.composite_severity, 0)
        rank += self.business_impact_score
        rank += self.alert_count * 2
        if self.is_cascade:
            rank += 50
        return rank

    def add_alert(self, alert: ParsedFailure) -> None:
        self.all_alerts.append(alert)
        self.last_updated = alert.timestamp
        # Upgrade severity if new alert is more severe
        sev_order = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}
        if sev_order.get(alert.severity, 0) > sev_order.get(self.composite_severity, 0):
            self.composite_severity = alert.severity


# ─────────────────────────────────────────────────
# Dashboard / UI Models
# ─────────────────────────────────────────────────

class DashboardMetrics(BaseModel):
    """Aggregated metrics for the dashboard."""
    total_alerts_24h: int = 0
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    avg_ttd_minutes: float = 0.0
    avg_ttm_minutes: float = 0.0
    auto_resolved_count: int = 0
    top_error_classes: list[dict[str, Any]] = Field(default_factory=list)
    alerts_by_pipeline: list[dict[str, Any]] = Field(default_factory=list)
    hourly_trend: list[dict[str, Any]] = Field(default_factory=list)


class AlertFeedItem(BaseModel):
    """A single item in the alert feed."""
    failure: ParsedFailure
    routing: Optional[RoutingDecision] = None
    similar_count: int = 0
    has_runbook: bool = False
    acknowledgment_status: str = "pending"
