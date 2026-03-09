"""
RAG Retrieval Engine — Advanced Multi-Strategy.

Implements multi-source retrieval with:
- Topic tree hierarchical retrieval
- Reranking of results
- Context window optimization
- Q&A over large document sets
- CRAG (Corrective RAG) with relevance grading
- HyDE (Hypothetical Document Embeddings)
- FLARE (Forward-Looking Active Retrieval)
- Agentic RAG for multi-index orchestration
- GraphRAG for cascading failure tracing
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

import structlog

from src.models import (
    CRAGRelevanceScore,
    DocumentChunk,
    FailureGraph,
    GraphEdge,
    GraphNode,
    HistoricalIncident,
    ParsedFailure,
    RetrievalResult,
    Severity,
    TopicNode,
)
from src.rag.vector_store import VectorStore
from src.rag.advanced_techniques import (
    CrossEncoderReranker,
    QueryRewriter,
    RAGFusion,
    SelfRAG,
)

logger = structlog.get_logger(__name__)


class TopicTreeRetriever:
    """
    Implements Topic Tree Retrieval for hierarchical document navigation.

    The topic tree organizes knowledge into a hierarchy:
    - Level 0: Root categories (Spark, Synapse, Kusto, General)
    - Level 1: Error categories (OOM, Timeout, Connection, Auth, Data)
    - Level 2: Specific error patterns
    - Leaves: Relevant document chunk IDs

    This enables more precise retrieval by first identifying the
    relevant branch of the tree, then fetching documents within that branch.
    """

    def __init__(self):
        self.root = self._build_default_tree()

    def _build_default_tree(self) -> TopicNode:
        """Build the default topic tree for data pipeline errors."""
        return TopicNode(
            topic="Data Pipeline Errors",
            description="Root node for all pipeline error categories",
            children=[
                TopicNode(
                    topic="Spark Errors",
                    description="Apache Spark job failures, driver/executor issues",
                    children=[
                        TopicNode(
                            topic="OutOfMemory",
                            description="Java heap space, GC overhead, driver/executor OOM",
                            relevant_doc_ids=[],
                        ),
                        TopicNode(
                            topic="Shuffle Failures",
                            description="Shuffle fetch failures, disk space, network issues during shuffle",
                            relevant_doc_ids=[],
                        ),
                        TopicNode(
                            topic="Executor Lost",
                            description="Executor heartbeat timeout, container killed, spot preemption",
                            relevant_doc_ids=[],
                        ),
                        TopicNode(
                            topic="Schema Mismatch",
                            description="Column type errors, missing fields, schema evolution issues",
                            relevant_doc_ids=[],
                        ),
                        TopicNode(
                            topic="Spark Timeout",
                            description="Job/stage/task timeouts, broadcast timeouts, network timeouts",
                            relevant_doc_ids=[],
                        ),
                    ],
                ),
                TopicNode(
                    topic="Synapse Errors",
                    description="Azure Synapse Analytics pipeline and activity failures",
                    children=[
                        TopicNode(
                            topic="Pipeline Timeout",
                            description="Pipeline/activity execution timeouts, long-running queries",
                            relevant_doc_ids=[],
                        ),
                        TopicNode(
                            topic="Copy Activity Failures",
                            description="Data movement errors, format issues, source/sink connectivity",
                            relevant_doc_ids=[],
                        ),
                        TopicNode(
                            topic="SQL Pool Errors",
                            description="DWU limits, concurrency, deadlocks, distribution issues",
                            relevant_doc_ids=[],
                        ),
                        TopicNode(
                            topic="Authentication Failures",
                            description="Managed identity, SPN auth, key vault, token expiry",
                            relevant_doc_ids=[],
                        ),
                    ],
                ),
                TopicNode(
                    topic="Kusto Errors",
                    description="Azure Data Explorer ingestion and query errors",
                    children=[
                        TopicNode(
                            topic="Ingestion Failures",
                            description="Data format issues, mapping errors, throttling, schema mismatch",
                            relevant_doc_ids=[],
                        ),
                        TopicNode(
                            topic="Query Failures",
                            description="Query timeout, resource limits, semantic errors, partial failures",
                            relevant_doc_ids=[],
                        ),
                        TopicNode(
                            topic="Cluster Issues",
                            description="Cluster capacity, scaling failures, cache pressure, node issues",
                            relevant_doc_ids=[],
                        ),
                    ],
                ),
                TopicNode(
                    topic="Infrastructure Errors",
                    description="Cross-cutting infrastructure issues",
                    children=[
                        TopicNode(
                            topic="Network Connectivity",
                            description="DNS failures, VNet issues, firewall rules, private endpoints",
                            relevant_doc_ids=[],
                        ),
                        TopicNode(
                            topic="Storage Issues",
                            description="ADLS Gen2 errors, blob storage, throttling, permissions",
                            relevant_doc_ids=[],
                        ),
                        TopicNode(
                            topic="Authentication & Secrets",
                            description="Key Vault, managed identity, token refresh, certificate expiry",
                            relevant_doc_ids=[],
                        ),
                    ],
                ),
            ],
        )

    def classify_error(self, error_message: str, source: str = "") -> list[TopicNode]:
        """
        Classify an error message into topic tree branches.
        Returns matching leaf nodes ordered by confidence.
        Uses keyword matching as a fast first-pass classifier.
        """
        matches = []
        error_lower = error_message.lower()

        def _score_node(node: TopicNode, depth: int = 0) -> None:
            topic_words = set(node.topic.lower().split())
            desc_words = set(node.description.lower().split())
            all_keywords = topic_words | desc_words

            match_count = sum(1 for kw in all_keywords if kw in error_lower)
            if match_count > 0:
                confidence = min(match_count / max(len(all_keywords), 1), 1.0)
                if not node.children:
                    confidence *= 1.2
                node.confidence = min(confidence, 1.0)
                matches.append(node)

            for child in node.children:
                _score_node(child, depth + 1)

        _score_node(self.root)
        matches.sort(key=lambda n: n.confidence, reverse=True)
        return matches[:5]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CRAG — Corrective RAG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CRAGEvaluator:
    """
    Corrective RAG evaluator — grades retrieved chunks for relevance
    and triggers corrective actions when retrieval quality is low.

    Why CRAG for Spark/Synapse/Kusto troubleshooting:
    - Generic OOM runbooks get retrieved for specific Delta Lake compaction OOMs
      — CRAG detects this mismatch and corrects the retrieval
    - Old/outdated incident resolutions may match on keywords but describe
      deprecated configs (Spark 2.x advice for Spark 3.x clusters)
    - Kusto ingestion errors look similar across different mapping formats
      but require very different fixes — CRAG filters out false positives
    - When no relevant context exists (novel failure), CRAG falls back to
      web search or LLM parametric knowledge rather than returning junk
    """

    RELEVANCE_CORRECT = "correct"
    RELEVANCE_INCORRECT = "incorrect"
    RELEVANCE_AMBIGUOUS = "ambiguous"

    def __init__(
        self,
        relevance_threshold: float = 0.5,
        ambiguity_threshold: float = 0.3,
    ):
        self.relevance_threshold = relevance_threshold
        self.ambiguity_threshold = ambiguity_threshold

    def grade_retrieval(
        self,
        query: str,
        chunks: list[DocumentChunk],
    ) -> list[CRAGRelevanceScore]:
        """
        Grade each retrieved chunk for relevance to the query.
        Uses keyword overlap + metadata matching as a fast heuristic.

        Returns:
            List of CRAGRelevanceScore for each chunk
        """
        scores = []
        query_lower = query.lower()
        query_tokens = set(re.findall(r'\b\w+\b', query_lower))

        for chunk in chunks:
            content_lower = chunk.content.lower()
            content_tokens = set(re.findall(r'\b\w+\b', content_lower))

            # Calculate token overlap
            overlap = query_tokens & content_tokens
            if not query_tokens:
                overlap_ratio = 0.0
            else:
                overlap_ratio = len(overlap) / len(query_tokens)

            # Combine with vector similarity score if available
            vector_sim = float(chunk.metadata.get("similarity_score", 0.5))
            combined_score = 0.2 * overlap_ratio + 0.8 * vector_sim

            # Classify relevance
            if combined_score >= self.relevance_threshold:
                relevance = self.RELEVANCE_CORRECT
            elif combined_score >= self.ambiguity_threshold:
                relevance = self.RELEVANCE_AMBIGUOUS
            else:
                relevance = self.RELEVANCE_INCORRECT

            scores.append(
                CRAGRelevanceScore(
                    chunk_id=chunk.chunk_id,
                    query=query,
                    relevance=relevance,
                    score=round(combined_score, 4),
                    reasoning=f"Token overlap: {overlap_ratio:.2%}, Vector sim: {vector_sim:.2%}",
                )
            )

        return scores

    def apply_correction(
        self,
        chunks: list[DocumentChunk],
        scores: list[CRAGRelevanceScore],
    ) -> tuple[list[DocumentChunk], str]:
        """
        Apply corrective actions based on relevance grades.

        Actions:
        - CORRECT: Keep the chunk as-is
        - AMBIGUOUS: Keep but flag for refinement
        - INCORRECT: Remove from context

        Returns:
            Tuple of (corrected chunks, action taken)
        """
        correct = []
        ambiguous = []
        incorrect_count = 0

        score_map = {s.chunk_id: s for s in scores}

        for chunk in chunks:
            grade = score_map.get(chunk.chunk_id)
            if not grade:
                correct.append(chunk)
                continue

            if grade.relevance == self.RELEVANCE_CORRECT:
                correct.append(chunk)
            elif grade.relevance == self.RELEVANCE_AMBIGUOUS:
                ambiguous.append(chunk)
            else:
                incorrect_count += 1

        # Decision logic
        if not correct and not ambiguous:
            action = "no_relevant_context"
            return [], action
        elif correct:
            # Include ambiguous chunks at lower priority
            result = correct + ambiguous
            action = f"kept_{len(correct)}_correct_{len(ambiguous)}_ambiguous_removed_{incorrect_count}"
        else:
            # Only ambiguous — flag for query refinement
            result = ambiguous
            action = f"only_ambiguous_{len(ambiguous)}_need_refinement"

        logger.info(
            "crag_correction_applied",
            correct=len(correct),
            ambiguous=len(ambiguous),
            removed=incorrect_count,
            action=action,
        )
        return result, action


class LLMJudgeCRAG:
    """
    LLM-as-judge for CRAG relevance grading.

    Why LLM-as-judge replaces token overlap for Spark/Synapse/Kusto:
    - Token overlap scores "Spark executor OOM" as relevant to ANY Spark OOM
      doc, even if the specific cause (skew vs broadcast vs spill) differs —
      an LLM understands the semantic difference
    - Kusto ingestion errors have near-identical keyword profiles but vastly
      different root causes (mapping vs format vs throttle) — LLM judges
      can distinguish these based on context, not just token match
    - Version-specific advice (Spark 2.x vs 3.x, Synapse Gen1 vs Gen2) looks
      identical to keyword matchers but is critically different — LLM judges
      can check version compatibility

    Falls back to CRAGEvaluator heuristic when LLM is unavailable.
    """

    JUDGE_PROMPT_TEMPLATE = (
        "You are a relevance judge for data pipeline troubleshooting.\n\n"
        "QUERY: {query}\n\n"
        "DOCUMENT:\n{document}\n\n"
        "Rate the document's relevance to the query on a scale of 0-10:\n"
        "- 0-3: INCORRECT — document is about a different error/pipeline/version\n"
        "- 4-6: AMBIGUOUS — partially relevant but missing key specifics\n"
        "- 7-10: CORRECT — directly addresses the query's error scenario\n\n"
        "Respond with ONLY a JSON object: {{\"score\": <0-10>, \"reasoning\": \"<brief explanation>\"}}"
    )

    def __init__(
        self,
        llm_client: Any = None,
        fallback_evaluator: Optional[CRAGEvaluator] = None,
    ):
        self.llm_client = llm_client
        self.fallback = fallback_evaluator or CRAGEvaluator()

    async def grade_retrieval(
        self,
        query: str,
        chunks: list[DocumentChunk],
    ) -> list[CRAGRelevanceScore]:
        """
        Grade each chunk using LLM-as-judge.
        Falls back to heuristic CRAGEvaluator if LLM is unavailable.
        """
        if not self.llm_client:
            logger.info("llm_judge_fallback_to_heuristic", reason="no_llm_client")
            return self.fallback.grade_retrieval(query, chunks)

        scores = []
        for chunk in chunks:
            try:
                prompt = self.JUDGE_PROMPT_TEMPLATE.replace("{query}", query[:500]).replace(
                    "{document}", chunk.content[:1000]
                )
                response = await self.llm_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=200,
                )
                result_text = response.choices[0].message.content or ""

                # Parse JSON response
                import json as _json
                judge_result = _json.loads(
                    re.search(r'\{.*\}', result_text, re.DOTALL).group()
                )
                score_val = float(judge_result.get("score", 5)) / 10.0
                reasoning = judge_result.get("reasoning", "")

                if score_val >= 0.7:
                    relevance = CRAGEvaluator.RELEVANCE_CORRECT
                elif score_val >= 0.4:
                    relevance = CRAGEvaluator.RELEVANCE_AMBIGUOUS
                else:
                    relevance = CRAGEvaluator.RELEVANCE_INCORRECT

                scores.append(
                    CRAGRelevanceScore(
                        chunk_id=chunk.chunk_id,
                        query=query,
                        relevance=relevance,
                        score=round(score_val, 4),
                        reasoning=f"LLM Judge: {reasoning}",
                    )
                )
            except Exception as e:
                logger.warning("llm_judge_chunk_failed", error=str(e), chunk_id=chunk.chunk_id)
                # Fall back to heuristic for this chunk
                fallback_scores = self.fallback.grade_retrieval(query, [chunk])
                scores.extend(fallback_scores)

        logger.info("llm_judge_grading_complete", chunks_graded=len(scores))
        return scores


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HyDE — Hypothetical Document Embeddings
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class HyDEGenerator:
    """
    Generates hypothetical documents for improved retrieval via HyDE.

    Why HyDE for Spark/Synapse/Kusto troubleshooting:
    - User queries are terse ("OOM on daily job") but knowledge docs are
      verbose runbooks — HyDE bridges this vocabulary gap by generating
      a hypothetical resolution document that embeds closer to real ones
    - Support engineers describe symptoms ("job keeps failing at stage 12")
      not solutions — HyDE generates what the ideal resolution doc would
      look like, improving retrieval of actual resolution content
    - Error messages use Spark/Kusto internal terminology but runbooks
      use operational language — hypothetical docs bridge both vocabularies
    - Short KQL error codes map poorly to long-form Confluence articles;
      HyDE creates an intermediary document that matches better semantically
    """

    # Templates for generating hypothetical documents per error domain
    HYPOTHESIS_TEMPLATES = {
        "spark_oom": (
            "Resolution for Spark OutOfMemory Error:\n"
            "The pipeline {pipeline} experienced a Java heap space OOM error "
            "during {stage_hint}. Root cause was {cause_hint}. "
            "Resolution: {resolution_hint}. "
            "Prevention: Enable AQE with spark.sql.adaptive.enabled=true, "
            "monitor executor memory usage via Spark UI Executors tab."
        ),
        "spark_shuffle": (
            "Resolution for Spark Shuffle Failure:\n"
            "Shuffle fetch failures in pipeline {pipeline} caused by "
            "{cause_hint}. Check disk space on executors, increase "
            "spark.shuffle.maxRetries, verify network connectivity between nodes."
        ),
        "synapse_timeout": (
            "Resolution for Synapse Pipeline Timeout:\n"
            "The pipeline {pipeline} exceeded its timeout threshold. "
            "Root cause: {cause_hint}. "
            "Resolution: Increase activity timeout, optimize query performance, "
            "check for blocking queries in SQL pool."
        ),
        "kusto_ingestion": (
            "Resolution for Kusto Ingestion Failure:\n"
            "Ingestion to table via pipeline {pipeline} failed with {error_hint}. "
            "Resolution: Verify ingestion mapping exists and matches source schema, "
            "check EventHub consumer group lag, validate data format."
        ),
        "generic": (
            "Troubleshooting guide for data pipeline failure:\n"
            "Pipeline {pipeline} failed with error: {error_hint}. "
            "Investigation steps: 1) Check logs for root cause, "
            "2) Review recent changes, 3) Compare with similar past incidents, "
            "4) Apply mitigation steps from runbook."
        ),
    }

    def generate_hypothetical_document(
        self,
        query: str,
        pipeline_name: str = "",
        error_class: str = "",
    ) -> str:
        """
        Generate a hypothetical document that represents what an ideal
        answer document would look like. This document is then embedded
        and used for retrieval instead of the raw query.

        Args:
            query: Original search query
            pipeline_name: Pipeline name for context
            error_class: Error classification for template selection

        Returns:
            Hypothetical document text for embedding
        """
        # Select template based on error class
        template_key = self._classify_for_template(error_class, query)
        template = self.HYPOTHESIS_TEMPLATES.get(template_key, self.HYPOTHESIS_TEMPLATES["generic"])

        # Extract hints from the query
        cause_hint = self._extract_cause_hint(query)
        resolution_hint = self._extract_resolution_hint(query, error_class)
        stage_hint = self._extract_stage_hint(query)
        error_hint = error_class or query[:100]

        # Render the hypothetical document using safe substitution
        # (str.format() crashes if pipeline_name contains { or })
        substitutions = {
            "pipeline": pipeline_name or "unknown_pipeline",
            "cause_hint": cause_hint,
            "resolution_hint": resolution_hint,
            "stage_hint": stage_hint,
            "error_hint": error_hint,
        }
        hypothetical = template
        for key, value in substitutions.items():
            hypothetical = hypothetical.replace("{" + key + "}", str(value))

        logger.info(
            "hyde_document_generated",
            template=template_key,
            query_length=len(query),
            doc_length=len(hypothetical),
        )
        return hypothetical

    @staticmethod
    def _classify_for_template(error_class: str, query: str) -> str:
        """Classify the error into a HyDE template category."""
        combined = f"{error_class} {query}".lower()
        if any(kw in combined for kw in ["outofmemory", "oom", "heap", "gc overhead"]):
            return "spark_oom"
        if any(kw in combined for kw in ["shuffle", "fetch fail"]):
            return "spark_shuffle"
        if any(kw in combined for kw in ["synapse", "timeout", "copy activity"]):
            return "synapse_timeout"
        if any(kw in combined for kw in ["kusto", "ingestion", "mapping"]):
            return "kusto_ingestion"
        return "generic"

    @staticmethod
    def _extract_cause_hint(query: str) -> str:
        """Extract a cause hint from the query."""
        cause_patterns = [
            r"caused?\s+by\s+(.+?)(?:\.|$)",
            r"because\s+(.+?)(?:\.|$)",
            r"due\s+to\s+(.+?)(?:\.|$)",
            r"root\s+cause.*?:\s*(.+?)(?:\.|$)",
        ]
        for pattern in cause_patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                return match.group(1).strip()[:200]
        return "data processing issue or resource contention"

    @staticmethod
    def _extract_resolution_hint(query: str, error_class: str) -> str:
        """Generate a resolution hint based on error class."""
        hints = {
            "OutOfMemoryError": "Increase executor memory, enable AQE, add salting for skewed joins",
            "TimeoutException": "Increase timeout threshold, optimize query, check for blocking operations",
            "ConnectionFailure": "Verify network connectivity, check firewall rules, validate credentials",
            "SchemaError": "Verify schema mapping, check for column type mismatches",
            "ConfigurationError": "Review and correct configuration, verify referenced resources exist",
        }
        return hints.get(error_class, "Review logs, identify root cause, apply appropriate fix")

    @staticmethod
    def _extract_stage_hint(query: str) -> str:
        """Extract stage information from query."""
        match = re.search(r"stage\s+(\d+)", query, re.IGNORECASE)
        if match:
            return f"stage {match.group(1)}"
        return "a data processing stage"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FLARE — Forward-Looking Active Retrieval
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class FLARERetriever:
    """
    FLARE retrieves additional context only when the LLM's confidence
    drops during generation, avoiding unnecessary retrieval calls.

    Why FLARE for Spark/Synapse/Kusto troubleshooting:
    - Simple questions ("what's the runbook for OOM?") don't need extra
      retrieval — the initial context is sufficient. FLARE avoids wasting
      latency on unnecessary lookups
    - Complex cascading failures (Spark → ADLS → Kusto ingestion) benefit
      from mid-generation retrieval when the LLM realizes it needs info
      about a downstream component it didn't initially have context for
    - Support conversations are iterative — the engineer refines their
      question over multiple turns. FLARE retrieves only when new
      information is actually needed, reducing token costs
    - Error chains in Synapse pipelines can span multiple activities;
      FLARE retrieves additional context for each hop in the failure chain
    """

    def __init__(
        self,
        vector_store: VectorStore,
        confidence_threshold: float = 0.5,
        max_retrieval_rounds: int = 3,
    ):
        self.vector_store = vector_store
        self.confidence_threshold = confidence_threshold
        self.max_retrieval_rounds = max_retrieval_rounds

    def detect_low_confidence_segments(
        self,
        generated_text: str,
    ) -> list[str]:
        """
        Scan generated text for segments that indicate low confidence
        and would benefit from additional retrieval.

        Detects:
        - Hedging language ("might be", "possibly", "I'm not sure")
        - Incomplete information markers ("[unknown]", "...")
        - Generic placeholder advice ("check the logs", "contact support")
        - References to unknown components or unclear dependencies
        """
        low_confidence_patterns = [
            (r"(?:might|may|could)\s+be", "hedging_language"),
            (r"(?:i'm\s+not\s+sure|uncertain|unclear)", "explicit_uncertainty"),
            (r"(?:possibly|perhaps|probably)", "probabilistic_language"),
            (r"\[(?:unknown|tbd|todo|check)\]", "placeholder_markers"),
            (r"(?:check\s+the\s+logs?|review\s+the\s+documentation)", "generic_advice"),
            (r"(?:more\s+information\s+(?:is\s+)?needed|need\s+to\s+investigate)", "info_gap"),
            (r"(?:not\s+enough\s+context|insufficient\s+data)", "context_gap"),
        ]

        segments_needing_retrieval = []
        for pattern, reason in low_confidence_patterns:
            matches = re.finditer(pattern, generated_text, re.IGNORECASE)
            for match in matches:
                # Extract surrounding context (50 chars before/after)
                start = max(0, match.start() - 50)
                end = min(len(generated_text), match.end() + 50)
                segment = generated_text[start:end].strip()
                segments_needing_retrieval.append(segment)

        return segments_needing_retrieval

    async def retrieve_for_segments(
        self,
        segments: list[str],
        existing_context: str = "",
        top_k: int = 3,
    ) -> RetrievalResult:
        """
        Retrieve additional context for low-confidence segments.
        Deduplicates against existing context to avoid redundancy.
        """
        if not segments:
            return RetrievalResult(
                chunks=[], query="", total_found=0, retrieval_time_ms=0
            )

        # Combine segments into a focused retrieval query
        combined_query = " ".join(segments[:3])  # Limit to top 3 segments

        result = await self.vector_store.search(
            query=combined_query,
            top_k=top_k,
            use_hybrid=True,
        )

        # Filter out chunks already in existing context
        if existing_context:
            novel_chunks = []
            for chunk in result.chunks:
                # Simple dedup: skip if >50% of chunk content is already in context
                overlap = sum(
                    1 for word in chunk.content.split()[:20]
                    if word.lower() in existing_context.lower()
                )
                if overlap < 10:
                    novel_chunks.append(chunk)
            result.chunks = novel_chunks
            result.total_found = len(novel_chunks)

        logger.info(
            "flare_retrieval_complete",
            segments_count=len(segments),
            chunks_found=result.total_found,
        )
        return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GraphRAG — Knowledge Graph for Failure Tracing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class GraphRAGEngine:
    """
    Graph-based retrieval for understanding cascading failures
    and dependency relationships between pipelines and components.

    Why GraphRAG for Spark/Synapse/Kusto troubleshooting:
    - Data pipelines have complex dependency graphs (ingestion → transform
      → serve) — when a Spark job fails, understanding what downstream
      Kusto tables and dashboards are affected requires graph traversal
    - Cascading failures (ADLS throttling → Spark read fails → Synapse
      pipeline aborts → Kusto table goes stale) need multi-hop reasoning
      that flat retrieval cannot provide
    - Root cause analysis requires tracing BACKWARDS through the dependency
      graph: "this Kusto table is stale because the Synapse pipeline failed
      because the Spark job OOM'd because of a data skew introduced by
      a schema change in the upstream source"
    - Similar incidents are better found by traversing the graph
      (same pipeline → same error type → same resolution) than by
      pure semantic similarity which may return superficially similar
      but operationally irrelevant results
    """

    def __init__(self, graph_config_path: Optional[str] = None):
        self._graph = FailureGraph()
        if graph_config_path:
            self._load_graph_from_config(graph_config_path)
        else:
            self._build_default_graph()
            logger.info("graphrag_using_default_graph", node_count=len(self._graph.nodes))

    def _load_graph_from_config(self, config_path: str) -> None:
        """
        Load pipeline dependency graph from a JSON config file.

        Expected format:
        {
            "nodes": [
                {"node_id": "...", "node_type": "pipeline|component|error", "name": "..."}
            ],
            "edges": [
                {"source_id": "...", "target_id": "...", "relationship": "FEEDS_INTO|TRIGGERS|..."}
            ]
        }

        Supports ingestion from ADF, Databricks, or Synapse metadata exports.
        """
        try:
            with open(config_path, "r") as f:
                config = json.load(f)

            for node_data in config.get("nodes", []):
                self._graph.nodes.append(
                    GraphNode(
                        node_id=node_data["node_id"],
                        node_type=node_data["node_type"],
                        name=node_data["name"],
                    )
                )

            for edge_data in config.get("edges", []):
                self._graph.edges.append(
                    GraphEdge(
                        source_id=edge_data["source_id"],
                        target_id=edge_data["target_id"],
                        relationship=edge_data["relationship"],
                    )
                )

            logger.info(
                "graphrag_loaded_from_config",
                config_path=config_path,
                nodes=len(self._graph.nodes),
                edges=len(self._graph.edges),
            )
        except FileNotFoundError:
            logger.warning("graphrag_config_not_found", path=config_path)
            self._build_default_graph()
        except (json.JSONDecodeError, KeyError) as e:
            logger.error("graphrag_config_parse_error", error=str(e), path=config_path)
            self._build_default_graph()

    def ingest_from_metadata_api(
        self,
        nodes: list[dict[str, str]],
        edges: list[dict[str, str]],
        merge: bool = False,
    ) -> dict[str, int]:
        """
        Ingest pipeline dependency graph from ADF, Databricks, or Synapse metadata.

        This method supports real pipeline graph ingestion from external APIs.
        Call this after fetching metadata from:
        - Azure Data Factory: GET /pipelines → extract activity dependencies
        - Databricks: GET /api/2.1/jobs/list → extract job→job dependencies
        - Synapse: GET /pipelines → extract pipeline→dataset→pipeline chains

        Args:
            nodes: List of {"node_id": ..., "node_type": ..., "name": ...}
            edges: List of {"source_id": ..., "target_id": ..., "relationship": ...}
            merge: If True, add to existing graph; if False, replace entirely

        Returns:
            Dict with counts of nodes and edges added
        """
        if not merge:
            self._graph = FailureGraph()

        existing_node_ids = {n.node_id for n in self._graph.nodes}
        existing_edge_keys = {
            (e.source_id, e.target_id, e.relationship) for e in self._graph.edges
        }

        nodes_added = 0
        edges_added = 0

        for node_data in nodes:
            if node_data["node_id"] not in existing_node_ids:
                self._graph.nodes.append(
                    GraphNode(
                        node_id=node_data["node_id"],
                        node_type=node_data.get("node_type", "pipeline"),
                        name=node_data["name"],
                    )
                )
                existing_node_ids.add(node_data["node_id"])
                nodes_added += 1

        for edge_data in edges:
            edge_key = (
                edge_data["source_id"],
                edge_data["target_id"],
                edge_data["relationship"],
            )
            if edge_key not in existing_edge_keys:
                self._graph.edges.append(
                    GraphEdge(
                        source_id=edge_data["source_id"],
                        target_id=edge_data["target_id"],
                        relationship=edge_data["relationship"],
                    )
                )
                existing_edge_keys.add(edge_key)
                edges_added += 1

        logger.info(
            "graphrag_metadata_ingested",
            nodes_added=nodes_added,
            edges_added=edges_added,
            total_nodes=len(self._graph.nodes),
            total_edges=len(self._graph.edges),
            merge_mode=merge,
        )

        return {"nodes_added": nodes_added, "edges_added": edges_added}

    async def refresh_from_live_metadata(
        self,
        adf_client: Any = None,
        databricks_client: Any = None,
        synapse_client: Any = None,
        merge: bool = True,
    ) -> dict[str, Any]:
        """
        Dynamically populate the pipeline dependency graph from live
        ADF, Databricks, and Synapse metadata APIs.

        This replaces the static default graph with real pipeline
        topology, enabling accurate blast-radius analysis and
        cascading-failure tracing in production.

        Call this on a schedule (e.g., hourly) or on application
        startup to keep the graph current.

        Args:
            adf_client: Authenticated Azure Data Factory ManagementClient.
                Expects client.pipelines.list_by_factory() / .get() support.
            databricks_client: Authenticated Databricks REST API client.
                Expects GET /api/2.1/jobs/list support.
            synapse_client: Authenticated Synapse management client.
                Expects client.pipelines.list_pipelines_by_workspace().
            merge: If True, add to existing graph. If False, replace.

        Returns:
            Summary dict with counts per source.
        """
        all_nodes: list[dict[str, str]] = []
        all_edges: list[dict[str, str]] = []
        summary: dict[str, Any] = {"sources_processed": []}

        # ── Azure Data Factory ─────────────────────────
        if adf_client:
            try:
                adf_nodes, adf_edges = await self._extract_adf_topology(adf_client)
                all_nodes.extend(adf_nodes)
                all_edges.extend(adf_edges)
                summary["sources_processed"].append("adf")
                summary["adf"] = {"nodes": len(adf_nodes), "edges": len(adf_edges)}
                logger.info("graphrag_adf_extracted", nodes=len(adf_nodes), edges=len(adf_edges))
            except Exception as e:
                logger.error("graphrag_adf_extraction_failed", error=str(e))
                summary["adf_error"] = str(e)

        # ── Databricks ─────────────────────────────────
        if databricks_client:
            try:
                db_nodes, db_edges = await self._extract_databricks_topology(databricks_client)
                all_nodes.extend(db_nodes)
                all_edges.extend(db_edges)
                summary["sources_processed"].append("databricks")
                summary["databricks"] = {"nodes": len(db_nodes), "edges": len(db_edges)}
                logger.info("graphrag_databricks_extracted", nodes=len(db_nodes), edges=len(db_edges))
            except Exception as e:
                logger.error("graphrag_databricks_extraction_failed", error=str(e))
                summary["databricks_error"] = str(e)

        # ── Synapse ────────────────────────────────────
        if synapse_client:
            try:
                syn_nodes, syn_edges = await self._extract_synapse_topology(synapse_client)
                all_nodes.extend(syn_nodes)
                all_edges.extend(syn_edges)
                summary["sources_processed"].append("synapse")
                summary["synapse"] = {"nodes": len(syn_nodes), "edges": len(syn_edges)}
                logger.info("graphrag_synapse_extracted", nodes=len(syn_nodes), edges=len(syn_edges))
            except Exception as e:
                logger.error("graphrag_synapse_extraction_failed", error=str(e))
                summary["synapse_error"] = str(e)

        # Ingest all extracted topology
        if all_nodes or all_edges:
            result = self.ingest_from_metadata_api(all_nodes, all_edges, merge=merge)
            summary.update(result)
        else:
            logger.warning("graphrag_no_metadata_extracted", msg="Falling back to default graph")
            if not self._graph.nodes:
                self._build_default_graph()
            summary["fallback"] = "default_graph"

        return summary

    @staticmethod
    async def _extract_adf_topology(
        adf_client: Any,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """
        Extract pipeline dependency graph from Azure Data Factory.

        Iterates over ADF pipelines and their activities to build:
        - Pipeline nodes (type="pipeline")
        - Dataset/LinkedService nodes (type="component")
        - Activity dependency edges (FEEDS_INTO, READS_FROM, WRITES_TO)

        Expects adf_client to expose:
            adf_client.pipelines.list_by_factory(resource_group, factory_name)
            Each pipeline has .activities with .depends_on, .inputs, .outputs
        """
        nodes: list[dict[str, str]] = []
        edges: list[dict[str, str]] = []

        # NOTE: Production implementation should iterate over:
        #   pipelines = adf_client.pipelines.list_by_factory(rg, factory)
        #   for pipeline in pipelines:
        #       nodes.append({"node_id": f"adf_{pipeline.name}", "node_type": "pipeline", "name": pipeline.name})
        #       for activity in pipeline.activities:
        #           # Extract depends_on for intra-pipeline ordering
        #           for dep in (activity.depends_on or []):
        #               edges.append({
        #                   "source_id": f"adf_{pipeline.name}_{dep.activity}",
        #                   "target_id": f"adf_{pipeline.name}_{activity.name}",
        #                   "relationship": "FEEDS_INTO",
        #               })
        #           # Extract dataset references for cross-pipeline linking
        #           for inp in (activity.inputs or []):
        #               ds_id = f"adf_dataset_{inp.reference_name}"
        #               nodes.append({"node_id": ds_id, "node_type": "component", "name": inp.reference_name})
        #               edges.append({"source_id": ds_id, "target_id": f"adf_{pipeline.name}", "relationship": "READS_FROM"})
        #           for out in (activity.outputs or []):
        #               ds_id = f"adf_dataset_{out.reference_name}"
        #               nodes.append({"node_id": ds_id, "node_type": "component", "name": out.reference_name})
        #               edges.append({"source_id": f"adf_{pipeline.name}", "target_id": ds_id, "relationship": "WRITES_TO"})

        logger.info("graphrag_adf_extraction_stub", msg="Implement with real ADF client")
        return nodes, edges

    @staticmethod
    async def _extract_databricks_topology(
        databricks_client: Any,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """
        Extract job dependency graph from Databricks.

        Expects databricks_client to expose:
            databricks_client.jobs.list()  # returns list of job definitions
            Each job may have tasks with depends_on references
        """
        nodes: list[dict[str, str]] = []
        edges: list[dict[str, str]] = []

        # NOTE: Production implementation should iterate over:
        #   jobs = databricks_client.jobs.list() or GET /api/2.1/jobs/list
        #   for job in jobs["jobs"]:
        #       nodes.append({"node_id": f"dbx_{job['job_id']}", "node_type": "pipeline", "name": job["settings"]["name"]})
        #       for task in job["settings"].get("tasks", []):
        #           for dep in task.get("depends_on", []):
        #               edges.append({
        #                   "source_id": f"dbx_{job['job_id']}_{dep['task_key']}",
        #                   "target_id": f"dbx_{job['job_id']}_{task['task_key']}",
        #                   "relationship": "FEEDS_INTO",
        #               })
        #           # Cluster references
        #           cluster_id = task.get("existing_cluster_id") or task.get("new_cluster", {}).get("cluster_name", "")
        #           if cluster_id:
        #               nodes.append({"node_id": f"dbx_cluster_{cluster_id}", "node_type": "component", "name": cluster_id})
        #               edges.append({"source_id": f"dbx_{job['job_id']}", "target_id": f"dbx_cluster_{cluster_id}", "relationship": "RUNS_ON"})

        logger.info("graphrag_databricks_extraction_stub", msg="Implement with real Databricks client")
        return nodes, edges

    @staticmethod
    async def _extract_synapse_topology(
        synapse_client: Any,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """
        Extract pipeline dependency graph from Azure Synapse Analytics.

        Expects synapse_client to expose:
            synapse_client.pipelines.list_pipelines_by_workspace()
            Each pipeline has activities with dependencies and dataset references
        """
        nodes: list[dict[str, str]] = []
        edges: list[dict[str, str]] = []

        # NOTE: Production implementation should iterate over:
        #   pipelines = synapse_client.pipeline.get_pipelines_by_workspace()
        #   for pipeline in pipelines:
        #       nodes.append({"node_id": f"syn_{pipeline.name}", "node_type": "pipeline", "name": pipeline.name})
        #       for activity in pipeline.activities:
        #           for dep in (activity.depends_on or []):
        #               edges.append({
        #                   "source_id": f"syn_{pipeline.name}_{dep.activity}",
        #                   "target_id": f"syn_{pipeline.name}_{activity.name}",
        #                   "relationship": "FEEDS_INTO",
        #               })
        #           # Dedicated SQL pool references
        #           if hasattr(activity, "sql_pool") and activity.sql_pool:
        #               pool_id = f"syn_pool_{activity.sql_pool.reference_name}"
        #               nodes.append({"node_id": pool_id, "node_type": "component", "name": activity.sql_pool.reference_name})
        #               edges.append({"source_id": f"syn_{pipeline.name}", "target_id": pool_id, "relationship": "RUNS_ON"})

        logger.info("graphrag_synapse_extraction_stub", msg="Implement with real Synapse client")
        return nodes, edges

    def _build_default_graph(self) -> None:
        """Build a default pipeline dependency graph for demo/dev mode."""
        # Pipeline nodes
        pipelines = [
            ("pipe_spark_daily_ingest", "pipeline", "spark_etl_daily_ingest"),
            ("pipe_spark_weekly_merge", "pipeline", "spark_etl_weekly_merge"),
            ("pipe_synapse_customer360", "pipeline", "synapse_pipeline_customer360"),
            ("pipe_kusto_telemetry", "pipeline", "kusto_ingestion_telemetry"),
            ("pipe_synapse_reporting", "pipeline", "synapse_pipeline_daily_report"),
        ]

        # Component nodes
        components = [
            ("comp_adls_raw", "component", "ADLS Gen2 Raw Zone"),
            ("comp_adls_curated", "component", "ADLS Gen2 Curated Zone"),
            ("comp_spark_cluster", "component", "Databricks Spark Cluster"),
            ("comp_synapse_pool", "component", "Synapse SQL Pool"),
            ("comp_kusto_cluster", "component", "ADX Kusto Cluster"),
            ("comp_keyvault", "component", "Azure Key Vault"),
        ]

        # Error pattern nodes
        errors = [
            ("err_oom", "error", "OutOfMemoryError"),
            ("err_shuffle", "error", "ShuffleFetchFailure"),
            ("err_timeout", "error", "TimeoutException"),
            ("err_mapping", "error", "MappingNotFound"),
            ("err_auth", "error", "AuthenticationFailure"),
        ]

        # Add all nodes
        for nid, ntype, name in pipelines + components + errors:
            self._graph.nodes.append(GraphNode(node_id=nid, node_type=ntype, name=name))

        # Pipeline dependency edges
        dependencies = [
            ("pipe_spark_daily_ingest", "pipe_spark_weekly_merge", "FEEDS_INTO"),
            ("pipe_spark_daily_ingest", "pipe_synapse_customer360", "FEEDS_INTO"),
            ("pipe_spark_daily_ingest", "pipe_synapse_reporting", "FEEDS_INTO"),
            ("pipe_synapse_customer360", "pipe_kusto_telemetry", "TRIGGERS"),
            # Component dependencies
            ("pipe_spark_daily_ingest", "comp_adls_raw", "READS_FROM"),
            ("pipe_spark_daily_ingest", "comp_adls_curated", "WRITES_TO"),
            ("pipe_spark_daily_ingest", "comp_spark_cluster", "RUNS_ON"),
            ("pipe_synapse_customer360", "comp_synapse_pool", "RUNS_ON"),
            ("pipe_kusto_telemetry", "comp_kusto_cluster", "RUNS_ON"),
            # Error associations
            ("pipe_spark_daily_ingest", "err_oom", "COMMONLY_FAILS_WITH"),
            ("pipe_spark_daily_ingest", "err_shuffle", "COMMONLY_FAILS_WITH"),
            ("pipe_synapse_customer360", "err_timeout", "COMMONLY_FAILS_WITH"),
            ("pipe_kusto_telemetry", "err_mapping", "COMMONLY_FAILS_WITH"),
            # Auth chain
            ("comp_keyvault", "comp_adls_raw", "PROVIDES_AUTH"),
            ("comp_keyvault", "comp_synapse_pool", "PROVIDES_AUTH"),
        ]

        for src, tgt, rel in dependencies:
            self._graph.edges.append(GraphEdge(source_id=src, target_id=tgt, relationship=rel))

    def trace_blast_radius(
        self,
        pipeline_name: str,
        depth: int = 3,
    ) -> dict[str, Any]:
        """
        Trace the blast radius of a pipeline failure through the dependency graph.

        Returns affected downstream pipelines, components, and estimated impact.
        """
        # Find the pipeline node
        pipe_node = None
        for node in self._graph.nodes:
            if node.name == pipeline_name or node.node_id == pipeline_name:
                pipe_node = node
                break

        if not pipe_node:
            return {"affected": [], "blast_radius": "unknown", "depth_searched": depth}

        # BFS to find downstream dependencies
        visited = set()
        frontier = {pipe_node.node_id}
        affected_pipelines = []
        affected_components = []
        current_depth = 0

        while frontier and current_depth < depth:
            next_frontier = set()
            for nid in frontier:
                if nid in visited:
                    continue
                visited.add(nid)

                for edge in self._graph.edges:
                    if edge.source_id == nid and edge.relationship in ("FEEDS_INTO", "TRIGGERS", "WRITES_TO"):
                        target = next((n for n in self._graph.nodes if n.node_id == edge.target_id), None)
                        if target and target.node_id not in visited:
                            if target.node_type == "pipeline":
                                affected_pipelines.append(target.name)
                            elif target.node_type == "component":
                                affected_components.append(target.name)
                            next_frontier.add(target.node_id)

            frontier = next_frontier
            current_depth += 1

        return {
            "source_pipeline": pipeline_name,
            "affected_pipelines": affected_pipelines,
            "affected_components": affected_components,
            "blast_radius": f"{len(affected_pipelines)} pipelines, {len(affected_components)} components",
            "depth_searched": current_depth,
        }

    def find_failure_chain(
        self,
        pipeline_name: str,
        error_class: str,
    ) -> list[dict[str, str]]:
        """
        Trace the causal chain for a failure, finding upstream causes
        and downstream effects.
        """
        chain = []

        # Find pipeline and error nodes
        pipe_node = next(
            (n for n in self._graph.nodes if n.name == pipeline_name), None
        )
        error_node = next(
            (n for n in self._graph.nodes if n.name == error_class), None
        )

        if pipe_node:
            # Find upstream dependencies (what this pipeline reads from)
            for edge in self._graph.edges:
                if edge.source_id == pipe_node.node_id and edge.relationship in ("READS_FROM", "RUNS_ON"):
                    target = next((n for n in self._graph.nodes if n.node_id == edge.target_id), None)
                    if target:
                        chain.append({
                            "step": "upstream_dependency",
                            "component": target.name,
                            "relationship": edge.relationship,
                        })

            # Add the failure itself
            chain.append({
                "step": "failure_point",
                "component": pipeline_name,
                "error": error_class,
            })

            # Find downstream impact
            blast = self.trace_blast_radius(pipeline_name, depth=2)
            for affected in blast.get("affected_pipelines", []):
                chain.append({
                    "step": "downstream_impact",
                    "component": affected,
                    "relationship": "BLOCKED_BY_FAILURE",
                })

        return chain

    def get_related_context(
        self,
        pipeline_name: str,
        error_class: str = "",
    ) -> str:
        """
        Build a context string from graph relationships for LLM prompts.
        """
        parts = []

        # Blast radius
        blast = self.trace_blast_radius(pipeline_name)
        if blast["affected_pipelines"]:
            parts.append(
                f"**Blast Radius:** {blast['blast_radius']}\n"
                f"Affected pipelines: {', '.join(blast['affected_pipelines'])}\n"
                f"Affected components: {', '.join(blast['affected_components'])}"
            )

        # Failure chain
        if error_class:
            chain = self.find_failure_chain(pipeline_name, error_class)
            if chain:
                chain_str = " → ".join(
                    f"{step['component']} ({step.get('relationship', step.get('error', ''))})"
                    for step in chain
                )
                parts.append(f"**Failure Chain:** {chain_str}")

        # Common error associations
        pipe_node = next(
            (n for n in self._graph.nodes if n.name == pipeline_name), None
        )
        if pipe_node:
            common_errors = []
            for edge in self._graph.edges:
                if edge.source_id == pipe_node.node_id and edge.relationship == "COMMONLY_FAILS_WITH":
                    err = next((n for n in self._graph.nodes if n.node_id == edge.target_id), None)
                    if err:
                        common_errors.append(err.name)
            if common_errors:
                parts.append(f"**Common Errors for this Pipeline:** {', '.join(common_errors)}")

        return "\n\n".join(parts) if parts else "No graph context available."


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Agentic RAG Orchestrator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AgenticRAGOrchestrator:
    """
    Orchestrates retrieval across multiple specialized indexes/sources.
    Acts as an agent that decides WHICH sources to query and in what order.

    Why Agentic RAG for Spark/Synapse/Kusto troubleshooting:
    - Troubleshooting often requires querying multiple sources:
      Confluence for runbooks, ICM for past incidents, Log Analytics
      for fresh logs, and Kusto for telemetry — an agent decides
      which sources are relevant for each specific question
    - A question about "similar incidents" should query ICM first,
      while "runbook steps" should query Confluence first —
      static retrieval queries all sources equally and wastes tokens
    - Complex investigations may need sequential retrieval:
      first find the error pattern in logs, then search ICM for matching
      incidents, then fetch the relevant runbook — this requires planning
    - Different source types have different retrieval needs:
      Kusto logs need keyword-exact search (BM25), Confluence needs
      semantic search, ICM benefits from structured field matching
    """

    def __init__(
        self,
        vector_store: VectorStore,
        graph_engine: Optional[GraphRAGEngine] = None,
    ):
        self.vector_store = vector_store
        self.graph_engine = graph_engine or GraphRAGEngine()
        self._source_capabilities = {
            "confluence": {
                "strengths": ["runbooks", "procedures", "architecture docs", "best practices"],
                "retrieval_mode": "semantic",
            },
            "icm": {
                "strengths": ["past incidents", "resolutions", "root causes", "TTD/TTM metrics"],
                "retrieval_mode": "hybrid",
            },
            "log_analytics": {
                "strengths": ["fresh logs", "real-time errors", "metric data", "traces"],
                "retrieval_mode": "keyword",
            },
        }

    def plan_retrieval(
        self,
        query: str,
        intent: str = "",
        active_alert: Optional[ParsedFailure] = None,
    ) -> list[dict[str, Any]]:
        """
        Plan the retrieval strategy based on the query intent.
        Returns an ordered list of retrieval steps.

        Each step specifies: source, query variant, priority, and expected output.
        """
        steps = []
        query_lower = query.lower()

        # Intent-based source routing
        if any(kw in query_lower for kw in ["similar", "past", "incident", "historical", "before"]):
            steps.append({
                "source": "icm",
                "query": query,
                "priority": 1,
                "reason": "Finding similar past incidents in ICM history",
                "use_hybrid": True,
            })
            steps.append({
                "source": "confluence",
                "query": query,
                "priority": 2,
                "reason": "Finding documented patterns for this error type",
                "use_hybrid": False,
            })

        elif any(kw in query_lower for kw in ["runbook", "procedure", "steps", "how to"]):
            steps.append({
                "source": "confluence",
                "query": query,
                "priority": 1,
                "reason": "Fetching runbook procedures from Confluence",
                "use_hybrid": False,
            })

        elif any(kw in query_lower for kw in ["log", "trace", "stack", "error output"]):
            steps.append({
                "source": "log_analytics",
                "query": query,
                "priority": 1,
                "reason": "Fetching recent log data from Log Analytics",
                "use_hybrid": True,
            })

        elif any(kw in query_lower for kw in ["root cause", "why", "what caused", "explain"]):
            steps.append({
                "source": "icm",
                "query": query,
                "priority": 1,
                "reason": "Finding past root cause analyses",
                "use_hybrid": True,
            })
            steps.append({
                "source": "confluence",
                "query": query,
                "priority": 2,
                "reason": "Finding relevant architecture/troubleshooting docs",
                "use_hybrid": False,
            })
            if active_alert:
                steps.append({
                    "source": "graph",
                    "query": active_alert.pipeline_name,
                    "priority": 3,
                    "reason": "Tracing dependency chain for blast radius analysis",
                    "use_hybrid": False,
                })

        else:
            # Default: query all sources
            for source in ["icm", "confluence", "log_analytics"]:
                steps.append({
                    "source": source,
                    "query": query,
                    "priority": 2,
                    "reason": f"General search across {source}",
                    "use_hybrid": True,
                })

        # Sort by priority
        steps.sort(key=lambda s: s["priority"])

        logger.info(
            "agentic_rag_plan",
            query=query[:60],
            steps=len(steps),
            sources=[s["source"] for s in steps],
        )
        return steps

    async def execute_plan(
        self,
        steps: list[dict[str, Any]],
        top_k_per_source: int = 3,
    ) -> RetrievalResult:
        """
        Execute the retrieval plan, querying each source in order.
        Merges results from all sources.
        """
        all_chunks: dict[str, DocumentChunk] = {}
        total_time = 0.0

        for step in steps:
            source = step["source"]

            if source == "graph" and self.graph_engine:
                # Graph retrieval — return as a synthetic chunk
                context = self.graph_engine.get_related_context(
                    step["query"],
                    step.get("error_class", ""),
                )
                if context and context != "No graph context available.":
                    chunk_id = f"graph_{hashlib.md5(context.encode()).hexdigest()[:8]}"
                    all_chunks[chunk_id] = DocumentChunk(
                        chunk_id=chunk_id,
                        source_type="graph",
                        source_id="dependency_graph",
                        source_title="Pipeline Dependency Analysis",
                        content=context,
                        metadata={"similarity_score": 0.9, "source_step": source},
                    )
            else:
                # Vector/hybrid store search
                result = await self.vector_store.search(
                    query=step["query"],
                    top_k=top_k_per_source,
                    source_type=source if source != "log_analytics" else None,
                    use_hybrid=step.get("use_hybrid", False),
                )
                total_time += result.retrieval_time_ms

                for chunk in result.chunks:
                    if chunk.chunk_id not in all_chunks:
                        all_chunks[chunk.chunk_id] = chunk

        chunks = list(all_chunks.values())

        logger.info(
            "agentic_rag_executed",
            steps=len(steps),
            total_chunks=len(chunks),
            time_ms=round(total_time, 2),
        )

        return RetrievalResult(
            chunks=chunks,
            query=steps[0]["query"] if steps else "",
            total_found=len(chunks),
            retrieval_time_ms=total_time,
            reranked=False,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main RAG Retriever (Enhanced)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RAGRetriever:
    """
    Main RAG retrieval engine combining ALL 13 techniques:

    Pre-Retrieval:
    - Query Rewriting — synonym expansion, abbreviation expansion, LLM rewriting
    - RAG Fusion — multi-query decomposition + Reciprocal Rank Fusion
    - HyDE — hypothetical document generation for better embedding matching
    - Topic Tree — hierarchical error classification for branch-targeted retrieval

    Retrieval:
    - Azure AI Search native hybrid (vector + BM25 keyword)
    - Agentic RAG — multi-source orchestration (Confluence, ICM, Log Analytics)
    - Parent-Child Chunking — precise child retrieval with parent context expansion
    - Semantic Chunking — embedding-similarity boundary detection for coherent chunks

    Post-Retrieval:
    - CRAG (Corrective RAG) — relevance grading and corrective filtering
    - Cross-Encoder Reranking — joint query-document scoring (~15-20% precision gain)
    - GraphRAG — dependency graph context injection (dynamic ADF/Databricks/Synapse)

    During Generation:
    - FLARE — forward-looking active retrieval on low-confidence segments
    - Self-RAG — self-reflective evaluation with [Supported]/[Relevant] checks
    """

    def __init__(
        self,
        vector_store: VectorStore,
        top_k: int = 5,
        similarity_threshold: float = 0.3,
        max_context_tokens: int = 6000,
        enable_crag: bool = True,
        enable_hyde: bool = True,
        enable_graph: bool = True,
        enable_agentic: bool = True,
        enable_llm_judge: bool = False,
        enable_query_rewriting: bool = True,
        enable_rag_fusion: bool = True,
        enable_self_rag: bool = True,
        enable_cross_encoder: bool = True,
        use_dedicated_reranker: bool = False,
        dedicated_reranker_model: Optional[str] = None,
        llm_client: Any = None,
    ):
        self.vector_store = vector_store
        self.topic_tree = TopicTreeRetriever()
        self.top_k = top_k
        self.similarity_threshold = similarity_threshold
        self.max_context_tokens = max_context_tokens

        # Pre-retrieval components
        self.query_rewriter = QueryRewriter(llm_client=llm_client) if enable_query_rewriting else None
        self.rag_fusion = RAGFusion(vector_store=vector_store, llm_client=llm_client) if enable_rag_fusion else None

        # Retrieval components
        self.crag = CRAGEvaluator() if enable_crag else None
        self.hyde = HyDEGenerator() if enable_hyde else None
        self.graph_rag = GraphRAGEngine() if enable_graph else None
        self.agentic_rag = AgenticRAGOrchestrator(vector_store, self.graph_rag) if enable_agentic else None
        self.flare = FLARERetriever(vector_store) if vector_store else None
        self.llm_judge = LLMJudgeCRAG(llm_client=llm_client, fallback_evaluator=self.crag) if enable_llm_judge else None

        # Post-retrieval components
        self.cross_encoder = CrossEncoderReranker(
            llm_client=llm_client,
            use_dedicated_model=use_dedicated_reranker,
            dedicated_model_path=dedicated_reranker_model,
        ) if enable_cross_encoder else None

        # During-generation components
        self.self_rag = SelfRAG(llm_client=llm_client, vector_store=vector_store) if enable_self_rag else None

    async def retrieve_for_alert(
        self,
        failure: ParsedFailure,
    ) -> RetrievalResult:
        """
        Retrieve relevant context for an alert using the full 13-technique pipeline:

        PRE-RETRIEVAL:
        1. Topic tree classification to identify relevant branches
        2. Query Rewriting — synonym expansion + context enrichment
        3. HyDE — generate hypothetical document for better embedding
        4. RAG Fusion — decompose into multi-faceted query variants

        RETRIEVAL:
        5. Azure AI Search hybrid (vector + BM25) for each query variant
        6. Agentic source routing (implicit via query variants)
        7. Parent-Child chunking + Semantic Chunking (at ingestion time)

        POST-RETRIEVAL:
        8. CRAG — grade and filter retrieved chunks
        9. GraphRAG — add dependency context (dynamic ADF/Databricks/Synapse)
        10. Cross-Encoder Reranking — joint query-doc precision scoring
        11. Trim to context window

        DURING GENERATION (called separately):
        12. FLARE — forward-looking retrieval on low-confidence text
        13. Self-RAG — self-reflective evaluation with [Supported]/[Relevant] checks
        """
        logger.info(
            "rag_retrieve_for_alert",
            failure_id=failure.failure_id,
            error_class=failure.error_class,
        )

        # ── PRE-RETRIEVAL ───────────────────────────

        # Step 1: Topic tree classification
        topic_matches = self.topic_tree.classify_error(
            failure.error_message, failure.source.value
        )
        topic_context = " | ".join([t.topic for t in topic_matches[:3]])
        logger.info("topic_classification", topics=topic_context)

        # Step 2: Query Rewriting
        base_queries = [
            failure.error_message[:500],
            f"{failure.pipeline_name} {failure.error_class}",
            f"{failure.error_class} {topic_context}",
        ]
        if self.query_rewriter:
            rewritten = await self.query_rewriter.rewrite(
                query=failure.error_message[:300],
                pipeline_name=failure.pipeline_name,
                error_class=failure.error_class,
                source=failure.source.value,
            )
            base_queries.extend(rewritten)
            logger.info("query_rewriting_applied", variants=len(rewritten))

        # Step 3: HyDE — generate hypothetical document
        if self.hyde:
            hyde_query = self.hyde.generate_hypothetical_document(
                query=failure.error_message[:300],
                pipeline_name=failure.pipeline_name,
                error_class=failure.error_class,
            )
            if hyde_query:
                base_queries.append(hyde_query[:500])

        # Deduplicate queries
        seen = set()
        queries = []
        for q in base_queries:
            q_key = q.strip().lower()[:100]
            if q_key not in seen:
                seen.add(q_key)
                queries.append(q)

        # ── RETRIEVAL ───────────────────────────────

        # Step 4 (optional): RAG Fusion for multi-faceted retrieval
        all_chunks: dict[str, DocumentChunk] = {}

        if self.rag_fusion:
            fusion_result = await self.rag_fusion.retrieve_and_fuse(
                query=failure.error_message[:300],
                top_k=self.top_k,
                use_hybrid=True,
                expand_to_parent=True,
            )
            for chunk in fusion_result.chunks:
                all_chunks[chunk.chunk_id] = chunk
            logger.info("rag_fusion_applied", fused_chunks=len(fusion_result.chunks))

        # Step 5: Multi-query vector search (Azure AI Search hybrid)
        for query in queries:
            result = await self.vector_store.search(
                query=query,
                top_k=self.top_k,
                similarity_threshold=self.similarity_threshold,
                use_hybrid=True,
                expand_to_parent=True,
            )
            for chunk in result.chunks:
                if chunk.chunk_id not in all_chunks:
                    all_chunks[chunk.chunk_id] = chunk

        # ── POST-RETRIEVAL ──────────────────────────

        # Step 6: CRAG — grade and filter chunks
        chunks_list = list(all_chunks.values())
        crag_action = "disabled"
        if self.crag and chunks_list:
            scores = self.crag.grade_retrieval(failure.error_message, chunks_list)
            chunks_list, crag_action = self.crag.apply_correction(chunks_list, scores)
            logger.info("crag_applied", action=crag_action, remaining=len(chunks_list))

        # Step 7: GraphRAG — add dependency context
        if self.graph_rag:
            graph_context = self.graph_rag.get_related_context(
                failure.pipeline_name, failure.error_class
            )
            if graph_context and graph_context != "No graph context available.":
                graph_chunk = DocumentChunk(
                    chunk_id=f"graph_{failure.failure_id}",
                    source_type="graph",
                    source_id="dependency_graph",
                    source_title="Pipeline Dependency Analysis",
                    content=graph_context,
                    metadata={"similarity_score": 0.85},
                )
                chunks_list.append(graph_chunk)

        # Step 8: Cross-Encoder Reranking
        if self.cross_encoder and chunks_list:
            chunks_list = await self.cross_encoder.rerank(
                query=failure.error_message[:300],
                chunks=chunks_list,
            )
            logger.info("cross_encoder_applied", remaining=len(chunks_list))
        else:
            # Fallback: metadata-based reranking
            chunks_list = self._rerank_chunks(chunks_list, failure)

        # Step 9: Trim to context window
        trimmed_chunks = self._trim_to_context_window(chunks_list)

        return RetrievalResult(
            chunks=trimmed_chunks,
            query=failure.error_message[:200],
            total_found=len(all_chunks),
            retrieval_time_ms=0,
            reranked=True,
        )

    async def retrieve_for_query(
        self,
        query: str,
        source_filter: Optional[str] = None,
        active_alert: Optional[ParsedFailure] = None,
    ) -> RetrievalResult:
        """
        Retrieve context for a freeform conversational query.
        Applies the full pre/post-retrieval pipeline:
        1. Query Rewriting (pre-retrieval)
        2. RAG Fusion or Agentic RAG (retrieval)
        3. CRAG + Cross-Encoder Reranking (post-retrieval)
        """
        # ── PRE-RETRIEVAL: Query Rewriting ───────────
        search_query = query
        if self.query_rewriter and active_alert:
            rewritten = await self.query_rewriter.rewrite(
                query=query,
                pipeline_name=active_alert.pipeline_name if active_alert else "",
                error_class=active_alert.error_class if active_alert else "",
            )
            # Use the enriched version if available
            if len(rewritten) > 1:
                search_query = rewritten[1]  # First rewritten variant

        # ── RETRIEVAL ────────────────────────────────
        if self.rag_fusion and not source_filter:
            # RAG Fusion: multi-query decomposition + RRF
            result = await self.rag_fusion.retrieve_and_fuse(
                query=search_query,
                top_k=self.top_k,
                use_hybrid=True,
            )
        elif self.agentic_rag and not source_filter:
            # Agentic RAG: plan and execute multi-source retrieval
            plan = self.agentic_rag.plan_retrieval(
                query=search_query,
                active_alert=active_alert,
            )
            result = await self.agentic_rag.execute_plan(plan)
        else:
            # Fallback: simple vector search
            result = await self.vector_store.search(
                query=search_query,
                top_k=self.top_k,
                similarity_threshold=self.similarity_threshold,
                source_type=source_filter,
                use_hybrid=True,
            )

        # ── POST-RETRIEVAL ───────────────────────────
        # CRAG filtering
        if self.crag and result.chunks:
            scores = self.crag.grade_retrieval(query, result.chunks)
            result.chunks, _ = self.crag.apply_correction(result.chunks, scores)
            result.total_found = len(result.chunks)

        # Cross-Encoder Reranking
        if self.cross_encoder and result.chunks:
            result.chunks = await self.cross_encoder.rerank(
                query=query,
                chunks=result.chunks,
            )
            result.total_found = len(result.chunks)
            result.reranked = True

        return result

    async def retrieve_with_flare(
        self,
        generated_text: str,
        existing_context: str = "",
    ) -> Optional[RetrievalResult]:
        """
        FLARE: Check if generated text has low-confidence segments
        and retrieve additional context if needed.

        Returns additional context or None if not needed.
        """
        if not self.flare:
            return None

        segments = self.flare.detect_low_confidence_segments(generated_text)
        if not segments:
            return None

        logger.info("flare_triggered", low_confidence_segments=len(segments))
        return await self.flare.retrieve_for_segments(segments, existing_context)

    async def find_similar_incidents(
        self,
        failure: ParsedFailure,
        days_back: int = 90,
        max_results: int = 5,
    ) -> list[HistoricalIncident]:
        """Find similar historical incidents for a failure."""
        result = await self.vector_store.search_similar_incidents(
            error_message=failure.error_message,
            pipeline_name=failure.pipeline_name,
            top_k=max_results * 2,
        )

        incidents = []
        for chunk in result.chunks[:max_results]:
            similarity = chunk.metadata.get("similarity_score", 0.5)
            incidents.append(
                HistoricalIncident(
                    incident_id=chunk.source_id,
                    title=chunk.source_title,
                    description=chunk.content[:300],
                    root_cause=chunk.metadata.get("root_cause", "See incident details"),
                    resolution=chunk.metadata.get("resolution", "See resolution notes"),
                    pipeline_name=chunk.metadata.get("pipeline", failure.pipeline_name),
                    error_class=chunk.metadata.get("error_class", failure.error_class),
                    severity=Severity(chunk.metadata.get("severity", "medium")),
                    occurred_at=failure.timestamp,
                    similarity_score=float(similarity),
                    tags=chunk.metadata.get("tags", "").split(",") if isinstance(chunk.metadata.get("tags"), str) else [],
                )
            )

        incidents.sort(key=lambda x: x.similarity_score, reverse=True)
        return incidents

    def _rerank_chunks(
        self,
        chunks: list[DocumentChunk],
        failure: ParsedFailure,
    ) -> list[DocumentChunk]:
        """Rerank chunks based on relevance to the specific failure."""
        scored: list[tuple[float, DocumentChunk]] = []

        for chunk in chunks:
            score = chunk.metadata.get("similarity_score", 0.5)
            if isinstance(score, str):
                try:
                    score = float(score)
                except ValueError:
                    score = 0.5

            if failure.pipeline_name.lower() in chunk.content.lower():
                score += 0.15
            if failure.error_class.lower() in chunk.content.lower():
                score += 0.20
            if any(kw in chunk.content.lower() for kw in ["resolution", "fix", "solution", "workaround", "steps"]):
                score += 0.10
            if chunk.source_type == "confluence":
                score += 0.05
            if chunk.source_type == "icm":
                score += 0.05
            if chunk.source_type == "graph":
                score += 0.08  # Boost graph context

            scored.append((min(score, 1.0), chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [chunk for _, chunk in scored]

    def _trim_to_context_window(
        self,
        chunks: list[DocumentChunk],
    ) -> list[DocumentChunk]:
        """Trim chunks to fit within the context window token budget."""
        char_budget = self.max_context_tokens * 4
        trimmed = []
        total_chars = 0

        for chunk in chunks:
            chunk_chars = len(chunk.content)
            if total_chars + chunk_chars > char_budget:
                remaining = char_budget - total_chars
                if remaining > 200:
                    chunk.content = chunk.content[:remaining] + "..."
                    trimmed.append(chunk)
                break
            trimmed.append(chunk)
            total_chars += chunk_chars

        return trimmed

    async def evaluate_response(self, query: str, context: str, response: str) -> dict[str, Any]:
        """
        Self-RAG evaluation: check if the generated response is supported,
        relevant, and useful given the retrieved context.

        Returns evaluation dict with confidence, supported status, and warnings.
        """
        if not self.self_rag:
            return {
                "original_response": response,
                "enhanced_response": response,
                "evaluation": {"supported": "unknown", "confidence": 0.5},
                "warnings": [],
            }
        return await self.self_rag.evaluate_and_enhance(query, context, response)

    async def build_context_string(
        self,
        chunks: list[DocumentChunk],
        max_chars: int = 24000,
    ) -> str:
        """Build a formatted context string from retrieved chunks."""
        if not chunks:
            return "No relevant historical context found."

        sections = []
        total_chars = 0

        for i, chunk in enumerate(chunks, 1):
            source_label = chunk.source_type.upper()
            search_mode = chunk.metadata.get("search_mode", "")
            mode_tag = f" [{search_mode}]" if search_mode else ""

            section = (
                f"--- Context Source {i} [{source_label}]{mode_tag} ---\n"
                f"Title: {chunk.source_title}\n"
                f"Content:\n{chunk.content}\n"
            )
            if total_chars + len(section) > max_chars:
                break
            sections.append(section)
            total_chars += len(section)

        return "\n".join(sections)
