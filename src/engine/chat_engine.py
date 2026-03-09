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

import hashlib
import re
import time
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, AsyncIterator, Optional

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
# Response Cache
# ─────────────────────────────────────────────────

class ResponseCache:
    """LRU-style response cache with TTL expiration."""

    def __init__(self, max_size: int = 200, ttl_seconds: int = 300):
        self._cache: dict[str, tuple[str, float]] = {}
        self._max_size = max_size
        self._ttl = ttl_seconds

    def _make_key(self, query: str, intent: str, context_hash: str) -> str:
        raw = f"{query.lower().strip()}::{intent}::{context_hash}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def get(self, query: str, intent: str, context_hash: str = "") -> Optional[str]:
        key = self._make_key(query, intent, context_hash)
        if key in self._cache:
            val, ts = self._cache[key]
            if time.time() - ts < self._ttl:
                return val
            del self._cache[key]
        return None

    def put(self, query: str, intent: str, response: str, context_hash: str = "") -> None:
        if len(self._cache) >= self._max_size:
            # Evict oldest
            oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]
        key = self._make_key(query, intent, context_hash)
        self._cache[key] = (response, time.time())

    def invalidate(self) -> None:
        self._cache.clear()


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

    # ── CE2: Regex-based uncertainty detection patterns ────────────────────
    UNCERTAINTY_PATTERNS = [
        re.compile(r'\bI think\b', re.IGNORECASE),
        re.compile(r'\bmaybe\b', re.IGNORECASE),
        re.compile(r'\bpossibly\b', re.IGNORECASE),
        re.compile(r'\bnot sure\b', re.IGNORECASE),
        re.compile(r'\bmight be\b', re.IGNORECASE),
        re.compile(r'\bcould be\b', re.IGNORECASE),
        re.compile(r'\bprobably\b', re.IGNORECASE),
        re.compile(r'\buncertain\b', re.IGNORECASE),
        re.compile(r'\bapproximately\b', re.IGNORECASE),
    ]

    # ── CE3: Per-intent temperature map (keys match UserIntent constants) ──
    INTENT_TEMPERATURE_MAP: dict[str, float] = {
        UserIntent.ROOT_CAUSE_DETAIL: 0.05,
        UserIntent.SIMILAR_INCIDENTS: 0.05,
        UserIntent.VIEW_LOGS: 0.1,
        UserIntent.ALERT_SEARCH: 0.1,
        UserIntent.RUNBOOK_LOOKUP: 0.1,
        UserIntent.ESCALATION: 0.1,
        UserIntent.PLATFORM_HEALTH: 0.15,
        UserIntent.CROSS_ALERT_ANALYTICS: 0.15,
        UserIntent.ALERT_DISCOVERY: 0.15,
        UserIntent.TREND_ANALYSIS: 0.15,
        UserIntent.BLAST_RADIUS: 0.1,
        UserIntent.RERUN_PIPELINE: 0.1,
        UserIntent.QUERY_DATA: 0.1,
        UserIntent.STATUS_UPDATE: 0.15,
        UserIntent.GENERAL_QA: 0.3,
    }

    # ── CE7: Session TTL management ────────────────────────────────────────
    SESSION_TTL_SECONDS: int = 3600  # 1 hour
    MAX_SESSIONS: int = 100

    # ── CE8: Minimum routing confidence threshold ──────────────────────────
    MIN_ROUTING_CONFIDENCE: float = 0.3

    # ── CE11: Configurable context window size ─────────────────────────────
    CONTEXT_WINDOW_SIZE: int = 20

    # ── CE16: Stale runbook detection ──────────────────────────────────────
    RUNBOOK_STALENESS_DAYS: int = 90

    # ── CE17: Special command handling ────────────────────────────────────
    COMMANDS = {"/new", "/reset", "/clear"}

    # ── CE22: Proactive follow-up suggestions map ──────────────────────────
    FOLLOW_UP_MAP: dict[str, list[str]] = {
        "ROOT_CAUSE": [
            "What are the recommended fix steps?",
            "Show me the runbook",
            "Are there similar past incidents?",
        ],
        "TROUBLESHOOT": [
            "Can you escalate this?",
            "What's the blast radius?",
            "Show related alerts",
        ],
        "HEALTH_CHECK": [
            "Show me critical alerts",
            "What failed recently?",
            "Compare with yesterday",
        ],
        "ALERT_SEARCH": [
            "Show me details for the top result",
            "What's the root cause?",
            "Any patterns across these?",
        ],
        "RUNBOOK": [
            "Is this runbook up to date?",
            "Show me similar incidents",
            "What's the current status?",
        ],
    }

    # ── CE1: CoVE-enabled intents ──────────────────────────────────────────
    cove_enabled_intents: set = {
        UserIntent.ROOT_CAUSE_DETAIL,
        UserIntent.RUNBOOK_LOOKUP,
        UserIntent.SIMILAR_INCIDENTS,
        UserIntent.ESCALATION,
        UserIntent.TREND_ANALYSIS,
        UserIntent.BLAST_RADIUS,
        UserIntent.ALERT_DISCOVERY,
        UserIntent.PLATFORM_HEALTH,
        UserIntent.ALERT_SEARCH,
        UserIntent.CROSS_ALERT_ANALYTICS,
    }

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
        # CE4: Response cache with TTL
        self._response_cache = ResponseCache(max_size=200, ttl_seconds=300)

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
            # Clear stale runbook context from any previous alert binding
            session.runbook_context = None
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
        # CE7: Cleanup stale sessions at the start of each message
        self._cleanup_stale_sessions()

        session = self._sessions.get(session_id)
        if not session:
            return "Session not found. Please start a new conversation."

        # CE17: Handle special commands before anything else
        command_response = self._handle_commands(user_message, session_id)
        if command_response is not None:
            return command_response

        session.add_message(MessageRole.USER, user_message)
        # CE7: Track last active time
        if not hasattr(session, "_last_active"):
            object.__setattr__(session, "_last_active", time.time()) if hasattr(session, "__slots__") else setattr(session, "_last_active", time.time())
        else:
            try:
                session._last_active = time.time()  # type: ignore[attr-defined]
            except AttributeError:
                pass

        # CE24: Use shared routing method
        intent, confidence = self._route_intent(user_message, session)
        logger.info("intent_classified", session_id=session_id, intent=intent, confidence=round(confidence, 3))

        # CE3: Set per-intent temperature for downstream invoke_llm calls
        self._current_temperature = self._get_temperature_for_intent(intent)

        # CE4: Check response cache before routing
        context_hash = hashlib.sha256(
            f"{session.active_alert.failure_id if session.active_alert else ''}::{session.message_count}".encode()
        ).hexdigest()[:12]
        cached = self._response_cache.get(user_message, intent, context_hash)
        if cached is not None:
            logger.info("response_cache_hit", session_id=session_id, intent=intent)
            session.add_message(MessageRole.ASSISTANT, cached)
            return cached

        # CE6: Detect multi-intent and route each sub-query
        sub_queries = self._detect_multi_intent(user_message)
        if len(sub_queries) > 1:
            logger.info("multi_intent_detected", count=len(sub_queries))
            # Process each sub-query independently and merge responses
            responses: list[str] = []
            for sq in sub_queries:
                sq_intent, _sq_conf = self._route_intent(sq, session)
                self._current_temperature = self._get_temperature_for_intent(sq_intent)
                sq_response = await self._route_to_handler(sq_intent, session, sq)
                responses.append(sq_response)
            response = "\n\n---\n\n".join(responses)
        else:
            # Single intent — standard routing
            response = await self._route_to_handler(intent, session, user_message)

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
                    # CE21: Deduplicate FLARE-retrieved context
                    response = self._deduplicate_flare_context(response, flare_result.chunks)
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

        # CE1: Apply CoVE verification for configured intents
        response = await self._maybe_apply_cove(response, intent, session)

        # CE22: Append proactive follow-up suggestions
        response += self._generate_follow_ups(intent)

        # CE4: Cache the final response
        self._response_cache.put(user_message, intent, response, context_hash)

        session.add_message(MessageRole.ASSISTANT, response)
        return response

    async def process_message_streaming(
        self,
        session_id: str,
        user_message: str,
    ):
        """
        CE23: Streaming entry point — delegates to stream_response_tokens
        for real token-level streaming, with FLARE augmentation afterward.

        No longer duplicates process_message routing logic.
        """
        # CE7: Cleanup stale sessions
        self._cleanup_stale_sessions()

        session = self._sessions.get(session_id)
        if not session:
            yield "Session not found. Please start a new conversation."
            return

        # CE17: Handle special commands
        command_response = self._handle_commands(user_message, session_id)
        if command_response is not None:
            yield command_response
            return

        session.add_message(MessageRole.USER, user_message)
        try:
            session._last_active = time.time()  # type: ignore[attr-defined]
        except AttributeError:
            pass

        # CE24: Use shared routing
        intent, confidence = self._route_intent(user_message, session)
        self._current_temperature = self._get_temperature_for_intent(intent)
        logger.info("streaming_intent_classified", session_id=session_id, intent=intent, confidence=round(confidence, 3))

        # CE23: Delegate to stream_response_tokens for real token-level streaming
        collected_tokens: list[str] = []
        async for token in self.stream_response_tokens(user_message, session_id):
            collected_tokens.append(token)
            yield token

        response = "".join(collected_tokens)

        # CE1: Apply CoVE for configured intents (post-stream enrichment)
        verified = await self._maybe_apply_cove(response, intent, session)
        if verified != response:
            supplement = verified[len(response):] if verified.startswith(response) else f"\n\n{verified}"
            response = verified
            yield supplement

        # CE22: Append follow-up suggestions
        follow_ups = self._generate_follow_ups(intent)
        if follow_ups:
            response += follow_ups
            yield follow_ups

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
                    new_response = self._deduplicate_flare_context(response, flare_result.chunks)
                    flare_supplement = new_response[len(response):]
                    response = new_response
                    if flare_supplement:
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
        # CE15: Self-RAG relevance pass on similar incidents
        similar = self._verify_similar_incidents(similar, message)
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
            # CE16: Check runbook staleness
            if session.active_alert and hasattr(session.active_alert, "runbook_metadata"):
                staleness_warning = self._check_runbook_staleness(
                    session.active_alert.runbook_metadata or {}
                )
                if staleness_warning:
                    response += f"\n\n{staleness_warning}"
        elif session.active_alert:
            steps = await self.alert_processor._fetch_runbook_steps(session.active_alert)
            session.runbook_context = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
            response = f"**Runbook Steps:**\n\n{session.runbook_context}"
            # CE16: Check runbook staleness when fetching fresh
            if hasattr(session.active_alert, "runbook_metadata"):
                staleness_warning = self._check_runbook_staleness(
                    session.active_alert.runbook_metadata or {}
                )
                if staleness_warning:
                    response += f"\n\n{staleness_warning}"
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
        response = await self.prompt_engine.invoke_llm(messages, temperature=getattr(self, '_current_temperature', None))

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
            base = (
                f"**Rerun Pipeline:** {session.active_alert.pipeline_name}\n\n"
                f"Before rerunning, verify:\n"
                f"1. The root cause has been addressed\n"
                f"2. Upstream data sources are healthy\n"
                f"3. Cluster resources are available"
            )
            # CE19: Wrap in confirmation-formatted rerun action
            return base + self._format_rerun_action(
                session.active_alert.rerun_url,
                session.active_alert.pipeline_name,
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
        return await self.prompt_engine.invoke_llm(messages, temperature=getattr(self, '_current_temperature', None))

    async def _handle_general_qa(
        self,
        session: ConversationContext,
        message: str,
    ) -> str:
        """
        Handle general Q&A with enhanced RAG context.
        Uses Agentic RAG for multi-source retrieval.

        P2: Cold-start routing — when no active alert is bound and no triage engine
        is available, use the specialized cold_start prompt instead of generic Q&A.
        This guides the LLM to ask clarifying questions or suggest discovery queries.
        """
        # P2: Route to cold-start prompt if there is no alert context and no triage engine
        is_cold_start = (
            session.active_alert is None
            and not self.triage_engine
        )
        if is_cold_start:
            messages = self.prompt_engine.build_cold_start_prompt(
                user_question=message,
                context=session,
            )
            logger.info("cold_start_routing_applied", session_id=session.session_id)
            return await self.prompt_engine.invoke_llm(messages, temperature=getattr(self, '_current_temperature', None))

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
        response = await self.prompt_engine.invoke_llm(messages, temperature=getattr(self, '_current_temperature', None))

        # CE2: Active prompting — use regex-based uncertainty detection patterns
        if any(pat.search(response) for pat in self.UNCERTAINTY_PATTERNS):
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

    # ─────────────────────────────────────────────────
    # CE-specific helper methods
    # ─────────────────────────────────────────────────

    # ── CE1: CoVE wrapper ────────────────────────────────────────────
    async def _maybe_apply_cove(
        self,
        response: str,
        intent: str,
        session: ConversationContext,
    ) -> str:
        """
        CE1: Apply CoVE (Chain-of-Verification) for configured intents.
        Skips GENERAL_QA and QUERY_DATA which don't benefit from CoVE.
        """
        if intent not in self.cove_enabled_intents:
            return response
        # Only apply CoVE if the prompt_engine supports invoke_with_verification
        if not hasattr(self.prompt_engine, "invoke_with_verification"):
            return response
        try:
            # Build evidence from recent conversation context
            evidence = "\n".join(
                f"{m.role.value}: {m.content[:500]}"
                for m in session.get_recent_messages(5)
            )
            alert_context = ""
            if session.active_alert:
                alert_context = (
                    f"Pipeline: {session.active_alert.pipeline_name}, "
                    f"Error: {session.active_alert.error_class}, "
                    f"Severity: {session.active_alert.severity.value}"
                )
            verified_text, _claims = await self.prompt_engine.invoke_with_verification(
                analysis=response,
                evidence=evidence,
                alert_context=alert_context,
            )
            return verified_text if verified_text else response
        except Exception as e:
            logger.warning("cove_failed", intent=intent, error=str(e))
            return response

    # ── CE6: Multi-intent detection ────────────────────────────────────
    def _detect_multi_intent(self, query: str) -> list[str]:
        """Detect if query contains multiple intents."""
        SPLIT_PATTERNS = [
            r'\band\s+also\b',
            r'\band\s+then\b',
            r'\bas\s+well\s+as\b',
            r'\badditionally\b',
            r'\bplus\b',
            r'\balso\s+show\b',
        ]
        for pat in SPLIT_PATTERNS:
            if re.search(pat, query, re.IGNORECASE):
                parts = re.split(pat, query, flags=re.IGNORECASE)
                return [p.strip() for p in parts if p.strip()]
        return [query]

    # ── CE6: Unified handler dispatch ─────────────────────────────────
    async def _route_to_handler(
        self,
        intent: str,
        session: ConversationContext,
        message: str,
    ) -> str:
        """Route a single intent to its handler. Used by both single- and multi-intent paths."""
        if intent == UserIntent.ALERT_DISCOVERY:
            return await self._handle_alert_discovery(session, message)
        elif intent == UserIntent.PLATFORM_HEALTH:
            return await self._handle_platform_health(session, message)
        elif intent == UserIntent.ALERT_SEARCH:
            return await self._handle_alert_search(session, message)
        elif intent == UserIntent.CROSS_ALERT_ANALYTICS:
            return await self._handle_cross_alert_analytics(session, message)
        elif intent == UserIntent.SIMILAR_INCIDENTS:
            return await self._handle_similar_incidents(session, message)
        elif intent == UserIntent.RUNBOOK_LOOKUP:
            return await self._handle_runbook_lookup(session, message)
        elif intent == UserIntent.ROOT_CAUSE_DETAIL:
            return await self._handle_root_cause_detail(session, message)
        elif intent == UserIntent.VIEW_LOGS:
            return await self._handle_view_logs(session, message)
        elif intent == UserIntent.RERUN_PIPELINE:
            return self._handle_rerun_request(session)
        elif intent == UserIntent.ESCALATION:
            return await self._handle_escalation(session, message)
        elif intent == UserIntent.TREND_ANALYSIS:
            return await self._handle_trend_analysis(session, message)
        elif intent == UserIntent.BLAST_RADIUS:
            return await self._handle_blast_radius(session, message)
        else:
            return await self._handle_general_qa(session, message)

    # ── CE7: Session cleanup ───────────────────────────────────────────
    def _cleanup_stale_sessions(self) -> int:
        """Remove sessions older than TTL. Returns count removed."""
        now = time.time()
        stale = [
            sid for sid, ctx in self._sessions.items()
            if (now - getattr(ctx, "_last_active", 0)) > self.SESSION_TTL_SECONDS
        ]
        for sid in stale:
            del self._sessions[sid]
        # Also enforce max sessions
        while len(self._sessions) > self.MAX_SESSIONS:
            oldest = min(
                self._sessions,
                key=lambda s: getattr(self._sessions[s], "_last_active", 0),
            )
            del self._sessions[oldest]
        if stale:
            logger.info("sessions_cleaned", removed=len(stale), remaining=len(self._sessions))
        return len(stale)

    # ── CE3: Resolve temperature for current intent ────────────────────────
    def _get_temperature_for_intent(self, intent: str) -> float:
        """Return the temperature setting for the given intent."""
        return self.INTENT_TEMPERATURE_MAP.get(intent, 0.3)

    # ── CE13: User-facing error classification ────────────────────────────
    def _classify_error(self, error: Exception) -> str:
        """Return user-friendly error message."""
        error_str = str(error).lower()
        if "timeout" in error_str or "timed out" in error_str:
            return "The request timed out. The data source may be under heavy load. Please try again in a moment."
        if "connection" in error_str or "unreachable" in error_str:
            return "Unable to connect to the data source. Please check connector status in Settings."
        if "rate limit" in error_str or "429" in error_str:
            return "Rate limit reached. Please wait a moment before retrying."
        if "auth" in error_str or "401" in error_str or "403" in error_str:
            return "Authentication error. Please verify credentials in Settings."
        return f"An unexpected error occurred: {type(error).__name__}. Please try rephrasing your question."

    # ── CE15: Self-RAG pass on similar incidents ──────────────────────────
    def _verify_similar_incidents(self, incidents: list, query: str) -> list:
        """Re-score incidents for relevance using Self-RAG keyword overlap."""
        if not incidents:
            return incidents
        verified = []
        query_terms = set(query.lower().split())
        for inc in incidents:
            title = getattr(inc, "title", "")
            description = getattr(inc, "description", "")
            error_class = getattr(inc, "error_class", "")
            inc_terms = set(f"{title} {description} {error_class}".lower().split())
            overlap = len(query_terms & inc_terms) / max(len(query_terms), 1)
            if overlap >= 0.15:
                verified.append(inc)
        return verified or incidents[:3]  # Fallback to top 3 if none pass

    # ── CE16: Stale runbook detection ───────────────────────────────────
    def _check_runbook_staleness(self, runbook_metadata: dict) -> Optional[str]:
        """Return warning if runbook is stale."""
        last_modified = runbook_metadata.get("last_modified")
        if last_modified:
            from datetime import datetime, timezone
            if isinstance(last_modified, str):
                last_modified = datetime.fromisoformat(last_modified.replace("Z", "+00:00"))
            try:
                age_days = (datetime.now(timezone.utc) - last_modified).days
            except TypeError:
                # last_modified may be naive; make it aware
                if last_modified.tzinfo is None:
                    from datetime import timezone as _tz
                    last_modified = last_modified.replace(tzinfo=_tz.utc)
                age_days = (datetime.now(timezone.utc) - last_modified).days
            if age_days > self.RUNBOOK_STALENESS_DAYS:
                return (
                    f"⚠️ **Note:** This runbook was last updated {age_days} days ago "
                    f"and may contain outdated information. Please verify steps before executing."
                )
        return None

    # ── CE17: Command handling ─────────────────────────────────────────
    def _handle_commands(self, query: str, session_id: str) -> Optional[str]:
        """Handle special commands. Returns response if command handled, None otherwise."""
        cmd = query.strip().lower()
        if cmd in self.COMMANDS:
            if session_id in self._sessions:
                del self._sessions[session_id]
            return "Session reset. How can I help you?"
        if cmd == "/export":
            return self._export_session(session_id)
        if cmd == "/help":
            return (
                "Available commands:\n"
                "- `/new` or `/reset` — Start a new session\n"
                "- `/export` — Export conversation\n"
                "- `/help` — Show this help message"
            )
        return None

    def _export_session(self, session_id: str) -> str:
        """Export session conversation history."""
        session = self._sessions.get(session_id)
        if not session:
            return "No active session to export."
        lines = [f"# Conversation Export — Session {session_id}\n"]
        for msg in session.messages:
            role = getattr(msg, "role", "") if hasattr(msg, "role") else ""
            content = getattr(msg, "content", str(msg))
            role_label = role.value if hasattr(role, "value") else str(role)
            lines.append(f"**{role_label.upper()}:** {content}\n")
        return "\n".join(lines)

    # ── CE19: Rerun confirmation formatting ──────────────────────────────
    def _format_rerun_action(self, rerun_url: str, pipeline_name: str) -> str:
        """Format rerun action with confirmation warning."""
        return (
            f"\n\n---\n"
            f"🔄 **Rerun Available:** [{pipeline_name}]({rerun_url})\n"
            f"⚠️ **Confirm before rerunning** — This will trigger a new pipeline execution. "
            f"Ensure the root cause has been addressed first.\n"
            f"---"
        )

    # ── CE21: FLARE dedup + summarization ───────────────────────────────
    def _deduplicate_flare_context(self, existing_context: str, new_chunks: list) -> str:
        """Deduplicate and summarize FLARE-retrieved content."""
        existing_sentences = set(existing_context.split('. '))
        new_content = []
        for chunk in new_chunks:
            content = getattr(chunk, "content", "") or ""
            sentences = content.split('. ')
            for s in sentences:
                if s.strip() and s not in existing_sentences:
                    new_content.append(s)
                    existing_sentences.add(s)
        if new_content:
            return existing_context + "\n\n**Additional Context:**\n" + '. '.join(new_content[:10])
        return existing_context

    # ── CE22: Proactive follow-up suggestions ────────────────────────────
    def _generate_follow_ups(self, intent: str) -> str:
        """Generate follow-up suggestions based on intent."""
        suggestions = self.FOLLOW_UP_MAP.get(intent, [])
        if not suggestions:
            return ""
        lines = ["\n\n---", "**💡 You might also ask:**"]
        for s in suggestions[:3]:
            lines.append(f"- {s}")
        return "\n".join(lines)

    # ── CE23: Real token-level streaming ───────────────────────────────
    async def stream_response_tokens(
        self,
        query: str,
        session_id: str,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """Real token-level streaming using LLM streaming API."""
        # CE17: Handle commands
        command_response = self._handle_commands(query, session_id)
        if command_response is not None:
            yield command_response
            return

        # CE24: Use shared routing
        session = self._sessions.get(session_id)
        intent, _confidence = self._route_intent(query, session)
        prompt = self._build_prompt_for_intent(query, intent, session, **kwargs)

        try:
            async for token in self._llm_stream(prompt):
                yield token
        except Exception as e:
            yield self._classify_error(e)

    async def _llm_stream(self, prompt: Any) -> AsyncGenerator[str, None]:
        """Async generator that streams tokens from the LLM."""
        if hasattr(self.prompt_engine, "invoke_llm_stream"):
            async for token in self.prompt_engine.invoke_llm_stream(prompt):
                yield token
        else:
            # Fallback: yield full response as a single chunk
            response = await self.prompt_engine.invoke_llm(prompt)
            yield response

    def _build_prompt_for_intent(
        self,
        query: str,
        intent: str,
        session: Optional[ConversationContext],
        **kwargs: Any,
    ) -> Any:
        """Build the prompt messages for a given intent."""
        if session is None:
            return self.prompt_engine.build_conversational_prompt(
                user_question=query,
                context=None,  # type: ignore[arg-type]
                rag_context="",
            )
        return self.prompt_engine.build_conversational_prompt(
            user_question=query,
            context=session,
            rag_context="",
        )

    # ── CE24: Unified intent routing ────────────────────────────────────
    def _route_intent(
        self,
        query: str,
        context: Optional[ConversationContext] = None,
    ) -> tuple[str, float]:
        """
        CE24: Unified intent routing — single source of truth.
        Returns (intent, confidence).
        Applies CE8 minimum confidence threshold.
        """
        if self.intent_classifier and hasattr(self.intent_classifier, "classify_with_confidence"):
            intent, confidence = self.intent_classifier.classify_with_confidence(query)
        elif self.intent_classifier:
            intent = self.intent_classifier.classify(query)
            # Estimate confidence from embedding score if available
            confidence = 0.8  # Default when using embedding classifier
        else:
            intent = classify_intent(query)
            # Estimate confidence from pattern score
            message_lower = query.lower()
            scores = {
                i: sum(1 for p in pats if re.search(p, message_lower))
                for i, pats in INTENT_PATTERNS.items()
            }
            max_score = max(scores.values(), default=0)
            confidence = min(max_score / 3.0, 1.0) if max_score > 0 else 0.0

        # CE8: Apply minimum confidence threshold
        if confidence < self.MIN_ROUTING_CONFIDENCE and intent != UserIntent.GENERAL_QA:
            logger.warning(
                "low_confidence_routing",
                original_intent=intent,
                confidence=round(confidence, 3),
                fallback="GENERAL_QA",
            )
            intent = UserIntent.GENERAL_QA

        return intent, confidence
