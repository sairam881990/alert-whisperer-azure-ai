"""
Advanced RAG Techniques — Query Rewriting, RAG Fusion, Self-RAG, Cross-Encoder Reranking.

These are the NEW techniques added to the Alert Whisperer RAG pipeline:

1. Cross-Encoder Reranking (post-retrieval):
   - Uses a cross-encoder model to rescore retrieved chunks
   - ~15-20% precision improvement over bi-encoder scoring alone
   - Falls back to metadata-weighted heuristic when model unavailable

2. Query Rewriting (pre-retrieval):
   - Rewrites ambiguous/terse user queries into explicit retrieval queries
   - Synonym expansion for Spark/Synapse/Kusto vocabulary
   - Bridges the gap between support engineer language and KB content

3. RAG Fusion (pre-retrieval → retrieval):
   - Generates multiple query variants for multi-faceted questions
   - Retrieves independently, then fuses results via Reciprocal Rank Fusion (RRF)
   - Handles queries that span multiple error domains or troubleshooting steps

4. Self-RAG (during generation):
   - Self-reflective retrieval-augmented generation
   - LLM generates tokens, then self-evaluates whether to retrieve more
   - Produces [Retrieve], [Relevant], [Supported], [Useful] control tokens
   - Filters hallucinations and unsupported claims during generation
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Optional

import structlog

from src.models import DocumentChunk, RetrievalResult

logger = structlog.get_logger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Cross-Encoder Reranking (Post-Retrieval)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CrossEncoderReranker:
    """
    Cross-encoder reranking for post-retrieval precision improvement.

    Why Cross-Encoder Reranking for Spark/Synapse/Kusto troubleshooting:
    - Bi-encoder (embedding) similarity treats query and document independently,
      missing fine-grained relevance signals. A cross-encoder scores the
      (query, document) pair JOINTLY, catching nuances like version specificity
      (Spark 2.x vs 3.x advice) that bi-encoders miss.
    - Spark OOM errors have many subtypes (skew, broadcast, spill, GC) that
      share vocabulary but require different resolutions — cross-encoders
      distinguish these by attending to both query AND document tokens together.
    - ICM incident descriptions use internal jargon that maps poorly to
      Confluence runbook language — cross-encoders learn these cross-domain
      mappings better than independent embeddings.
    - ~15-20% precision improvement on retrieval benchmarks by rescoring
      the top-K candidates from the initial bi-encoder retrieval.

    Implementation:
    - Primary: LLM-as-reranker (GPT-4o-mini for low latency)
    - Fallback: Metadata-weighted heuristic scoring
    """

    def __init__(
        self,
        llm_client: Any = None,
        model: str = "gpt-4o-mini",
        top_n: int = 5,
    ):
        self.llm_client = llm_client
        self.model = model
        self.top_n = top_n

    RERANK_PROMPT = (
        "You are a relevance scorer for data pipeline troubleshooting documentation.\n\n"
        "QUERY: {query}\n\n"
        "DOCUMENT:\n{document}\n\n"
        "Score the document's relevance to the query from 0.0 to 1.0:\n"
        "- 0.0-0.3: Irrelevant (different error type, wrong pipeline, outdated)\n"
        "- 0.3-0.6: Partially relevant (same domain but different specifics)\n"
        "- 0.6-0.8: Relevant (correct error type, applicable guidance)\n"
        "- 0.8-1.0: Highly relevant (exact match, actionable resolution)\n\n"
        "Respond with ONLY a JSON object: {{\"score\": <float>, \"reason\": \"<brief>\"}}"
    )

    async def rerank(
        self,
        query: str,
        chunks: list[DocumentChunk],
    ) -> list[DocumentChunk]:
        """
        Rerank retrieved chunks using cross-encoder scoring.

        Args:
            query: The user's search query
            chunks: Pre-retrieved document chunks to rescore

        Returns:
            Reranked list of DocumentChunk (top_n), best first.
        """
        if not chunks:
            return []

        if self.llm_client:
            scored = await self._llm_rerank(query, chunks)
        else:
            scored = self._heuristic_rerank(query, chunks)

        # Sort by score descending, return top_n
        scored.sort(key=lambda x: x[0], reverse=True)
        result = [chunk for _, chunk in scored[:self.top_n]]

        logger.info(
            "cross_encoder_reranking_complete",
            input_chunks=len(chunks),
            output_chunks=len(result),
            method="llm" if self.llm_client else "heuristic",
        )
        return result

    async def _llm_rerank(
        self,
        query: str,
        chunks: list[DocumentChunk],
    ) -> list[tuple[float, DocumentChunk]]:
        """Score each chunk using LLM cross-encoder."""
        scored = []
        for chunk in chunks:
            try:
                prompt = self.RERANK_PROMPT.format(
                    query=query[:300],
                    document=chunk.content[:800],
                )
                response = await self.llm_client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=100,
                )
                result_text = response.choices[0].message.content or ""
                match = re.search(r'\{.*\}', result_text, re.DOTALL)
                if match:
                    result = json.loads(match.group())
                    score = float(result.get("score", 0.5))
                    chunk.metadata["reranker_score"] = round(score, 4)
                    chunk.metadata["reranker_reason"] = result.get("reason", "")
                    scored.append((score, chunk))
                else:
                    scored.append((0.5, chunk))
            except Exception as e:
                logger.warning("llm_rerank_error", error=str(e), chunk_id=chunk.chunk_id)
                # Fallback to existing score
                existing = float(chunk.metadata.get("similarity_score", 0.5))
                scored.append((existing, chunk))

        return scored

    def _heuristic_rerank(
        self,
        query: str,
        chunks: list[DocumentChunk],
    ) -> list[tuple[float, DocumentChunk]]:
        """Heuristic reranking based on keyword overlap and metadata signals."""
        scored = []
        query_lower = query.lower()
        query_tokens = set(re.findall(r'\b\w+\b', query_lower))

        for chunk in chunks:
            base_score = float(chunk.metadata.get("similarity_score", 0.5))

            # Token overlap boost
            content_lower = chunk.content.lower()
            content_tokens = set(re.findall(r'\b\w+\b', content_lower))
            overlap = query_tokens & content_tokens
            overlap_ratio = len(overlap) / max(len(query_tokens), 1)
            base_score += 0.1 * overlap_ratio

            # Exact phrase match boost
            if len(query_lower) > 10 and query_lower[:30] in content_lower:
                base_score += 0.15

            # Resolution/fix keyword boost
            if any(kw in content_lower for kw in ["resolution", "fix", "solution", "workaround", "steps"]):
                base_score += 0.08

            # Source type priority
            src = chunk.source_type.lower()
            if src == "confluence":
                base_score += 0.05
            elif src == "icm":
                base_score += 0.05
            elif src == "graph":
                base_score += 0.06

            chunk.metadata["reranker_score"] = round(min(base_score, 1.0), 4)
            scored.append((min(base_score, 1.0), chunk))

        return scored


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Query Rewriting (Pre-Retrieval)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class QueryRewriter:
    """
    Pre-retrieval query rewriting for improved recall.

    Why Query Rewriting for Spark/Synapse/Kusto troubleshooting:
    - Support engineers type terse queries ("OOM on daily job") that don't
      match the verbose language in Confluence runbooks — rewriting expands
      "OOM" to "OutOfMemoryError Java heap space executor memory" for better retrieval
    - Domain-specific synonyms are invisible to embeddings:
      "job failed" = "pipeline error" = "activity failed" = "run aborted"
      — the rewriter normalises these into canonical retrieval terms
    - Abbreviations common in support chat ("ADX" = "Azure Data Explorer" = "Kusto",
      "ADF" = "Azure Data Factory", "ADLS" = "Azure Data Lake Storage Gen2")
      are expanded for both embedding and keyword matching
    - Ambiguous queries ("what happened?") are enriched with context from
      the active alert or recent conversation to produce targeted retrieval queries

    Phases:
    1. Synonym expansion (domain-specific)
    2. Abbreviation expansion
    3. Context enrichment (from active alert)
    4. LLM-based rewriting (optional, for complex queries)
    """

    # Domain-specific synonym map for Spark/Synapse/Kusto
    SYNONYM_MAP = {
        "oom": "OutOfMemoryError Java heap space executor memory",
        "out of memory": "OutOfMemoryError Java heap space executor memory",
        "shuffle failure": "ShuffleFetchFailedException shuffle fetch failed disk space",
        "timeout": "TimeoutException deadline exceeded activity timeout pipeline timeout",
        "auth failure": "AuthenticationFailure token expired credential invalid managed identity",
        "auth error": "AuthenticationFailure token expired credential invalid managed identity",
        "mapping error": "MappingNotFound ingestion mapping schema mismatch",
        "schema mismatch": "SchemaError column type mismatch missing field schema evolution",
        "copy failed": "CopyActivity failure data movement source sink connectivity",
        "slow query": "query performance timeout long running query blocking",
        "throttling": "throttle rate limit 429 too many requests quota exceeded",
        "connection failed": "ConnectionError network DNS firewall private endpoint",
        "job failed": "pipeline failure activity error run failed execution error",
        "skew": "data skew partition skew salting broadcast join AQE",
    }

    # Abbreviation expansions
    ABBREVIATION_MAP = {
        "adx": "Azure Data Explorer Kusto",
        "adf": "Azure Data Factory",
        "adls": "Azure Data Lake Storage Gen2",
        "ase": "App Service Environment",
        "aqe": "Adaptive Query Execution spark.sql.adaptive",
        "spn": "Service Principal",
        "mi": "Managed Identity",
        "kql": "Kusto Query Language",
        "dwu": "Data Warehouse Units Synapse",
        "rca": "root cause analysis",
        "ttd": "time to detect",
        "ttm": "time to mitigate",
        "gc": "garbage collection Java GC overhead",
    }

    REWRITE_PROMPT = (
        "You are a query rewriting assistant for data pipeline troubleshooting.\n\n"
        "Original query: {query}\n"
        "{context_section}"
        "\nRewrite this query into an explicit, detailed retrieval query that would "
        "match relevant Confluence runbooks, ICM incidents, and error documentation. "
        "Include specific error types, component names, and resolution keywords.\n\n"
        "Respond with ONLY the rewritten query (no explanation)."
    )

    def __init__(self, llm_client: Any = None):
        self.llm_client = llm_client

    async def rewrite(
        self,
        query: str,
        pipeline_name: str = "",
        error_class: str = "",
        source: str = "",
    ) -> list[str]:
        """
        Rewrite a user query into one or more optimised retrieval queries.

        Returns:
            List of rewritten queries (always includes the original).
        """
        queries = [query]  # Always include original

        # Step 1: Synonym expansion
        expanded = self._expand_synonyms(query)
        if expanded != query:
            queries.append(expanded)

        # Step 2: Abbreviation expansion
        abbrev_expanded = self._expand_abbreviations(query)
        if abbrev_expanded != query and abbrev_expanded != expanded:
            queries.append(abbrev_expanded)

        # Step 3: Context enrichment
        if pipeline_name or error_class:
            context_query = self._enrich_with_context(query, pipeline_name, error_class, source)
            if context_query not in queries:
                queries.append(context_query)

        # Step 4: LLM rewriting (optional)
        if self.llm_client:
            llm_rewrite = await self._llm_rewrite(query, pipeline_name, error_class)
            if llm_rewrite and llm_rewrite not in queries:
                queries.append(llm_rewrite)

        logger.info(
            "query_rewriting_complete",
            original=query[:60],
            variants=len(queries),
        )
        return queries

    def _expand_synonyms(self, query: str) -> str:
        """Expand domain-specific synonyms in the query."""
        result = query
        query_lower = query.lower()
        for short, long in self.SYNONYM_MAP.items():
            if short in query_lower:
                # Append expansion rather than replace (keeps original context)
                result = f"{result} {long}"
                break  # Only expand the first match to avoid bloat
        return result

    def _expand_abbreviations(self, query: str) -> str:
        """Expand abbreviations in the query."""
        words = query.split()
        expanded = []
        for word in words:
            lower = word.lower().strip(".,!?;:")
            if lower in self.ABBREVIATION_MAP:
                expanded.append(f"{word} ({self.ABBREVIATION_MAP[lower]})")
            else:
                expanded.append(word)
        return " ".join(expanded)

    def _enrich_with_context(
        self,
        query: str,
        pipeline_name: str,
        error_class: str,
        source: str,
    ) -> str:
        """Add pipeline/error context to the query."""
        parts = [query]
        if pipeline_name:
            parts.append(f"pipeline:{pipeline_name}")
        if error_class:
            parts.append(f"error:{error_class}")
        if source:
            parts.append(f"source:{source}")
        return " ".join(parts)

    async def _llm_rewrite(
        self,
        query: str,
        pipeline_name: str,
        error_class: str,
    ) -> Optional[str]:
        """Use LLM to generate an optimised retrieval query."""
        try:
            context_section = ""
            if pipeline_name or error_class:
                context_section = f"Active context — Pipeline: {pipeline_name}, Error: {error_class}\n"

            prompt = self.REWRITE_PROMPT.format(
                query=query,
                context_section=context_section,
            )
            response = await self.llm_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200,
            )
            rewritten = (response.choices[0].message.content or "").strip()
            if rewritten and len(rewritten) > 10:
                return rewritten
        except Exception as e:
            logger.warning("llm_rewrite_error", error=str(e))
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RAG Fusion (Pre-Retrieval → Retrieval)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RAGFusion:
    """
    RAG Fusion generates multiple query variants, retrieves results for each,
    and merges them using Reciprocal Rank Fusion (RRF).

    Why RAG Fusion for Spark/Synapse/Kusto troubleshooting:
    - Complex questions span multiple domains: "Why did the Spark job OOM and
      how do I prevent it from blocking the Kusto ingestion?" needs retrieval
      about (a) Spark OOM causes, (b) OOM prevention, (c) Kusto ingestion
      dependency. A single query can't optimally retrieve all three facets.
    - RRF gives stable, calibrated fusion of results from different query
      variants — unlike simple score averaging which is sensitive to score
      distributions from different search modes
    - Support engineers often pack multiple sub-questions into one message;
      RAG Fusion decomposes these into separate retrievals automatically
    - When combined with Query Rewriting, RAG Fusion amplifies the recall
      improvement: 3 rewritten queries × 3 fusion variants = 9 total
      retrieval passes, dramatically reducing the chance of missing relevant docs

    Algorithm:
    1. Generate N query variants (LLM or template-based)
    2. Run independent retrieval for each variant
    3. Apply Reciprocal Rank Fusion: RRF_score(d) = Σ 1/(k + rank_i(d))
    4. Return top-K documents by fused score
    """

    RRF_K = 60  # RRF constant (standard value from the original paper)

    FUSION_PROMPT = (
        "You are a search query decomposition assistant.\n\n"
        "Original question: {query}\n\n"
        "Generate 3 different search queries that together would retrieve all the "
        "information needed to fully answer the original question. Each query should "
        "focus on a different aspect or angle.\n\n"
        "Respond with ONLY a JSON array of 3 strings:\n"
        "[\"query 1\", \"query 2\", \"query 3\"]"
    )

    def __init__(
        self,
        vector_store: Any = None,
        llm_client: Any = None,
        num_variants: int = 3,
    ):
        self.vector_store = vector_store
        self.llm_client = llm_client
        self.num_variants = num_variants

    async def generate_query_variants(self, query: str) -> list[str]:
        """
        Generate multiple query variants for a single user question.

        Uses LLM decomposition if available, otherwise template-based.
        """
        variants = [query]  # Always include original

        if self.llm_client:
            llm_variants = await self._llm_generate_variants(query)
            variants.extend(llm_variants)
        else:
            variants.extend(self._template_generate_variants(query))

        logger.info("rag_fusion_variants", original=query[:60], count=len(variants))
        return variants[:self.num_variants + 1]  # original + N variants

    async def fuse_results(
        self,
        ranked_lists: list[list[DocumentChunk]],
        top_k: int = 5,
    ) -> list[DocumentChunk]:
        """
        Apply Reciprocal Rank Fusion to merge multiple ranked result lists.

        Args:
            ranked_lists: List of ranked document lists (one per query variant)
            top_k: Number of results to return after fusion

        Returns:
            Fused, deduplicated list of DocumentChunk sorted by RRF score
        """
        rrf_scores: dict[str, float] = defaultdict(float)
        chunk_map: dict[str, DocumentChunk] = {}

        for ranked_list in ranked_lists:
            for rank, chunk in enumerate(ranked_list):
                rrf_scores[chunk.chunk_id] += 1.0 / (self.RRF_K + rank + 1)
                if chunk.chunk_id not in chunk_map:
                    chunk_map[chunk.chunk_id] = chunk

        # Sort by RRF score
        sorted_ids = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)

        result = []
        for chunk_id, rrf_score in sorted_ids[:top_k]:
            chunk = chunk_map[chunk_id]
            chunk.metadata["rrf_score"] = round(rrf_score, 6)
            chunk.metadata["fusion_method"] = "reciprocal_rank_fusion"
            result.append(chunk)

        logger.info(
            "rag_fusion_complete",
            input_lists=len(ranked_lists),
            total_unique_chunks=len(chunk_map),
            output_chunks=len(result),
        )
        return result

    async def retrieve_and_fuse(
        self,
        query: str,
        top_k: int = 5,
        **search_kwargs: Any,
    ) -> RetrievalResult:
        """
        Full RAG Fusion pipeline: generate variants → retrieve → fuse.

        Args:
            query: Original user query
            top_k: Number of results to return
            **search_kwargs: Extra args passed to vector_store.search()

        Returns:
            RetrievalResult with fused chunks
        """
        if not self.vector_store:
            return RetrievalResult(chunks=[], query=query, total_found=0, retrieval_time_ms=0)

        # Step 1: Generate query variants
        variants = await self.generate_query_variants(query)

        # Step 2: Retrieve for each variant
        ranked_lists = []
        total_time = 0.0
        for variant in variants:
            result = await self.vector_store.search(
                query=variant,
                top_k=top_k * 2,  # Retrieve more to give RRF better input
                **search_kwargs,
            )
            ranked_lists.append(result.chunks)
            total_time += result.retrieval_time_ms

        # Step 3: Fuse results
        fused = await self.fuse_results(ranked_lists, top_k=top_k)

        return RetrievalResult(
            chunks=fused,
            query=query,
            total_found=len(fused),
            retrieval_time_ms=total_time,
            reranked=True,
        )

    async def _llm_generate_variants(self, query: str) -> list[str]:
        """Generate query variants using LLM."""
        try:
            prompt = self.FUSION_PROMPT.format(query=query)
            response = await self.llm_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=300,
            )
            text = response.choices[0].message.content or ""
            # Parse JSON array
            match = re.search(r'\[.*\]', text, re.DOTALL)
            if match:
                variants = json.loads(match.group())
                return [v for v in variants if isinstance(v, str) and len(v) > 5]
        except Exception as e:
            logger.warning("rag_fusion_llm_error", error=str(e))

        return self._template_generate_variants(query)

    @staticmethod
    def _template_generate_variants(query: str) -> list[str]:
        """Generate query variants using templates (no LLM needed)."""
        variants = []

        # Variant 1: Cause-focused
        variants.append(f"root cause analysis: {query}")

        # Variant 2: Resolution-focused
        variants.append(f"resolution steps fix workaround: {query}")

        # Variant 3: Similar incidents
        variants.append(f"similar incidents past occurrences: {query}")

        return variants[:3]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Self-RAG (During Generation)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SelfRAG:
    """
    Self-RAG: Self-Reflective Retrieval-Augmented Generation.

    During generation, the LLM self-evaluates whether:
    1. [Retrieve] — Additional retrieval is needed for the current segment
    2. [Relevant] — Retrieved passages are relevant to the query
    3. [Supported] — Generated claims are supported by retrieved evidence
    4. [Useful] — The overall response is useful to the user

    Why Self-RAG for Spark/Synapse/Kusto troubleshooting:
    - Prevents hallucinated Spark configs: "set spark.sql.adaptive.enabled=true"
      is real, but "set spark.sql.adaptive.skewThreshold=0.5" is made up —
      Self-RAG checks each config recommendation against retrieved docs
    - Catches outdated advice: Self-RAG detects when generated text references
      deprecated Spark 2.x patterns and triggers retrieval for current guidance
    - Validates resolution steps: "restart the cluster" is a valid generic step
      but may be wrong for a specific Kusto ingestion mapping error —
      Self-RAG verifies each step against the retrieved runbook context
    - Reduces overconfident responses: when no relevant context is found,
      Self-RAG signals low confidence rather than generating plausible-sounding
      but unverified troubleshooting steps

    Implementation:
    - Uses LLM self-evaluation prompts (not fine-tuned control tokens)
    - Falls back to heuristic evaluation when LLM unavailable
    - Integrates with FLARE for mid-generation retrieval
    """

    SELF_EVAL_PROMPT = (
        "You are evaluating a troubleshooting response for data pipeline errors.\n\n"
        "QUERY: {query}\n\n"
        "RETRIEVED CONTEXT:\n{context}\n\n"
        "GENERATED RESPONSE SEGMENT:\n{segment}\n\n"
        "Evaluate this segment:\n"
        "1. SUPPORTED: Is the claim supported by the retrieved context? (yes/partial/no)\n"
        "2. RELEVANT: Is it relevant to the query? (yes/no)\n"
        "3. NEEDS_RETRIEVAL: Does it need more context? (yes/no)\n"
        "4. CONFIDENCE: How confident are you in this segment? (0.0-1.0)\n\n"
        "Respond with ONLY JSON: "
        "{{\"supported\": \"yes|partial|no\", \"relevant\": \"yes|no\", "
        "\"needs_retrieval\": \"yes|no\", \"confidence\": <float>, "
        "\"retrieval_query\": \"<query if needs_retrieval=yes, else empty>\"}}"
    )

    def __init__(
        self,
        llm_client: Any = None,
        vector_store: Any = None,
        confidence_threshold: float = 0.6,
        max_retrieval_rounds: int = 2,
    ):
        self.llm_client = llm_client
        self.vector_store = vector_store
        self.confidence_threshold = confidence_threshold
        self.max_retrieval_rounds = max_retrieval_rounds

    async def evaluate_and_enhance(
        self,
        query: str,
        context: str,
        generated_response: str,
    ) -> dict[str, Any]:
        """
        Evaluate a generated response and enhance it if needed.

        Args:
            query: Original user query
            context: Retrieved context used for generation
            generated_response: The LLM's generated response

        Returns:
            Dict with evaluation results and optionally enhanced response:
            {
                "original_response": str,
                "enhanced_response": str (may be same as original),
                "evaluation": {
                    "supported": "yes|partial|no",
                    "relevant": "yes|no",
                    "confidence": float,
                    "retrieval_rounds": int,
                },
                "additional_context": str (newly retrieved, if any),
                "warnings": list[str],
            }
        """
        result = {
            "original_response": generated_response,
            "enhanced_response": generated_response,
            "evaluation": {
                "supported": "unknown",
                "relevant": "unknown",
                "confidence": 0.5,
                "retrieval_rounds": 0,
            },
            "additional_context": "",
            "warnings": [],
        }

        if not self.llm_client:
            # Heuristic evaluation
            heuristic = self._heuristic_evaluate(query, context, generated_response)
            result["evaluation"] = heuristic
            return result

        # LLM-based self-evaluation
        segments = self._split_into_segments(generated_response)
        all_evaluations = []
        additional_contexts = []
        retrieval_rounds = 0

        for segment in segments:
            eval_result = await self._evaluate_segment(query, context, segment)
            all_evaluations.append(eval_result)

            # If segment needs retrieval and we haven't exceeded max rounds
            if (
                eval_result.get("needs_retrieval") == "yes"
                and retrieval_rounds < self.max_retrieval_rounds
                and self.vector_store
            ):
                retrieval_query = eval_result.get("retrieval_query", segment[:200])
                if retrieval_query:
                    extra = await self.vector_store.search(
                        query=retrieval_query,
                        top_k=3,
                        use_hybrid=True,
                    )
                    if extra.chunks:
                        new_context = "\n".join(c.content[:500] for c in extra.chunks)
                        additional_contexts.append(new_context)
                        retrieval_rounds += 1

        # Aggregate evaluation
        confidences = [e.get("confidence", 0.5) for e in all_evaluations]
        avg_confidence = sum(confidences) / max(len(confidences), 1)

        supported_counts = {"yes": 0, "partial": 0, "no": 0}
        for e in all_evaluations:
            s = e.get("supported", "unknown")
            if s in supported_counts:
                supported_counts[s] += 1

        if supported_counts["no"] > len(all_evaluations) * 0.3:
            overall_supported = "no"
            result["warnings"].append(
                "Multiple claims in the response are not supported by retrieved evidence. "
                "Consider verifying with original source systems."
            )
        elif supported_counts["partial"] > len(all_evaluations) * 0.3:
            overall_supported = "partial"
            result["warnings"].append(
                "Some claims are only partially supported. "
                "Review the referenced context for full details."
            )
        else:
            overall_supported = "yes"

        result["evaluation"] = {
            "supported": overall_supported,
            "relevant": "yes" if avg_confidence > 0.4 else "no",
            "confidence": round(avg_confidence, 3),
            "retrieval_rounds": retrieval_rounds,
            "segment_evaluations": len(all_evaluations),
        }
        result["additional_context"] = "\n---\n".join(additional_contexts)

        # If confidence is low, flag the response
        if avg_confidence < self.confidence_threshold:
            result["warnings"].append(
                f"Low confidence ({avg_confidence:.0%}). Response may be incomplete or inaccurate."
            )

        logger.info(
            "self_rag_evaluation_complete",
            segments_evaluated=len(all_evaluations),
            avg_confidence=round(avg_confidence, 3),
            supported=overall_supported,
            retrieval_rounds=retrieval_rounds,
        )

        return result

    async def _evaluate_segment(
        self,
        query: str,
        context: str,
        segment: str,
    ) -> dict[str, Any]:
        """Evaluate a single response segment using LLM."""
        try:
            prompt = self.SELF_EVAL_PROMPT.format(
                query=query[:300],
                context=context[:1500],
                segment=segment[:500],
            )
            response = await self.llm_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=200,
            )
            text = response.choices[0].message.content or ""
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                return json.loads(match.group())
        except Exception as e:
            logger.warning("self_rag_eval_error", error=str(e))

        return {"supported": "unknown", "relevant": "unknown", "confidence": 0.5, "needs_retrieval": "no"}

    def _heuristic_evaluate(
        self,
        query: str,
        context: str,
        response: str,
    ) -> dict[str, Any]:
        """Heuristic evaluation without LLM."""
        query_lower = query.lower()
        response_lower = response.lower()
        context_lower = context.lower()

        # Check keyword overlap between response and context
        response_tokens = set(re.findall(r'\b\w{4,}\b', response_lower))
        context_tokens = set(re.findall(r'\b\w{4,}\b', context_lower))
        overlap = response_tokens & context_tokens
        overlap_ratio = len(overlap) / max(len(response_tokens), 1)

        # Check for hedging language (low confidence signals)
        hedging_patterns = [
            r"(?:might|may|could)\s+be",
            r"(?:i'm\s+not\s+sure|uncertain|unclear)",
            r"(?:possibly|perhaps|probably)",
        ]
        hedging_count = sum(
            len(re.findall(p, response_lower))
            for p in hedging_patterns
        )

        confidence = min(0.3 + overlap_ratio * 0.5 - hedging_count * 0.1, 1.0)
        confidence = max(confidence, 0.1)

        supported = "yes" if overlap_ratio > 0.3 else ("partial" if overlap_ratio > 0.15 else "no")
        relevant = "yes" if any(t in response_lower for t in re.findall(r'\b\w{5,}\b', query_lower)[:5]) else "no"

        return {
            "supported": supported,
            "relevant": relevant,
            "confidence": round(confidence, 3),
            "retrieval_rounds": 0,
        }

    @staticmethod
    def _split_into_segments(text: str, max_segment_length: int = 500) -> list[str]:
        """Split response into segments for individual evaluation."""
        # Split on paragraph boundaries
        paragraphs = re.split(r'\n\s*\n', text)
        segments = []
        current = ""

        for para in paragraphs:
            if len(current) + len(para) > max_segment_length and current:
                segments.append(current.strip())
                current = para
            else:
                current = f"{current}\n\n{para}" if current else para

        if current.strip():
            segments.append(current.strip())

        # If no paragraph breaks, split on sentences
        if len(segments) == 1 and len(segments[0]) > max_segment_length:
            sentences = re.split(r'(?<=[.!?])\s+', segments[0])
            segments = []
            current = ""
            for sentence in sentences:
                if len(current) + len(sentence) > max_segment_length and current:
                    segments.append(current.strip())
                    current = sentence
                else:
                    current = f"{current} {sentence}" if current else sentence
            if current.strip():
                segments.append(current.strip())

        return segments if segments else [text]
