"""
RAG (Retrieval-Augmented Generation) Engine.

Provides vector storage (Azure AI Search + text-embedding-3-large),
retrieval, topic tree navigation, and context building for the
Alert Whisperer knowledge base.

Implements 13 RAG techniques across 4 phases:
- Pre-Retrieval: Query Rewriting, RAG Fusion, HyDE, Topic Tree
- Retrieval: Azure AI Search Hybrid, Agentic RAG, Parent-Child Chunking, Semantic Chunking
- Post-Retrieval: CRAG, Cross-Encoder Reranking, GraphRAG
- During Generation: FLARE, Self-RAG
"""

from src.rag.advanced_techniques import (
    CrossEncoderReranker,
    QueryRewriter,
    RAGFusion,
    SelfRAG,
)
from src.rag.retriever import RAGRetriever, TopicTreeRetriever
from src.rag.vector_store import SemanticChunker, VectorStore

__all__ = [
    "VectorStore",
    "SemanticChunker",
    "RAGRetriever",
    "TopicTreeRetriever",
    "CrossEncoderReranker",
    "QueryRewriter",
    "RAGFusion",
    "SelfRAG",
]
