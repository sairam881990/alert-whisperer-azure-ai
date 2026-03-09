"""
Vector Store for RAG — Azure AI Search with Hybrid Retrieval.

Manages document embedding (text-embedding-3-large, 3072 dims), storage,
retrieval, and reranking via Azure AI Search's native capabilities.

Enhanced with:
- Azure AI Search native hybrid search (vector + keyword BM25)
- Azure AI Search semantic reranking (L2 cross-encoder)
- Parent-Child Chunking with token-based splitting (400-512 tokens)
- BM25 in-memory fallback for local dev / offline mode
- text-embedding-3-large (3072-dimensional) embeddings via OpenAI SDK
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
import uuid
from collections import Counter, defaultdict
from typing import Any, Optional

import structlog

from src.models import DocumentChunk, ParentChildChunk, RetrievalResult

logger = structlog.get_logger(__name__)

# ── Optional imports with graceful fallback ─────
try:
    import tiktoken
    _TOKENIZER = tiktoken.get_encoding("cl100k_base")
except Exception:
    _TOKENIZER = None
    logger.warning("tiktoken_not_available", msg="Falling back to char-based chunking")

try:
    from azure.search.documents import SearchClient
    from azure.search.documents.indexes import SearchIndexClient
    from azure.search.documents.indexes.models import (
        HnswAlgorithmConfiguration,
        SearchableField,
        SearchField,
        SearchFieldDataType,
        SearchIndex,
        SemanticConfiguration,
        SemanticField,
        SemanticPrioritizedFields,
        SemanticSearch,
        SimpleField,
        VectorSearch,
        VectorSearchProfile,
    )
    from azure.search.documents.models import (
        VectorizableTextQuery,
        VectorizedQuery,
    )
    from azure.core.credentials import AzureKeyCredential
    _AZURE_SEARCH_AVAILABLE = True
except ImportError:
    _AZURE_SEARCH_AVAILABLE = False
    logger.info("azure_search_sdk_not_installed", msg="Using in-memory fallback store")

try:
    from openai import AsyncOpenAI
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False
    logger.info("openai_sdk_not_installed", msg="Embedding generation disabled")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Token-aware text utilities
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _count_tokens(text: str) -> int:
    """Count tokens using tiktoken (cl100k_base). Falls back to word estimate."""
    if _TOKENIZER:
        return len(_TOKENIZER.encode(text))
    return len(text.split())


def _tokens_to_chars(n_tokens: int) -> int:
    """Approximate character count for a given token count (~4 chars/token)."""
    return n_tokens * 4


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BM25 Index for Lexical Search (fallback / augmentation)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BM25Index:
    """
    Lightweight BM25 implementation for lexical keyword matching.

    Why BM25 for Spark/Synapse/Kusto troubleshooting:
    - Exact error codes (e.g., "Permanent_MappingNotFound") need lexical match
    - Stack trace class names (e.g., "UnsafeRow.copy") are missed by embeddings
    - Spark config keys (e.g., "spark.sql.adaptive.skewJoin.enabled") need token-exact matching
    - BM25 excels at matching specific Kusto error codes and ingestion mapping names

    Used as:
    - Primary BM25 engine when Azure AI Search is unavailable (local dev)
    - Augmentation layer on top of Azure AI Search when extra keyword coverage is needed
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._documents: dict[str, str] = {}  # doc_id -> content
        self._doc_lengths: dict[str, int] = {}
        self._avg_dl: float = 0.0
        self._df: Counter = Counter()  # document frequency per term
        self._tf: dict[str, Counter] = {}  # term frequency per doc
        self._n_docs: int = 0

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Simple whitespace + punctuation tokenizer."""
        return re.findall(r'\b\w+\b', text.lower())

    def add_document(self, doc_id: str, content: str) -> None:
        """Index a single document."""
        tokens = self._tokenize(content)
        self._documents[doc_id] = content
        self._doc_lengths[doc_id] = len(tokens)
        self._tf[doc_id] = Counter(tokens)

        # Update document frequency
        unique_terms = set(tokens)
        for term in unique_terms:
            self._df[term] += 1

        self._n_docs = len(self._documents)
        self._avg_dl = sum(self._doc_lengths.values()) / max(self._n_docs, 1)

    def add_documents_batch(self, docs: dict[str, str]) -> None:
        """Batch index multiple documents."""
        for doc_id, content in docs.items():
            self.add_document(doc_id, content)

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """
        Search for documents matching the query.

        Returns:
            List of (doc_id, bm25_score) tuples, sorted by score descending.
        """
        query_tokens = self._tokenize(query)
        scores: dict[str, float] = defaultdict(float)

        for term in query_tokens:
            if term not in self._df:
                continue

            # IDF component
            df = self._df[term]
            idf = math.log((self._n_docs - df + 0.5) / (df + 0.5) + 1.0)

            for doc_id, tf_counter in self._tf.items():
                tf = tf_counter.get(term, 0)
                if tf == 0:
                    continue

                dl = self._doc_lengths[doc_id]
                # BM25 scoring formula
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (1 - self.b + self.b * dl / self._avg_dl)
                scores[doc_id] += idf * (numerator / denominator)

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]

    @property
    def document_count(self) -> int:
        return self._n_docs


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Embedding Client (text-embedding-3-large, 3072 dims)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EmbeddingClient:
    """
    Wraps OpenAI (or Azure OpenAI) SDK to produce 3072-dimensional
    embeddings using text-embedding-3-large.

    Why text-embedding-3-large (3072 dims) over text-embedding-3-small (1536):
    - 3072 dims capture finer semantic distinctions between similar Spark
      errors (OOM from skew vs OOM from broadcast vs OOM from spill)
    - Higher dimensional space separates Kusto ingestion error subtypes
      that share >90% vocabulary but have different root causes
    - Improved recall for long-form Confluence runbooks where key
      resolution steps are buried in verbose procedural text
    - Azure AI Search HNSW index handles 3072 dims efficiently with
      no meaningful latency increase over 1536 dims
    """

    MODEL = "text-embedding-3-large"
    DIMENSIONS = 3072

    def __init__(
        self,
        api_key: str = "",
        api_base: str = "",
        api_version: str = "2024-02-01",
        provider: str = "azure_openai",
    ):
        self.provider = provider
        self._client: Optional[AsyncOpenAI] = None

        if not _OPENAI_AVAILABLE:
            logger.warning("openai_sdk_missing", msg="EmbeddingClient will return zero vectors")
            return

        if provider == "azure_openai" and api_base:
            from openai import AsyncAzureOpenAI
            self._client = AsyncAzureOpenAI(
                api_key=api_key,
                api_version=api_version,
                azure_endpoint=api_base,
            )
        elif api_key:
            self._client = AsyncOpenAI(api_key=api_key)
        else:
            logger.warning("no_api_key", msg="EmbeddingClient initialised without credentials")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for a list of texts.

        Returns:
            List of 3072-dimensional float vectors (one per input text).
            Falls back to zero vectors if the API is unavailable.
        """
        if not self._client:
            return [[0.0] * self.DIMENSIONS for _ in texts]

        try:
            response = await self._client.embeddings.create(
                model=self.MODEL,
                input=texts,
                dimensions=self.DIMENSIONS,
            )
            return [item.embedding for item in response.data]
        except Exception as e:
            logger.error("embedding_api_error", error=str(e), text_count=len(texts))
            return [[0.0] * self.DIMENSIONS for _ in texts]

    async def embed_single(self, text: str) -> list[float]:
        """Embed a single text string."""
        results = await self.embed([text])
        return results[0]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Parent-Child Chunking Manager (Token-Based)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ParentChildChunkManager:
    """
    Manages parent-child chunk relationships for hierarchical retrieval.
    Now uses TOKEN-based splitting (400-512 tokens) instead of character-based.

    Why Token-Based Chunking:
    - LLM context windows are measured in tokens, not characters. A 400-char
      chunk may be 80-120 tokens (waste of context window) while a 400-token
      chunk uses exactly the budget allocated for it.
    - text-embedding-3-large was TRAINED on token-aligned inputs; token-based
      chunks produce higher-quality embeddings with better intra-chunk coherence.
    - Consistent semantic density: each chunk carries ~400-512 tokens of meaning
      regardless of whether the content uses short code tokens or long English words.

    Why Parent-Child Chunking for Spark/Synapse/Kusto troubleshooting:
    - Runbooks have sections (Overview → Diagnosis → Resolution → Prevention)
      — a child chunk about "increase executor memory" is only useful with
      the parent context about WHICH error it applies to
    - ICM incidents have structured fields (Title, Description, Root Cause,
      Resolution) — retrieving just the resolution without the error context
      produces incomplete guidance
    - Log analysis requires seeing both the specific error line (child) AND
      the surrounding context (parent) to understand the failure chain
    - Confluence pages are long-form documents — small child chunks enable
      precise retrieval while parent chunks preserve narrative flow
    """

    def __init__(
        self,
        parent_chunk_tokens: int = 1200,
        child_chunk_tokens: int = 450,
        child_overlap_tokens: int = 50,
    ):
        # Token-based sizes (primary)
        self.parent_chunk_tokens = parent_chunk_tokens
        self.child_chunk_tokens = child_chunk_tokens
        self.child_overlap_tokens = child_overlap_tokens

        # Derived character estimates for fallback
        self.parent_chunk_size = _tokens_to_chars(parent_chunk_tokens)  # ~4800 chars
        self.child_chunk_size = _tokens_to_chars(child_chunk_tokens)     # ~1800 chars
        self.child_overlap = _tokens_to_chars(child_overlap_tokens)      # ~200 chars

        self._parents_by_id: dict[str, ParentChildChunk] = {}   # parent_id -> parent chunk
        self._child_to_parent: dict[str, ParentChildChunk] = {} # child_id -> parent chunk

    def create_parent_child_chunks(
        self,
        doc_id: str,
        content: str,
        source_type: str = "",
        source_id: str = "",
        source_title: str = "",
    ) -> tuple[list[ParentChildChunk], list[DocumentChunk]]:
        """
        Split a document into parent chunks, then further split each
        parent into child chunks. Returns both for indexing.

        The child chunks are indexed in the vector store for retrieval,
        while the parent chunks are stored for context expansion.

        Chunk sizes are measured in TOKENS (400-512 for children,
        ~1200 for parents) for optimal embedding quality.
        """
        parents = []
        all_children = []

        # Step 1: Create parent chunks (token-based)
        parent_texts = self._split_text_by_tokens(
            content, self.parent_chunk_tokens, overlap_tokens=100
        )

        for p_idx, parent_text in enumerate(parent_texts):
            parent_id = f"{doc_id}_parent_{p_idx}"

            # Step 2: Split each parent into child chunks (token-based)
            child_texts = self._split_text_by_tokens(
                parent_text, self.child_chunk_tokens, self.child_overlap_tokens
            )

            children = []
            for c_idx, child_text in enumerate(child_texts):
                child_id = f"{parent_id}_child_{c_idx}"
                child = DocumentChunk(
                    chunk_id=child_id,
                    source_type=source_type,
                    source_id=source_id,
                    source_title=source_title,
                    content=child_text,
                    metadata={
                        "parent_id": parent_id,
                        "child_index": c_idx,
                        "is_child_chunk": "true",
                        "token_count": str(_count_tokens(child_text)),
                    },
                )
                children.append(child)
                all_children.append(child)

            parent = ParentChildChunk(
                parent_id=parent_id,
                parent_content=parent_text,
                children=children,
                source_type=source_type,
                source_id=source_id,
                source_title=source_title,
            )
            parents.append(parent)
            self._parents_by_id[parent_id] = parent

            # Map child IDs to parent
            for child in children:
                self._child_to_parent[child.chunk_id] = parent

        return parents, all_children

    def get_parent_for_child(self, child_id: str) -> Optional[ParentChildChunk]:
        """Retrieve the parent chunk for a given child chunk ID."""
        return self._child_to_parent.get(child_id)

    def expand_to_parent(self, child_chunk: DocumentChunk) -> Optional[str]:
        """
        Given a retrieved child chunk, return the full parent content
        for richer context in the prompt.
        """
        parent_id = child_chunk.metadata.get("parent_id", "")
        if parent_id and parent_id in self._parents_by_id:
            return self._parents_by_id[parent_id].parent_content
        return None

    @staticmethod
    def _split_text_by_tokens(
        text: str,
        max_tokens: int,
        overlap_tokens: int = 0,
    ) -> list[str]:
        """
        Split text into chunks of at most `max_tokens` tokens each,
        with optional token overlap. Tries to break at sentence boundaries.

        Falls back to character-based splitting if tiktoken is unavailable.
        """
        if not _TOKENIZER:
            # Fallback: approximate with chars (~4 chars/token)
            char_size = max_tokens * 4
            char_overlap = overlap_tokens * 4
            return ParentChildChunkManager._split_text_by_chars(text, char_size, char_overlap)

        tokens = _TOKENIZER.encode(text)
        if len(tokens) <= max_tokens:
            return [text]

        chunks = []
        start = 0
        while start < len(tokens):
            end = min(start + max_tokens, len(tokens))
            chunk_tokens = tokens[start:end]
            chunk_text = _TOKENIZER.decode(chunk_tokens)

            # Try to break at sentence boundary within last 20% of chunk
            if end < len(tokens):
                search_start = int(len(chunk_text) * 0.8)
                last_period = chunk_text.rfind('. ', search_start)
                last_newline = chunk_text.rfind('\n', search_start)
                break_point = max(last_period, last_newline)
                if break_point > search_start:
                    chunk_text = chunk_text[:break_point + 1]
                    # Recalculate actual token count after trimming
                    actual_tokens = len(_TOKENIZER.encode(chunk_text))
                    end = start + actual_tokens

            chunks.append(chunk_text.strip())
            start = end - overlap_tokens

        return [c for c in chunks if c]

    @staticmethod
    def _split_text_by_chars(text: str, chunk_size: int, overlap: int = 0) -> list[str]:
        """Legacy character-based splitting (fallback)."""
        if len(text) <= chunk_size:
            return [text]

        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunk = text[start:end]

            # Try to break at sentence boundary
            if end < len(text):
                last_period = chunk.rfind('. ')
                last_newline = chunk.rfind('\n')
                break_point = max(last_period, last_newline)
                if break_point > chunk_size * 0.5:
                    chunk = chunk[:break_point + 1]
                    end = start + break_point + 1

            chunks.append(chunk.strip())
            start = end - overlap

        return [c for c in chunks if c]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Azure AI Search Vector Store
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class VectorStore:
    """
    Azure AI Search-backed vector store for Alert Whisperer knowledge base.

    Replaces the previous ChromaDB implementation with Azure AI Search for:
    - Native hybrid search (BM25 + vector in a single query, server-side)
    - Semantic reranking (L2 cross-encoder model, server-side)
    - 3072-dim HNSW vector index (text-embedding-3-large)
    - Enterprise-grade scalability, SLA, and managed infrastructure
    - Built-in geo-redundancy and zero-downtime index updates

    Falls back to in-memory BM25 + cosine similarity when Azure AI Search
    is not configured (local dev, offline, CI tests).

    Features:
    - Persistent cloud storage with incremental updates
    - Metadata filtering (source_type, pipeline, severity)
    - Similarity threshold filtering
    - Native hybrid search (vector + BM25 keyword) via Azure AI Search
    - Semantic reranking via Azure AI Search semantic configuration
    - Parent-Child chunking for hierarchical context
    - HyDE support for hypothetical document embedding
    """

    INDEX_NAME = "alert-whisperer-kb"
    VECTOR_DIMENSIONS = EmbeddingClient.DIMENSIONS  # 3072

    def __init__(
        self,
        persist_directory: str = "data/vector_store",
        collection_name: str = "alert_whisperer_kb",
        embedding_function: Optional[Any] = None,
        enable_hybrid_search: bool = True,
        hybrid_alpha: float = 0.6,
        # Azure AI Search config
        search_endpoint: str = "",
        search_api_key: str = "",
        # Embedding config
        embedding_api_key: str = "",
        embedding_api_base: str = "",
        embedding_api_version: str = "2024-02-01",
        embedding_provider: str = "azure_openai",
    ):
        self.persist_directory = persist_directory
        self.collection_name = collection_name
        self.hybrid_alpha = hybrid_alpha

        # ── Embedding client ────────────────────────
        self._embedding_client = EmbeddingClient(
            api_key=embedding_api_key,
            api_base=embedding_api_base,
            api_version=embedding_api_version,
            provider=embedding_provider,
        )

        # ── Azure AI Search client ──────────────────
        self._search_client: Optional[SearchClient] = None
        self._index_client: Optional[SearchIndexClient] = None
        self._azure_search_enabled = False

        search_endpoint = search_endpoint or os.getenv("AZURE_SEARCH_ENDPOINT", "")
        search_api_key = search_api_key or os.getenv("AZURE_SEARCH_API_KEY", "")

        if _AZURE_SEARCH_AVAILABLE and search_endpoint and search_api_key:
            try:
                credential = AzureKeyCredential(search_api_key)
                self._index_client = SearchIndexClient(
                    endpoint=search_endpoint, credential=credential
                )
                self._ensure_index_exists()
                self._search_client = SearchClient(
                    endpoint=search_endpoint,
                    index_name=self.INDEX_NAME,
                    credential=credential,
                )
                self._azure_search_enabled = True
                logger.info(
                    "azure_search_initialized",
                    endpoint=search_endpoint,
                    index=self.INDEX_NAME,
                )
            except Exception as e:
                logger.error("azure_search_init_failed", error=str(e))

        # ── Fallback: in-memory BM25 + document store ──
        self._bm25_index: Optional[BM25Index] = BM25Index() if enable_hybrid_search else None
        self._hybrid_enabled = enable_hybrid_search
        self._inmemory_docs: dict[str, dict[str, Any]] = {}  # chunk_id -> {content, metadata, embedding}

        # Parent-child chunk manager (token-based)
        self._parent_child_manager = ParentChildChunkManager()

        logger.info(
            "vector_store_initialized",
            collection=collection_name,
            azure_search=self._azure_search_enabled,
            hybrid_search=enable_hybrid_search,
            embedding_model=EmbeddingClient.MODEL,
            vector_dims=self.VECTOR_DIMENSIONS,
        )

    def _ensure_index_exists(self) -> None:
        """Create the Azure AI Search index if it doesn't already exist."""
        if not self._index_client:
            return

        try:
            self._index_client.get_index(self.INDEX_NAME)
            logger.info("azure_search_index_exists", index=self.INDEX_NAME)
            return
        except Exception:
            pass  # Index doesn't exist yet — create it

        # Define the index schema
        fields = [
            SimpleField(name="chunk_id", type=SearchFieldDataType.String, key=True, filterable=True),
            SearchableField(name="content", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
            SimpleField(name="source_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SimpleField(name="source_id", type=SearchFieldDataType.String, filterable=True),
            SearchableField(name="source_title", type=SearchFieldDataType.String),
            SimpleField(name="parent_id", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="is_child_chunk", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="pipeline_name", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SimpleField(name="error_class", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SimpleField(name="severity", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SimpleField(name="token_count", type=SearchFieldDataType.String, filterable=False),
            SearchField(
                name="content_vector",
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                searchable=True,
                vector_search_dimensions=self.VECTOR_DIMENSIONS,
                vector_search_profile_name="hnsw-profile",
            ),
        ]

        # HNSW vector search configuration
        vector_search = VectorSearch(
            algorithms=[
                HnswAlgorithmConfiguration(
                    name="hnsw-config",
                    parameters={
                        "m": 4,
                        "efConstruction": 400,
                        "efSearch": 500,
                        "metric": "cosine",
                    },
                ),
            ],
            profiles=[
                VectorSearchProfile(name="hnsw-profile", algorithm_configuration_name="hnsw-config"),
            ],
        )

        # Semantic reranking configuration (L2 cross-encoder)
        semantic_config = SemanticConfiguration(
            name="semantic-config",
            prioritized_fields=SemanticPrioritizedFields(
                title_field=SemanticField(field_name="source_title"),
                content_fields=[SemanticField(field_name="content")],
            ),
        )
        semantic_search = SemanticSearch(configurations=[semantic_config])

        index = SearchIndex(
            name=self.INDEX_NAME,
            fields=fields,
            vector_search=vector_search,
            semantic_search=semantic_search,
        )

        self._index_client.create_or_update_index(index)
        logger.info(
            "azure_search_index_created",
            index=self.INDEX_NAME,
            vector_dims=self.VECTOR_DIMENSIONS,
            algorithm="HNSW",
            semantic_reranking=True,
        )

    @property
    def document_count(self) -> int:
        """Current number of documents in the store."""
        if self._azure_search_enabled and self._search_client:
            try:
                results = self._search_client.search(search_text="*", top=0, include_total_count=True)
                return results.get_count() or 0
            except Exception:
                pass
        return len(self._inmemory_docs)

    @property
    def parent_child_manager(self) -> ParentChildChunkManager:
        """Access the parent-child chunk manager."""
        return self._parent_child_manager

    async def add_documents(
        self,
        chunks: list[DocumentChunk],
        batch_size: int = 50,
    ) -> int:
        """
        Add document chunks to the vector store.
        Generates embeddings via text-embedding-3-large (3072 dims)
        and uploads to Azure AI Search (or in-memory fallback).

        Also indexes in BM25 for hybrid search fallback.

        Args:
            chunks: List of DocumentChunk objects
            batch_size: Number of chunks to process per batch

        Returns:
            Number of new documents added
        """
        if not chunks:
            return 0

        added = 0

        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]

            # Generate embeddings for the batch
            texts = [c.content for c in batch]
            embeddings = await self._embedding_client.embed(texts)

            if self._azure_search_enabled and self._search_client:
                # ── Azure AI Search upload ──────────────
                documents = []
                for chunk, embedding in zip(batch, embeddings):
                    doc = {
                        "chunk_id": chunk.chunk_id,
                        "content": chunk.content,
                        "source_type": chunk.source_type,
                        "source_id": chunk.source_id,
                        "source_title": chunk.source_title,
                        "parent_id": chunk.metadata.get("parent_id", ""),
                        "is_child_chunk": chunk.metadata.get("is_child_chunk", "false"),
                        "pipeline_name": chunk.metadata.get("pipeline_name", ""),
                        "error_class": chunk.metadata.get("error_class", ""),
                        "severity": chunk.metadata.get("severity", ""),
                        "token_count": chunk.metadata.get("token_count", ""),
                        "content_vector": embedding,
                    }
                    documents.append(doc)

                try:
                    result = self._search_client.upload_documents(documents=documents)
                    succeeded = sum(1 for r in result if r.succeeded)
                    added += succeeded
                    logger.info("azure_search_batch_uploaded", batch_size=len(batch), succeeded=succeeded)
                except Exception as e:
                    logger.error("azure_search_upload_error", error=str(e), batch_idx=i)
            else:
                # ── In-memory fallback ──────────────────
                for chunk, embedding in zip(batch, embeddings):
                    self._inmemory_docs[chunk.chunk_id] = {
                        "content": chunk.content,
                        "metadata": {
                            "source_type": chunk.source_type,
                            "source_id": chunk.source_id,
                            "source_title": chunk.source_title,
                            **{k: str(v) for k, v in chunk.metadata.items()},
                        },
                        "embedding": embedding,
                    }
                    added += 1

            # Also index in BM25 for hybrid search
            if self._bm25_index:
                for chunk in batch:
                    self._bm25_index.add_document(chunk.chunk_id, chunk.content)

            logger.info("vector_store_batch_added", batch_size=len(batch), total_added=added)

        logger.info("vector_store_indexing_complete", total_added=added, total_docs=self.document_count)
        return added

    async def add_documents_with_parent_child(
        self,
        doc_id: str,
        content: str,
        source_type: str,
        source_id: str,
        source_title: str,
    ) -> int:
        """
        Add a document using parent-child chunking strategy.
        Child chunks are indexed for precise retrieval;
        parent chunks are stored for context expansion.

        Now uses token-based chunking (400-512 tokens per child).
        """
        parents, children = self._parent_child_manager.create_parent_child_chunks(
            doc_id=doc_id,
            content=content,
            source_type=source_type,
            source_id=source_id,
            source_title=source_title,
        )

        # Index child chunks in vector store
        added = await self.add_documents(children)

        logger.info(
            "parent_child_indexing_complete",
            doc_id=doc_id,
            parent_chunks=len(parents),
            child_chunks=len(children),
            indexed=added,
        )
        return added

    async def search(
        self,
        query: str,
        top_k: int = 5,
        similarity_threshold: float = 0.0,
        source_type: Optional[str] = None,
        metadata_filter: Optional[dict[str, str]] = None,
        use_hybrid: Optional[bool] = None,
        expand_to_parent: bool = False,
        use_semantic_reranking: bool = True,
    ) -> RetrievalResult:
        """
        Search for similar documents using Azure AI Search hybrid retrieval
        (vector + BM25 keyword) with optional semantic reranking.

        Falls back to in-memory cosine + BM25 when Azure is unavailable.

        Args:
            query: Search query text
            top_k: Number of results to return
            similarity_threshold: Minimum similarity score (0-1)
            source_type: Filter by source type (confluence, icm, log_analytics)
            metadata_filter: Additional metadata filters
            use_hybrid: Override hybrid search setting (None = use default)
            expand_to_parent: If True, expand child chunks to parent content
            use_semantic_reranking: If True, apply Azure semantic reranking (L2 model)

        Returns:
            RetrievalResult with matching chunks
        """
        start_time = time.time()
        do_hybrid = use_hybrid if use_hybrid is not None else self._hybrid_enabled

        if self._azure_search_enabled and self._search_client:
            chunks = await self._search_azure(
                query=query,
                top_k=top_k,
                similarity_threshold=similarity_threshold,
                source_type=source_type,
                metadata_filter=metadata_filter,
                do_hybrid=do_hybrid,
                use_semantic_reranking=use_semantic_reranking,
            )
        else:
            chunks = await self._search_inmemory(
                query=query,
                top_k=top_k,
                similarity_threshold=similarity_threshold,
                source_type=source_type,
                metadata_filter=metadata_filter,
                do_hybrid=do_hybrid,
            )

        # Optionally expand child chunks to parent content
        if expand_to_parent:
            for chunk in chunks:
                if chunk.metadata.get("is_child_chunk") == "true":
                    parent_content = self._parent_child_manager.expand_to_parent(chunk)
                    if parent_content:
                        chunk.content = parent_content

        elapsed_ms = (time.time() - start_time) * 1000

        logger.info(
            "vector_search_complete",
            query=query[:80],
            results=len(chunks),
            search_mode="azure_hybrid" if self._azure_search_enabled else ("hybrid" if do_hybrid else "vector"),
            semantic_reranking=use_semantic_reranking and self._azure_search_enabled,
            time_ms=round(elapsed_ms, 2),
        )

        return RetrievalResult(
            chunks=chunks,
            query=query,
            total_found=len(chunks),
            retrieval_time_ms=elapsed_ms,
            reranked=use_semantic_reranking and self._azure_search_enabled,
        )

    async def _search_azure(
        self,
        query: str,
        top_k: int,
        similarity_threshold: float,
        source_type: Optional[str],
        metadata_filter: Optional[dict[str, str]],
        do_hybrid: bool,
        use_semantic_reranking: bool,
    ) -> list[DocumentChunk]:
        """Execute search via Azure AI Search with native hybrid + semantic reranking."""
        # Generate query embedding
        query_embedding = await self._embedding_client.embed_single(query)

        # Build filter string (OData syntax)
        filter_parts = []
        if source_type:
            filter_parts.append(f"source_type eq '{source_type}'")
        if metadata_filter:
            for key, value in metadata_filter.items():
                filter_parts.append(f"{key} eq '{value}'")
        filter_str = " and ".join(filter_parts) if filter_parts else None

        # Build vector query
        vector_query = VectorizedQuery(
            vector=query_embedding,
            k_nearest_neighbors=top_k * 2,
            fields="content_vector",
        )

        # Execute search
        search_kwargs: dict[str, Any] = {
            "vector_queries": [vector_query],
            "top": top_k,
            "filter": filter_str,
            "select": [
                "chunk_id", "content", "source_type", "source_id",
                "source_title", "parent_id", "is_child_chunk",
                "pipeline_name", "error_class", "severity", "token_count",
            ],
        }

        if do_hybrid:
            search_kwargs["search_text"] = query

        if use_semantic_reranking:
            search_kwargs["query_type"] = "semantic"
            search_kwargs["semantic_configuration_name"] = "semantic-config"

        try:
            results = self._search_client.search(**search_kwargs)

            chunks = []
            for result in results:
                score = result.get("@search.score", 0.0)
                reranker_score = result.get("@search.reranker_score", None)

                # Normalize score to 0-1 range
                if reranker_score is not None:
                    normalized_score = min(reranker_score / 4.0, 1.0)  # reranker scores are 0-4
                else:
                    normalized_score = min(score / 10.0, 1.0) if score > 1 else score

                if normalized_score < similarity_threshold:
                    continue

                metadata = {
                    "source_type": result.get("source_type", ""),
                    "source_id": result.get("source_id", ""),
                    "source_title": result.get("source_title", ""),
                    "parent_id": result.get("parent_id", ""),
                    "is_child_chunk": result.get("is_child_chunk", "false"),
                    "pipeline_name": result.get("pipeline_name", ""),
                    "error_class": result.get("error_class", ""),
                    "severity": result.get("severity", ""),
                    "similarity_score": round(normalized_score, 4),
                    "search_score": round(score, 4),
                    "reranker_score": round(reranker_score, 4) if reranker_score else None,
                    "search_mode": "azure_hybrid_semantic" if use_semantic_reranking else "azure_hybrid",
                }

                chunks.append(DocumentChunk(
                    chunk_id=result["chunk_id"],
                    source_type=result.get("source_type", "unknown"),
                    source_id=result.get("source_id", ""),
                    source_title=result.get("source_title", ""),
                    content=result.get("content", ""),
                    metadata=metadata,
                ))

            return chunks

        except Exception as e:
            logger.error("azure_search_query_error", error=str(e))
            # Fall back to in-memory
            return await self._search_inmemory(
                query=query, top_k=top_k, similarity_threshold=similarity_threshold,
                source_type=source_type, metadata_filter=metadata_filter, do_hybrid=True,
            )

    async def _search_inmemory(
        self,
        query: str,
        top_k: int,
        similarity_threshold: float,
        source_type: Optional[str],
        metadata_filter: Optional[dict[str, str]],
        do_hybrid: bool,
    ) -> list[DocumentChunk]:
        """In-memory fallback search using cosine similarity + BM25."""
        if not self._inmemory_docs:
            return []

        # Generate query embedding
        query_embedding = await self._embedding_client.embed_single(query)

        # ── Vector similarity ──────────────────────
        vector_scores: dict[str, float] = {}
        for doc_id, doc_data in self._inmemory_docs.items():
            # Apply metadata filters
            meta = doc_data["metadata"]
            if source_type and meta.get("source_type") != source_type:
                continue
            if metadata_filter:
                skip = False
                for k, v in metadata_filter.items():
                    if meta.get(k) != v:
                        skip = True
                        break
                if skip:
                    continue

            doc_embedding = doc_data.get("embedding", [])
            if doc_embedding:
                similarity = self._cosine_similarity(query_embedding, doc_embedding)
                if similarity >= similarity_threshold:
                    vector_scores[doc_id] = similarity

        # ── BM25 scores ────────────────────────────
        bm25_scores: dict[str, float] = {}
        if do_hybrid and self._bm25_index and self._bm25_index.document_count > 0:
            bm25_results = self._bm25_index.search(query, top_k=top_k * 2)
            if bm25_results:
                max_bm25 = bm25_results[0][1] if bm25_results[0][1] > 0 else 1.0
                for doc_id, score in bm25_results:
                    if doc_id in self._inmemory_docs:
                        bm25_scores[doc_id] = score / max_bm25

        # ── Merge ──────────────────────────────────
        all_ids = set(vector_scores.keys()) | set(bm25_scores.keys())
        merged: list[tuple[str, float]] = []

        for doc_id in all_ids:
            v_score = vector_scores.get(doc_id, 0.0)
            b_score = bm25_scores.get(doc_id, 0.0)
            final = self.hybrid_alpha * v_score + (1 - self.hybrid_alpha) * b_score if do_hybrid else v_score
            if final >= similarity_threshold:
                merged.append((doc_id, final))

        merged.sort(key=lambda x: x[1], reverse=True)
        merged = merged[:top_k]

        # ── Build result chunks ────────────────────
        chunks = []
        for doc_id, score in merged:
            doc_data = self._inmemory_docs[doc_id]
            metadata = {
                **doc_data["metadata"],
                "similarity_score": round(score, 4),
                "vector_score": round(vector_scores.get(doc_id, 0.0), 4),
                "bm25_score": round(bm25_scores.get(doc_id, 0.0), 4),
                "search_mode": "hybrid" if do_hybrid else "vector",
            }
            chunks.append(DocumentChunk(
                chunk_id=doc_id,
                source_type=doc_data["metadata"].get("source_type", "unknown"),
                source_id=doc_data["metadata"].get("source_id", ""),
                source_title=doc_data["metadata"].get("source_title", ""),
                content=doc_data["content"],
                metadata=metadata,
            ))

        return chunks

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    async def search_similar_incidents(
        self,
        error_message: str,
        pipeline_name: Optional[str] = None,
        top_k: int = 10,
    ) -> RetrievalResult:
        """
        Specialized search for similar historical incidents.
        Combines error message and pipeline context.
        Uses hybrid search for better matching of exact error codes.
        """
        query = error_message
        if pipeline_name:
            query = f"Pipeline: {pipeline_name}. Error: {error_message}"

        return await self.search(
            query=query,
            top_k=top_k,
            source_type=None,
            similarity_threshold=0.3,
            use_hybrid=True,
            use_semantic_reranking=True,
        )

    async def delete_by_source(self, source_type: str) -> int:
        """Delete all documents from a specific source type."""
        if self._azure_search_enabled and self._search_client:
            try:
                results = self._search_client.search(
                    search_text="*",
                    filter=f"source_type eq '{source_type}'",
                    select=["chunk_id"],
                    top=1000,
                )
                ids_to_delete = [{"chunk_id": r["chunk_id"]} for r in results]
                if ids_to_delete:
                    self._search_client.delete_documents(documents=ids_to_delete)
                    logger.info("azure_search_deleted", source=source_type, count=len(ids_to_delete))
                    return len(ids_to_delete)
            except Exception as e:
                logger.error("azure_search_delete_error", error=str(e))
            return 0
        else:
            # In-memory fallback
            to_delete = [
                k for k, v in self._inmemory_docs.items()
                if v["metadata"].get("source_type") == source_type
            ]
            for k in to_delete:
                del self._inmemory_docs[k]
            return len(to_delete)

    async def get_stats(self) -> dict[str, Any]:
        """Get vector store statistics."""
        total = self.document_count

        source_counts: dict[str, int] = {}
        if self._azure_search_enabled and self._search_client:
            for source in ["confluence", "icm", "log_analytics"]:
                try:
                    results = self._search_client.search(
                        search_text="*",
                        filter=f"source_type eq '{source}'",
                        top=0,
                        include_total_count=True,
                    )
                    source_counts[source] = results.get_count() or 0
                except Exception:
                    source_counts[source] = 0
        else:
            for source in ["confluence", "icm", "log_analytics"]:
                source_counts[source] = sum(
                    1 for v in self._inmemory_docs.values()
                    if v["metadata"].get("source_type") == source
                )

        return {
            "total_documents": total,
            "by_source": source_counts,
            "collection_name": self.collection_name,
            "backend": "azure_ai_search" if self._azure_search_enabled else "in_memory",
            "index_name": self.INDEX_NAME if self._azure_search_enabled else "N/A",
            "embedding_model": EmbeddingClient.MODEL,
            "vector_dimensions": self.VECTOR_DIMENSIONS,
            "hybrid_search_enabled": self._hybrid_enabled,
            "semantic_reranking": self._azure_search_enabled,
            "bm25_indexed": self._bm25_index.document_count if self._bm25_index else 0,
        }
