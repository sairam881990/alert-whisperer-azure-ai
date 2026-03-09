"""
Chat Engine — Conversational troubleshooting interface.

Manages the interactive Q&A experience for the support team:
- Context-aware conversation with RAG
- Tool-augmented responses (run queries, check logs, find incidents)
- Session management with alert binding
- Active prompting with uncertainty detection
- FLARE for active retrieval during generation
- Reflexion for iterative self-refinement
- Chain-of-Verification for factual safety
- GraphRAG for blast radius tracing

Enhanced with alert-free modes:
- Proactive: system surfaces critical clusters on session start
- Discovery: user asks "what failed?" without selecting an alert
- Search: keyword/severity/pipeline search across all alerts
- Cross-alert analytics: patterns, trends, recurring errors
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Optional

import structlog

from src.engine.alert_processor import AlertProcessor
from src.models import (
    AnalysisResult,
    ChatMessage,
    ConversationContext,
    MessageRole,
    ParsedFailure,
    Severity,
)
from src.prompts.prompt_engine import PromptEngine
from src.rag.retriever import RAGRetriever

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────
# Intent Classification
# ─────────────────────────────────────────────────

class UserIntent:
    """Classified user intent for routing to the correct handler."""
    SIMILAR_INCIDENTS = "similar_incidents"
    RUNBOOK_LOOKUP = "runbook_lookup"
    ROOT_CAUSE_DETAIL = "root_cause_detail"
    RERUN_PIPELINE = "rerun_pipeline"
    VIEW_LOGS = "view_logs"
    QUERY_DATA = "query_data"
    GENERAL_QA = "general_qa"
    ESCALATION = "escalation"
    STATUS_UPDATE = "status_update"
    TREND_ANALYSIS = "trend_analysis"
    BLAST_RADIUS = "blast_radius"

    # New alert-free intents
    ALERT_DISCOVERY = "alert_discovery"
    PLATFORM_HEALTH = "platform_health"
    ALERT_SEARCH = "alert_search"
    CROSS_ALERT_ANALYTICS = "cross_alert_analytics"


INTENT_PATTERNS = {
    UserIntent.SIMILAR_INCIDENTS: [
        r"similar\s+(?:failures?|incidents?|errors?|issues?)",
        r"happened\s+before",
        r"seen\s+this\s+before",
        r"past\s+(?:\d+\s+)?days",
        r"historical",
        r"recurrence",
    ],
    UserIntent.RUNBOOK_LOOKUP: [
        r"runbook",
        r"procedure",
        r"steps?\s+to\s+(?:fix|resolve|mitigate)",
        r"how\s+(?:do\s+(?:I|we)|to)\s+(?:fix|resolve|handle)",
        r"troubleshoot",
        r"playbook",
    ],
    UserIntent.ROOT_CAUSE_DETAIL: [
        r"root\s+cause",
        r"why\s+(?:did|does|is)",
        r"what\s+(?:caused|happened|went\s+wrong)",
        r"explain\s+(?:the\s+)?(?:error|failure)",
        r"more\s+detail",
        r"deep(?:er)?\s+dive",
    ],
    UserIntent.RERUN_PIPELINE: [
        r"rerun",
        r"re-run",
        r"restart",
        r"retry",
        r"trigger\s+(?:again|the\s+pipeline)",
    ],
    UserIntent.VIEW_LOGS: [
        r"show\s+(?:me\s+)?(?:the\s+)?logs?",
        r"log\s+(?:entries|lines|output)",
        r"stack\s+trace",
        r"error\s+(?:log|output|trace)",
    ],
    UserIntent.QUERY_DATA: [
        r"(?:run|execute)\s+(?:a\s+)?(?:query|kql|kusto)",
        r"check\s+(?:the\s+)?(?:data|table|ingestion)",
        r"query\s+(?:for|the)",
        r"show\s+me\s+(?:the\s+)?data",
    ],
    UserIntent.ESCALATION: [
        r"escalat",
        r"page\s+(?:someone|the\s+team)",
        r"need\s+help",
        r"critical",
        r"urgent",
    ],
    UserIntent.STATUS_UPDATE: [
        r"status",
        r"what'?s\s+(?:the\s+)?(?:current|latest)",
        r"update",
        r"progress",
    ],
    UserIntent.TREND_ANALYSIS: [
        r"trend",
        r"pattern",
        r"frequency",
        r"how\s+often",
        r"increasing|decreasing",
    ],
    UserIntent.BLAST_RADIUS: [
        r"blast\s*radius",
        r"what.*affected",
        r"downstream\s+(?:impact|effect)",
        r"depend(?:ency|encies|ent)",
        r"impact\s+analysis",
    ],
    # New alert-free intents
    UserIntent.ALERT_DISCOVERY: [
        r"what\s+(?:failed|broke|is\s+broken|is\s+failing)",
        r"what'?s\s+broken",
        r"any\s+(?:failures?|errors?|issues?)",
        r"show\s+(?:me\s+)?(?:all\s+)?(?:active\s+)?alerts?",
        r"what'?s\s+(?:going\s+on|happening)",
        r"recent\s+(?:failures?|errors?|alerts?)",
    ],
    UserIntent.PLATFORM_HEALTH: [
        r"(?:platform|system|overall)\s+health",
        r"is\s+(?:everything|the\s+platform)\s+(?:ok|healthy|up)",
        r"health\s+(?:check|status|report)",
        r"is\s+\w+\s+healthy",
        r"how\s+is\s+\w+\s+doing",
        r"system\s+status",
    ],
    UserIntent.ALERT_SEARCH: [
        r"search\s+(?:for\s+)?(?:alerts?|failures?|errors?)",
        r"find\s+(?:alerts?|failures?|errors?)",
        r"(?:show|list)\s+(?:me\s+)?(?:all\s+)?(?:critical|high|medium|low)\s+alerts?",
        r"alerts?\s+(?:for|from|about|in)\s+",
        r"(?:filter|look\s+for)\s+",
    ],
    UserIntent.CROSS_ALERT_ANALYTICS: [
        r"compare\s+(?:yesterday|today|last\s+week)",
        r"analytics",
        r"(?:error|alert|failure)\s+(?:distribution|breakdown|summary)",
        r"(?:recurring|repeated)\s+(?:errors?|failures?|patterns?)",
        r"top\s+(?:errors?|failures?|issues?)",
        r"(?:how\s+many|count)\s+(?:alerts?|errors?|failures?)",
    ],
}


def classify_intent(message: str) -> str:
    """
    Classify user intent using regex pattern matching.
    Returns the most likely intent or GENERAL_QA as fallback.

    Priority: alert-free intents are checked with a slight bonus
    to avoid misrouting discovery queries to alert-bound handlers.
    """
    message_lower = message.lower()
    scores: dict[str, int] = {}

    # Alert-free intents get a small priority bonus to avoid
    # "what's broken?" being routed to STATUS_UPDATE (which requires active_alert)
    alert_free_intents = {
        UserIntent.ALERT_DISCOVERY,
        UserIntent.PLATFORM_HEALTH,
        UserIntent.ALERT_SEARCH,
        UserIntent.CROSS_ALERT_ANALYTICS,
    }

    for intent, patterns in INTENT_PATTERNS.items():
        score = sum(1 for p in patterns if re.search(p, message_lower))
        if score > 0:
            # Small bonus for alert-free intents to break ties
            if intent in alert_free_intents:
                score += 0.5
            scores[intent] = score

    if scores:
        return max(scores, key=scores.get)
    return UserIntent.GENERAL_QA


class EmbeddingIntentClassifier:
    """
    Embedding-based intent classification using cosine similarity.

    Why embedding-based intent classification replaces regex:
    - Regex misses paraphrases: "what went wrong" matches but "explain the
      issue" doesn't, even though both mean ROOT_CAUSE_DETAIL
    - Support engineers use varied vocabulary: "show me what broke" vs
      "root cause analysis" vs "why did it fail" all mean the same thing
    - Regex can't handle typos or abbreviations common in fast-paced
      incident response ("similiar incidents", "rca", "runbk")
    - New intent patterns require code changes with regex; with embeddings,
      just add example utterances to the training set

    Falls back to regex classify_intent() when embeddings are unavailable.
    """

    # Representative utterances for each intent (used as embedding anchors)
    INTENT_EXEMPLARS: dict[str, list[str]] = {
        UserIntent.SIMILAR_INCIDENTS: [
            "show me similar failures",
            "has this happened before",
            "find past incidents like this",
            "any recurring patterns",
            "historical occurrences of this error",
        ],
        UserIntent.RUNBOOK_LOOKUP: [
            "what are the runbook steps",
            "how do I fix this",
            "troubleshooting procedure",
            "resolution steps for this error",
            "what's the playbook",
        ],
        UserIntent.ROOT_CAUSE_DETAIL: [
            "what caused this failure",
            "explain the root cause",
            "why did the pipeline fail",
            "deep dive into the error",
            "what went wrong",
        ],
        UserIntent.RERUN_PIPELINE: [
            "rerun the pipeline",
            "restart the job",
            "retry the failed task",
            "trigger the pipeline again",
            "re-execute",
        ],
        UserIntent.VIEW_LOGS: [
            "show me the logs",
            "view the stack trace",
            "error log output",
            "display log entries",
            "what do the logs say",
        ],
        UserIntent.ESCALATION: [
            "I need to escalate this",
            "page the on-call team",
            "this is urgent",
            "create an incident ticket",
            "need immediate help",
        ],
        UserIntent.TREND_ANALYSIS: [
            "show me the trend",
            "how often does this happen",
            "error frequency over time",
            "is this increasing",
            "pattern analysis",
        ],
        UserIntent.BLAST_RADIUS: [
            "what's the blast radius",
            "which pipelines are affected",
            "downstream impact",
            "dependency analysis",
            "what else will break",
        ],
        # New alert-free intents
        UserIntent.ALERT_DISCOVERY: [
            "what's broken right now",
            "what failed in the last hour",
            "any active failures",
            "show me current issues",
            "what's going on with the pipelines",
        ],
        UserIntent.PLATFORM_HEALTH: [
            "is the platform healthy",
            "system health check",
            "is spark_etl_daily_ingest healthy",
            "how is the platform doing",
            "overall system status",
        ],
        UserIntent.ALERT_SEARCH: [
            "search for OOM alerts",
            "find all critical alerts",
            "show me alerts for spark_etl",
            "list high severity failures",
            "filter alerts by pipeline",
        ],
        UserIntent.CROSS_ALERT_ANALYTICS: [
            "compare yesterday vs today",
            "what are the top recurring errors",
            "alert distribution by severity",
            "how many failures this week",
            "error pattern analysis across pipelines",
        ],
    }

    def __init__(self, embedding_fn: Any = None):
        """
        Args:
            embedding_fn: Callable that takes a list of strings and returns
                          a list of embedding vectors (list[list[float]]).
                          If None, falls back to regex classification.
        """
        self._embedding_fn = embedding_fn
        self._intent_embeddings: dict[str, list[list[float]]] = {}
        self._initialized = False

        if embedding_fn:
            self._precompute_intent_embeddings()

    def _precompute_intent_embeddings(self) -> None:
        """Pre-compute embeddings for all intent exemplars."""
        try:
            for intent, exemplars in self.INTENT_EXEMPLARS.items():
                self._intent_embeddings[intent] = self._embedding_fn(exemplars)
            self._initialized = True
            logger.info("embedding_intent_classifier_ready", intents=len(self._intent_embeddings))
        except Exception as e:
            logger.warning("embedding_intent_classifier_init_failed", error=str(e))
            self._initialized = False

    def classify(self, message: str) -> str:
        """
        Classify intent using embedding cosine similarity.
        Falls back to regex if embeddings are unavailable.
        """
        if not self._initialized or not self._embedding_fn:
            return classify_intent(message)

        try:
            query_embedding = self._embedding_fn([message])[0]

            best_intent = UserIntent.GENERAL_QA
            best_score = -1.0

            for intent, exemplar_embeddings in self._intent_embeddings.items():
                for emb in exemplar_embeddings:
                    sim = self._cosine_similarity(query_embedding, emb)
                    if sim > best_score:
                        best_score = sim
                        best_intent = intent

            # Require minimum similarity threshold
            if best_score < 0.45:
                return UserIntent.GENERAL_QA

            logger.debug(
                "embedding_intent_classified",
                intent=best_intent,
                confidence=round(best_score, 3),
            )
            return best_intent

        except Exception as e:
            logger.warning("embedding_intent_fallback", error=str(e))
            return classify_intent(message)

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


# ─────────────────────────────────────────────────
# Chat Engine (Enhanced)
# ─────────────────────────────────────────────────

class ChatEngine:
    """
    Manages conversational troubleshooting sessions.

    Each session can be bound to an active alert, providing
    context-aware responses with RAG retrieval.

    Enhanced with:
    - FLARE for active mid-generation retrieval
    - Reflexion for iterative self-refinement on root cause queries
    - Chain-of-Verification for high-severity alert analyses
    - GraphRAG for blast radius tracing and dependency analysis
    - Agentic RAG for intelligent multi-source retrieval
    - Alert-free modes: discovery, health checks, search, analytics
    """

    def __init__(
        self,
        prompt_engine: PromptEngine,
        rag_retriever: RAGRetriever,
        alert_processor: AlertProcessor,
        intent_classifier: Optional[EmbeddingIntentClassifier] = None,
        triage_engine: Any = None,  # AlertTriageEngine
    ):
        self.prompt_engine = prompt_engine
        self.rag_retriever = rag_retriever
        self.alert_processor = alert_processor
        self.intent_classifier = intent_classifier
        self.triage_engine = triage_engine
        self._sessions: dict[str, ConversationContext] = {}

    def create_session(
        self,
        active_alert: Optional[ParsedFailure] = None,
        user_role: str = "support_engineer",
    ) -> str:
        """Create a new chat session, optionally bound to an alert."""
        session_id = str(uuid.uuid4())[:12]
        context = ConversationContext(
            session_id=session_id,
            active_alert=active_alert,
            user_role=user_role,
        )

        greeting = self._build_greeting(active_alert)
        context.add_message(MessageRole.ASSISTANT, greeting)

        self._sessions[session_id] = context
        logger.info("chat_session_created", session_id=session_id, has_alert=active_alert is not None)
        return session_id

    def get_session(self, session_id: str) -> Optional[ConversationContext]:
        """Retrieve a chat session."""
        return self._sessions.get(session_id)

    def bind_alert_to_session(
        self,
        session_id: str,
        alert: ParsedFailure,
        analysis: Optional[AnalysisResult] = None,
    ) -> None:
        """Bind an alert to an existing session."""
        session = self._sessions.get(session_id)
        if session:
            session.active_alert = alert
            if analysis:
                session.similar_incidents = analysis.similar_incidents
                session.runbook_context = "\n".join(analysis.runbook_steps)

    async def process_message(
        self,
        session_id: str,
        user_message: str,
    ) -> str:
        """
        Process a user message and return the assistant's response.

        Enhanced pipeline:
        1. Classify intent
        2. Route to specialized handler (with advanced techniques)
        3. Apply FLARE if confidence is low
        4. Apply Chain-of-Verification for critical responses
        5. Post-process and return

        Alert-free intents route to handlers that work WITHOUT active_alert.
        """
        session = self._sessions.get(session_id)
        if not session:
            return "Session not found. Please start a new conversation."

        session.add_message(MessageRole.USER, user_message)

        if self.intent_classifier:
            intent = self.intent_classifier.classify(user_message)
        else:
            intent = classify_intent(user_message)
        logger.info("intent_classified", session_id=session_id, intent=intent)

        # Route to handler — alert-free intents first
        if intent == UserIntent.ALERT_DISCOVERY:
            response = await self._handle_alert_discovery(session, user_message)
        elif intent == UserIntent.PLATFORM_HEALTH:
            response = await self._handle_platform_health(session, user_message)
        elif intent == UserIntent.ALERT_SEARCH:
            response = await self._handle_alert_search(session, user_message)
        elif intent == UserIntent.CROSS_ALERT_ANALYTICS:
            response = await self._handle_cross_alert_analytics(session, user_message)
        # Alert-bound intents
        elif intent == UserIntent.SIMILAR_INCIDENTS:
            response = await self._handle_similar_incidents(session, user_message)
        elif intent == UserIntent.RUNBOOK_LOOKUP:
            response = await self._handle_runbook_lookup(session, user_message)
        elif intent == UserIntent.ROOT_CAUSE_DETAIL:
            response = await self._handle_root_cause_detail(session, user_message)
        elif intent == UserIntent.VIEW_LOGS:
            response = await self._handle_view_logs(session, user_message)
        elif intent == UserIntent.RERUN_PIPELINE:
            response = self._handle_rerun_request(session)
        elif intent == UserIntent.ESCALATION:
            response = await self._handle_escalation(session, user_message)
        elif intent == UserIntent.TREND_ANALYSIS:
            response = await self._handle_trend_analysis(session, user_message)
        elif intent == UserIntent.BLAST_RADIUS:
            response = await self._handle_blast_radius(session, user_message)
        else:
            response = await self._handle_general_qa(session, user_message)

        # FLARE: Check if response needs supplemental retrieval
        # Only triggers when confidence is genuinely low — not on casual hedging
        if self.rag_retriever.flare:
            segments = self.rag_retriever.flare.detect_low_confidence_segments(response)
            # Require at least 2 low-confidence segments AND they must cover
            # a meaningful fraction of the response (>5% of characters)
            min_segments = 2
            min_uncertainty_ratio = 0.05
            total_segment_chars = sum(len(s) for s in segments)
            uncertainty_ratio = total_segment_chars / max(len(response), 1)

            if len(segments) >= min_segments and uncertainty_ratio >= min_uncertainty_ratio:
                flare_result = await self.rag_retriever.flare.retrieve_for_segments(
                    segments, existing_context=response
                )
                if flare_result and flare_result.chunks:
                    additional_context = await self.rag_retriever.build_context_string(flare_result.chunks)
                    response += (
                        "\n\n---\n"
                        "**Additional Context** (retrieved via active follow-up):\n"
                        f"{additional_context[:1000]}"
                    )
                    logger.info(
                        "flare_augmented_response",
                        segments_detected=len(segments),
                        uncertainty_ratio=round(uncertainty_ratio, 3),
                        chunks_added=len(flare_result.chunks),
                    )
            elif segments:
                logger.debug(
                    "flare_skipped_below_threshold",
                    segments=len(segments),
                    uncertainty_ratio=round(uncertainty_ratio, 3),
                )

        session.add_message(MessageRole.ASSISTANT, response)
        return response

    async def process_message_streaming(
        self,
        session_id: str,
        user_message: str,
    ):
        """
        Process a user message with streaming support and async FLARE.

        Yields partial response chunks for real-time UI updates.
        FLARE retrieval happens asynchronously between generation chunks.

        Why async FLARE streaming for Spark/Synapse/Kusto troubleshooting:
        - Support engineers need immediate feedback during high-severity incidents
        - FLARE retrieval can run concurrently with response display
        - Streaming shows progressive investigation steps, building confidence
        - Mid-stream retrieval results appear as they become available
        """
        session = self._sessions.get(session_id)
        if not session:
            yield "Session not found. Please start a new conversation."
            return

        session.add_message(MessageRole.USER, user_message)

        if self.intent_classifier:
            intent = self.intent_classifier.classify(user_message)
        else:
            intent = classify_intent(user_message)

        logger.info("streaming_intent_classified", session_id=session_id, intent=intent)

        # Generate initial response — alert-free intents first
        if intent == UserIntent.ALERT_DISCOVERY:
            response = await self._handle_alert_discovery(session, user_message)
        elif intent == UserIntent.PLATFORM_HEALTH:
            response = await self._handle_platform_health(session, user_message)
        elif intent == UserIntent.ALERT_SEARCH:
            response = await self._handle_alert_search(session, user_message)
        elif intent == UserIntent.CROSS_ALERT_ANALYTICS:
            response = await self._handle_cross_alert_analytics(session, user_message)
        elif intent == UserIntent.SIMILAR_INCIDENTS:
            response = await self._handle_similar_incidents(session, user_message)
        elif intent == UserIntent.RUNBOOK_LOOKUP:
            response = await self._handle_runbook_lookup(session, user_message)
        elif intent == UserIntent.ROOT_CAUSE_DETAIL:
            response = await self._handle_root_cause_detail(session, user_message)
        elif intent == UserIntent.VIEW_LOGS:
            response = await self._handle_view_logs(session, user_message)
        elif intent == UserIntent.RERUN_PIPELINE:
            response = self._handle_rerun_request(session)
        elif intent == UserIntent.ESCALATION:
            response = await self._handle_escalation(session, user_message)
        elif intent == UserIntent.TREND_ANALYSIS:
            response = await self._handle_trend_analysis(session, user_message)
        elif intent == UserIntent.BLAST_RADIUS:
            response = await self._handle_blast_radius(session, user_message)
        else:
            response = await self._handle_general_qa(session, user_message)

        # Yield initial response
        yield response

        # Async FLARE: retrieve supplemental context in background
        if self.rag_retriever.flare:
            segments = self.rag_retriever.flare.detect_low_confidence_segments(response)
            min_segments = 2
            total_segment_chars = sum(len(s) for s in segments)
            uncertainty_ratio = total_segment_chars / max(len(response), 1)

            if len(segments) >= min_segments and uncertainty_ratio >= 0.05:
                flare_result = await self.rag_retriever.flare.retrieve_for_segments(
                    segments, existing_context=response
                )
                if flare_result and flare_result.chunks:
                    additional_context = await self.rag_retriever.build_context_string(
                        flare_result.chunks
                    )
                    flare_supplement = (
                        "\n\n---\n"
                        "**Additional Context** (retrieved via active follow-up):\n"
                        f"{additional_context[:1000]}"
                    )
                    response += flare_supplement
                    yield flare_supplement
                    logger.info(
                        "flare_streaming_augmented",
                        segments=len(segments),
                        chunks_added=len(flare_result.chunks),
                    )

        session.add_message(MessageRole.ASSISTANT, response)

    # ─── Alert-Free Intent Handlers ──────────────

    async def _handle_alert_discovery(
        self,
        session: ConversationContext,
        message: str,
    ) -> str:
        """
        Handle discovery queries: "What's broken?", "What failed in the last hour?"
        Works WITHOUT an active_alert — queries the triage engine directly.
        """
        if not self.triage_engine:
            return (
                "Alert triage is not configured. "
                "Please select an alert from the sidebar, or try connecting to your MCP sources."
            )

        # Parse time window from message
        hours = self._extract_hours(message) or 1

        # Get top clusters from triage engine
        top_clusters = self.triage_engine.get_top_clusters(limit=5)
        critical = self.triage_engine.get_critical_clusters()

        if not top_clusters:
            return f"No active alerts found in the last {hours} hour(s). All pipelines appear healthy."

        parts = [f"**Active Issues** (last {hours}h):\n"]

        for i, cluster in enumerate(top_clusters, 1):
            cascade_tag = " ⛓️ CASCADE" if cluster.is_cascade else ""
            parts.append(
                f"**{i}. {cluster.primary_alert.pipeline_name}** — "
                f"{cluster.primary_alert.error_class}\n"
                f"   Severity: {cluster.composite_severity.value.upper()}{cascade_tag} | "
                f"Alerts: {cluster.alert_count}"
            )
            if cluster.blast_radius_pipelines:
                parts.append(
                    f"   Blast radius: {', '.join(cluster.blast_radius_pipelines[:3])}"
                )
            parts.append("")

        parts.append(
            f"**Summary:** {len(top_clusters)} active clusters, "
            f"{len(critical)} critical/high severity.\n"
            "Select an alert for detailed investigation, or ask "
            "\"tell me more about [pipeline]\" for specifics."
        )
        return "\n".join(parts)

    async def _handle_platform_health(
        self,
        session: ConversationContext,
        message: str,
    ) -> str:
        """
        Handle platform health queries: "Is the platform healthy?",
        "Is spark_etl_daily_ingest healthy?"
        Works WITHOUT an active_alert.
        """
        if not self.triage_engine:
            return (
                "Alert triage is not configured. "
                "Connect your MCP sources in Settings to enable platform health monitoring."
            )

        # Check if asking about a specific pipeline
        pipeline_name = self._extract_pipeline_name(message)

        if pipeline_name:
            health = self.triage_engine.get_pipeline_health(pipeline_name)
            status = health.get("status", "unknown")
            status_icon = {"critical": "🔴", "degraded": "🟠", "elevated": "🟡", "warning": "🟡", "healthy": "🟢"}.get(
                status, "⚪"
            )
            parts = [f"**Pipeline Health: {pipeline_name}**\n"]
            parts.append(f"Status: {status_icon} {status.upper()}")
            if health.get("alert_count_24h"):
                parts.append(f"Alerts (24h): {health['alert_count_24h']}")
            if health.get("latest_error"):
                parts.append(f"Latest error: {health['latest_error']}")
            if health.get("last_alert"):
                parts.append(f"Last alert: {health['last_alert'].strftime('%H:%M UTC')}")
            return "\n".join(parts)

        # Overall platform health
        health = self.triage_engine.get_pipeline_health()
        status = health.get("status", "unknown")
        status_icon = {"critical": "🔴", "degraded": "🟠", "elevated": "🟡", "healthy": "🟢"}.get(status, "⚪")

        parts = [
            f"**Platform Health Report**\n",
            f"Overall Status: {status_icon} {status.upper()}\n",
            f"- Alerts (last hour): {health.get('alerts_last_hour', 0)}",
            f"- Alerts (24h): {health.get('total_alerts_24h', 0)}",
            f"- Active clusters: {health.get('active_clusters', 0)}",
            f"- Critical: {health.get('critical_count', 0)}",
            f"- High: {health.get('high_count', 0)}",
        ]

        affected = health.get("affected_pipelines", [])
        if affected:
            parts.append(f"\n**Affected Pipelines:** {', '.join(affected[:5])}")

        # Per-pipeline breakdown
        pipeline_details = health.get("pipeline_details", {})
        if pipeline_details:
            parts.append("\n**Per-Pipeline Status:**")
            for name, detail in sorted(
                pipeline_details.items(),
                key=lambda x: {"critical": 0, "degraded": 1, "elevated": 2, "warning": 3, "healthy": 4}.get(
                    x[1].get("status", "healthy"), 5
                ),
            ):
                p_status = detail.get("status", "unknown")
                p_icon = {"critical": "🔴", "degraded": "🟠", "elevated": "🟡", "warning": "🟡", "healthy": "🟢"}.get(
                    p_status, "⚪"
                )
                parts.append(f"  {p_icon} {name} — {p_status} ({detail.get('alert_count_24h', 0)} alerts)")

        return "\n".join(parts)

    async def _handle_alert_search(
        self,
        session: ConversationContext,
        message: str,
    ) -> str:
        """
        Handle alert search queries: "Find all critical alerts",
        "Search for OOM errors", "Show alerts for spark_etl"
        Works WITHOUT an active_alert.
        """
        if not self.triage_engine:
            return "Alert triage is not configured. Please connect your MCP sources."

        # Parse search parameters
        severity_filter = self._extract_severity(message)
        pipeline_filter = self._extract_pipeline_name(message)
        hours = self._extract_hours(message) or 24

        # Extract search keyword (remove common filler words)
        query = self._extract_search_query(message)

        results = self.triage_engine.search_alerts(
            query=query,
            severity_filter=severity_filter,
            pipeline_filter=pipeline_filter,
            hours_back=hours,
        )

        if not results:
            return f"No alerts matching your search in the last {hours}h."

        parts = [f"**Search Results** ({len(results)} alerts found):\n"]

        for i, alert in enumerate(results[:10], 1):
            sev_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(
                alert.severity.value, "⚪"
            )
            parts.append(
                f"**{i}.** {sev_icon} **{alert.pipeline_name}** — {alert.error_class}\n"
                f"   {alert.root_cause_summary[:100]}...\n"
                f"   Time: {alert.timestamp.strftime('%H:%M UTC')} | "
                f"Severity: {alert.severity.value.upper()}"
            )
            parts.append("")

        if len(results) > 10:
            parts.append(f"_Showing top 10 of {len(results)} results. Refine your search to narrow down._")

        return "\n".join(parts)

    async def _handle_cross_alert_analytics(
        self,
        session: ConversationContext,
        message: str,
    ) -> str:
        """
        Handle cross-alert analytics: "Compare yesterday vs today",
        "Top recurring errors", "Error distribution"
        Works WITHOUT an active_alert.
        """
        if not self.triage_engine:
            return "Alert triage is not configured. Please connect your MCP sources."

        hours = self._extract_hours(message) or 24
        analytics = self.triage_engine.get_cross_alert_analytics(hours_back=hours)

        if analytics.get("summary"):
            return analytics["summary"]

        parts = [f"**Alert Analytics** (last {hours}h):\n"]

        parts.append(f"**Total Alerts:** {analytics.get('total_alerts', 0)}")
        parts.append(f"**Active Clusters:** {analytics.get('cluster_count', 0)}")
        parts.append(f"**Active Cascades:** {analytics.get('active_cascades', 0)}\n")

        # Severity distribution
        sev_dist = analytics.get("severity_distribution", {})
        if sev_dist:
            parts.append("**By Severity:**")
            for sev in ["critical", "high", "medium", "low"]:
                count = sev_dist.get(sev, 0)
                if count:
                    parts.append(f"  - {sev.upper()}: {count}")
            parts.append("")

        # Top errors
        top_errors = analytics.get("top_errors", [])
        if top_errors:
            parts.append("**Top Error Classes:**")
            for error, count in top_errors:
                parts.append(f"  - {error}: {count} occurrences")
            parts.append("")

        # Top pipelines
        top_pipelines = analytics.get("top_pipelines", [])
        if top_pipelines:
            parts.append("**Most Affected Pipelines:**")
            for pipeline, count in top_pipelines:
                parts.append(f"  - {pipeline}: {count} alerts")
            parts.append("")

        # Patterns
        patterns = analytics.get("patterns", [])
        if patterns:
            parts.append("**Detected Patterns:**")
            for p in patterns:
                parts.append(f"  ⚠️ {p}")

        return "\n".join(parts)

    # ─── Alert-Bound Intent Handlers ─────────────

    async def _handle_similar_incidents(
        self,
        session: ConversationContext,
        message: str,
    ) -> str:
        """Handle requests for similar historical incidents."""
        if not session.active_alert:
            # Graceful fallback: try discovery instead of hard gate
            if self.triage_engine:
                return await self._handle_alert_discovery(session, message)
            return (
                "No active alert is bound to this session. "
                "Please select an alert first, or ask \"What's broken?\" "
                "to discover active issues."
            )

        days = self._extract_days(message) or 90

        similar = await self.rag_retriever.find_similar_incidents(
            session.active_alert, days_back=days
        )
        session.similar_incidents = similar

        if not similar:
            return f"No similar incidents found in the last {days} days for pipeline '{session.active_alert.pipeline_name}'."

        parts = [f"Found **{len(similar)} similar incidents** in the last {days} days:\n"]
        for i, inc in enumerate(similar, 1):
            parts.append(
                f"**{i}. [{inc.incident_id}] {inc.title}**\n"
                f"   - Pipeline: {inc.pipeline_name}\n"
                f"   - Error: {inc.error_class}\n"
                f"   - Root Cause: {inc.root_cause[:100]}...\n"
                f"   - Resolution: {inc.resolution[:100]}...\n"
                f"   - Similarity: {inc.similarity_score:.0%}\n"
            )
        return "\n".join(parts)

    async def _handle_runbook_lookup(
        self,
        session: ConversationContext,
        message: str,
    ) -> str:
        """Handle runbook/procedure lookup requests."""
        if session.runbook_context:
            response = f"**Runbook Steps:**\n\n{session.runbook_context}"
            if session.active_alert and session.active_alert.runbook_url:
                response += f"\n\n[Open full runbook]({session.active_alert.runbook_url})"
        elif session.active_alert:
            steps = await self.alert_processor._fetch_runbook_steps(session.active_alert)
            session.runbook_context = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
            response = f"**Runbook Steps:**\n\n{session.runbook_context}"
        else:
            # Use Agentic RAG to search Confluence specifically
            result = await self.rag_retriever.retrieve_for_query(
                message, source_filter="confluence"
            )
            if result.chunks:
                context = await self.rag_retriever.build_context_string(result.chunks)
                response = f"**Relevant Procedures Found:**\n\n{context}"
            else:
                response = "No relevant runbooks found. Try describing the specific error or pipeline."

        return response

    async def _handle_root_cause_detail(
        self,
        session: ConversationContext,
        message: str,
    ) -> str:
        """
        Handle root cause detail requests.
        Uses Step-Back for ambiguous errors, Reflexion for refinement.
        """
        if not session.active_alert:
            # General RAG Q&A
            return await self._handle_general_qa(session, message)

        # Retrieve context using enhanced RAG pipeline
        result = await self.rag_retriever.retrieve_for_query(
            message, active_alert=session.active_alert
        )
        rag_context = await self.rag_retriever.build_context_string(result.chunks)

        # Select technique: Step-Back for ambiguous, standard for clear errors
        is_ambiguous = self.prompt_engine._is_ambiguous_error(
            session.active_alert.error_message
        )
        technique = "step_back" if is_ambiguous else "auto"

        # Build and invoke prompt
        messages = self.prompt_engine.build_root_cause_prompt(
            failure=session.active_alert,
            rag_context=rag_context,
            technique=technique,
        )
        response = await self.prompt_engine.invoke_llm(messages)

        # Apply Reflexion for self-refinement on critical alerts
        if session.active_alert.severity.value in ("critical", "high"):
            try:
                refined, steps = await self.prompt_engine.invoke_with_reflexion(
                    messages, session.active_alert, max_iterations=1
                )
                if steps:
                    response = refined
                    response += f"\n\n---\n*Analysis refined via Reflexion ({len(steps)} iteration{'s' if len(steps) > 1 else ''}).*"
                    logger.info("reflexion_applied", iterations=len(steps))
            except Exception as e:
                logger.warning("reflexion_failed", error=str(e))

        return response

    async def _handle_view_logs(
        self,
        session: ConversationContext,
        message: str,
    ) -> str:
        """Handle requests to view logs."""
        if session.active_alert:
            alert = session.active_alert
            parts = ["**Log Information:**\n"]

            if alert.log_snippet:
                parts.append(f"```\n{alert.log_snippet[:3000]}\n```")

            if alert.log_url:
                parts.append(f"\n[View full logs]({alert.log_url})")

            return "\n".join(parts)

        # No active alert — offer helpful guidance
        return (
            "No active alert selected. Select an alert from the sidebar to view its logs, "
            "or ask \"What's broken?\" to discover active issues."
        )

    def _handle_rerun_request(self, session: ConversationContext) -> str:
        """Handle pipeline rerun requests."""
        if session.active_alert and session.active_alert.rerun_url:
            return (
                f"**Rerun Pipeline:** {session.active_alert.pipeline_name}\n\n"
                f"[Click here to rerun]({session.active_alert.rerun_url})\n\n"
                f"Before rerunning, verify:\n"
                f"1. The root cause has been addressed\n"
                f"2. Upstream data sources are healthy\n"
                f"3. Cluster resources are available"
            )
        elif session.active_alert:
            return (
                f"No rerun URL is configured for '{session.active_alert.pipeline_name}'.\n"
                f"Please trigger a rerun manually through the orchestration tool (Databricks / Synapse Studio)."
            )
        return (
            "No active alert selected. Please specify which pipeline to rerun, "
            "or select an alert from the sidebar."
        )

    async def _handle_escalation(
        self,
        session: ConversationContext,
        message: str,
    ) -> str:
        """
        Handle escalation requests.
        Enhanced with GraphRAG blast radius analysis.
        """
        if not session.active_alert:
            return (
                "Please select an alert first, then I can help with escalation. "
                "Or ask \"What's broken?\" to discover active issues."
            )

        alert = session.active_alert
        parts = [
            f"**Escalation for: {alert.pipeline_name}**\n",
            f"Severity: {alert.severity.value.upper()}",
            f"Error: {alert.error_class}\n",
        ]

        # Add blast radius from GraphRAG
        if self.rag_retriever.graph_rag:
            blast = self.rag_retriever.graph_rag.trace_blast_radius(alert.pipeline_name)
            if blast.get("affected_pipelines"):
                parts.append(
                    f"**Blast Radius:** {blast['blast_radius']}\n"
                    f"Affected pipelines: {', '.join(blast['affected_pipelines'])}\n"
                    f"Affected components: {', '.join(blast.get('affected_components', []))}\n"
                )

        parts.append(
            "To escalate, I can:\n"
            "1. Send a Teams notification to the on-call channel\n"
            "2. Page the escalation contacts directly\n"
            "3. Create/update an ICM ticket\n\n"
            "Which action would you like to take?"
        )

        return "\n".join(parts)

    async def _handle_blast_radius(
        self,
        session: ConversationContext,
        message: str,
    ) -> str:
        """Handle blast radius / dependency analysis requests using GraphRAG."""
        if not session.active_alert:
            # Try to extract a pipeline name from the query
            pipeline = self._extract_pipeline_name(message)
            if pipeline and self.triage_engine:
                return await self._handle_platform_health(session, f"is {pipeline} healthy")
            return (
                "Please select an alert to analyze its blast radius, "
                "or ask about a specific pipeline: \"What's the blast radius of [pipeline]?\""
            )

        if self.rag_retriever.graph_rag:
            blast = self.rag_retriever.graph_rag.trace_blast_radius(
                session.active_alert.pipeline_name, depth=3
            )
            chain = self.rag_retriever.graph_rag.find_failure_chain(
                session.active_alert.pipeline_name,
                session.active_alert.error_class,
            )

            parts = [f"**Blast Radius Analysis for {session.active_alert.pipeline_name}:**\n"]

            if blast.get("affected_pipelines"):
                parts.append(f"**Affected Pipelines ({len(blast['affected_pipelines'])}):**")
                for p in blast["affected_pipelines"]:
                    parts.append(f"  - {p}")
                parts.append("")

            if blast.get("affected_components"):
                parts.append(f"**Affected Components ({len(blast['affected_components'])}):**")
                for c in blast["affected_components"]:
                    parts.append(f"  - {c}")
                parts.append("")

            if chain:
                parts.append("**Failure Chain:**")
                for step in chain:
                    arrow = "→" if step["step"] != "failure_point" else "✗"
                    parts.append(
                        f"  {arrow} {step['component']} "
                        f"({step.get('relationship', step.get('error', ''))})"
                    )
                parts.append("")

            parts.append(f"**Summary:** {blast.get('blast_radius', 'Unable to determine')}")
            return "\n".join(parts)

        return (
            "Graph-based dependency analysis is not enabled. "
            "Please configure GraphRAG for blast radius tracing."
        )

    async def _handle_trend_analysis(
        self,
        session: ConversationContext,
        message: str,
    ) -> str:
        """Handle trend/pattern analysis requests."""
        result = await self.rag_retriever.retrieve_for_query(
            message, active_alert=session.active_alert
        )

        if result.chunks:
            rag_context = await self.rag_retriever.build_context_string(result.chunks)
        else:
            rag_context = "No trend data available in the knowledge base."

        messages = self.prompt_engine.build_conversational_prompt(
            user_question=message,
            context=session,
            rag_context=rag_context,
        )
        return await self.prompt_engine.invoke_llm(messages)

    async def _handle_general_qa(
        self,
        session: ConversationContext,
        message: str,
    ) -> str:
        """
        Handle general Q&A with enhanced RAG context.
        Uses Agentic RAG for multi-source retrieval.
        """
        # Retrieve context using Agentic RAG (multi-source)
        result = await self.rag_retriever.retrieve_for_query(
            message, active_alert=session.active_alert
        )
        rag_context = await self.rag_retriever.build_context_string(result.chunks)

        # Build and invoke prompt
        messages = self.prompt_engine.build_conversational_prompt(
            user_question=message,
            context=session,
            rag_context=rag_context,
        )
        response = await self.prompt_engine.invoke_llm(messages)

        # Active prompting: Check for uncertainty markers
        uncertainty_markers = ["i'm not sure", "might be", "possibly", "unclear", "cannot determine"]
        if any(marker in response.lower() for marker in uncertainty_markers):
            response += (
                "\n\n---\n"
                "**Confidence Note:** My answer contains some uncertainty. "
                "For higher confidence, consider:\n"
                "- Providing more specific error details\n"
                "- Sharing the full stack trace\n"
                "- Checking the logs directly"
            )

        return response

    # ─── Helpers ──────────────────────────────

    def _build_greeting(self, alert: Optional[ParsedFailure]) -> str:
        """Build a context-aware greeting for a new session."""
        if alert:
            return (
                f"I'm Alert Whisperer, ready to help troubleshoot.\n\n"
                f"**Active Alert:**\n"
                f"- Pipeline: {alert.pipeline_name}\n"
                f"- Error: {alert.error_class}\n"
                f"- Severity: {alert.severity.value}\n"
                f"- Root Cause: {alert.root_cause_summary}\n\n"
                f"You can ask me about:\n"
                f"- Similar past incidents\n"
                f"- Runbook steps\n"
                f"- Root cause details\n"
                f"- Pipeline logs\n"
                f"- Blast radius / downstream impact\n"
                f"- Escalation options"
            )

        # No alert — use triage engine for proactive greeting
        if self.triage_engine:
            return self.triage_engine.build_proactive_greeting()

        return (
            "I'm Alert Whisperer. I can help you investigate pipeline failures, "
            "find similar past incidents, look up runbook procedures, and guide troubleshooting.\n\n"
            "Select an alert from the feed to start, or ask me a question.\n\n"
            "Try:\n"
            "- \"What's broken right now?\"\n"
            "- \"Show me all critical alerts\"\n"
            "- \"Is spark_etl_daily_ingest healthy?\"\n"
            "- \"Compare yesterday vs today\""
        )

    @staticmethod
    def _extract_days(message: str) -> Optional[int]:
        """Extract a number of days from a message like 'last 90 days'."""
        match = re.search(r'(\d+)\s*days?', message, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def _extract_hours(message: str) -> Optional[int]:
        """Extract a number of hours from a message like 'last 2 hours'."""
        match = re.search(r'(\d+)\s*hours?', message, re.IGNORECASE)
        if match:
            return int(match.group(1))
        # "last hour" without a number
        if re.search(r'last\s+hour\b', message, re.IGNORECASE):
            return 1
        return None

    @staticmethod
    def _extract_pipeline_name(message: str) -> Optional[str]:
        """Extract a pipeline name from a message."""
        # Known pipeline name patterns (underscore-separated identifiers)
        match = re.search(r'\b([a-z][a-z0-9_]{5,})\b', message.lower())
        if match:
            candidate = match.group(1)
            # Filter out common English words
            stop_words = {
                "anything", "something", "everything", "nothing", "however",
                "without", "through", "another", "between", "because",
                "before", "should", "during", "please", "healthy",
                "system", "status", "report", "search", "filter",
                "alerts", "errors", "failures", "issues", "broken",
                "compare", "yesterday", "analytics", "platform",
            }
            if candidate not in stop_words and "_" in candidate:
                return candidate
        return None

    @staticmethod
    def _extract_severity(message: str) -> Optional[list[Severity]]:
        """Extract severity filter from message."""
        msg_lower = message.lower()
        severities = []
        if "critical" in msg_lower:
            severities.append(Severity.CRITICAL)
        if "high" in msg_lower:
            severities.append(Severity.HIGH)
        if "medium" in msg_lower:
            severities.append(Severity.MEDIUM)
        if "low" in msg_lower:
            severities.append(Severity.LOW)
        return severities if severities else None

    @staticmethod
    def _extract_search_query(message: str) -> str:
        """Extract the core search term from a search message."""
        # Remove common search prefixes
        cleaned = re.sub(
            r"^(?:search|find|show|list|filter|look)\s+(?:for\s+|me\s+)?(?:all\s+)?",
            "",
            message.lower(),
        )
        # Remove severity words and common fillers
        cleaned = re.sub(
            r"\b(?:critical|high|medium|low|severity|alerts?|errors?|failures?|in|the|last|hours?|days?|\d+)\b",
            "",
            cleaned,
        )
        cleaned = " ".join(cleaned.split()).strip()
        return cleaned if cleaned else message.lower()
