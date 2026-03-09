# Alert Whisperer — Vector Search Schema & Index Documentation

## Overview

This document describes the vector search index schemas used in Alert Whisperer, including the Azure AI Search index definition, embedding model configuration, and examples of each index type used in the system.

---

## 1. Azure AI Search Index Schema

The primary index is `alert-whisperer-kb`, hosted on Azure AI Search.

### Index Definition

```json
{
    "name": "alert-whisperer-kb",
    "fields": [
        {
            "name": "chunk_id",
            "type": "Edm.String",
            "key": true,
            "filterable": true
        },
        {
            "name": "content",
            "type": "Edm.String",
            "searchable": true,
            "analyzer": "en.microsoft"
        },
        {
            "name": "source_type",
            "type": "Edm.String",
            "filterable": true,
            "facetable": true
        },
        {
            "name": "source_id",
            "type": "Edm.String",
            "filterable": true
        },
        {
            "name": "source_title",
            "type": "Edm.String",
            "searchable": true
        },
        {
            "name": "parent_id",
            "type": "Edm.String",
            "filterable": true
        },
        {
            "name": "is_child_chunk",
            "type": "Edm.String",
            "filterable": true
        },
        {
            "name": "pipeline_name",
            "type": "Edm.String",
            "filterable": true,
            "facetable": true
        },
        {
            "name": "error_class",
            "type": "Edm.String",
            "filterable": true,
            "facetable": true
        },
        {
            "name": "severity",
            "type": "Edm.String",
            "filterable": true,
            "facetable": true
        },
        {
            "name": "token_count",
            "type": "Edm.String"
        },
        {
            "name": "content_vector",
            "type": "Collection(Edm.Single)",
            "searchable": true,
            "dimensions": 3072,
            "vectorSearchProfile": "hnsw-profile"
        }
    ]
}
```

### Field Descriptions

| Field | Type | Purpose | Searchable | Filterable |
|---|---|---|---|---|
| `chunk_id` | String (key) | Unique identifier: `{doc_id}_parent_{n}_child_{m}` | No | Yes |
| `content` | String | Full text of the chunk (400-512 tokens for children) | Yes (BM25) | No |
| `source_type` | String | Origin: `confluence`, `icm`, `log_analytics`, `graph` | No | Yes |
| `source_id` | String | Original document/incident ID from the source system | No | Yes |
| `source_title` | String | Human-readable title of the source document | Yes | No |
| `parent_id` | String | ID of the parent chunk (for child chunks) | No | Yes |
| `is_child_chunk` | String | `"true"` for child chunks, `"false"` for standalone | No | Yes |
| `pipeline_name` | String | Associated pipeline name (if applicable) | No | Yes |
| `error_class` | String | Error classification (e.g., `OutOfMemoryError`) | No | Yes |
| `severity` | String | Alert severity: `critical`, `high`, `medium`, `low` | No | Yes |
| `token_count` | String | Number of tokens in the chunk (for diagnostics) | No | No |
| `content_vector` | Float[3072] | text-embedding-3-large vector embedding | Yes (vector) | No |

---

## 2. Vector Search Configuration

### HNSW Algorithm (Hierarchical Navigable Small World)

HNSW is the vector search algorithm used by Azure AI Search for approximate nearest neighbor (ANN) search.

```json
{
    "algorithmConfigurations": [
        {
            "name": "hnsw-config",
            "kind": "hnsw",
            "parameters": {
                "m": 4,
                "efConstruction": 400,
                "efSearch": 500,
                "metric": "cosine"
            }
        }
    ],
    "profiles": [
        {
            "name": "hnsw-profile",
            "algorithmConfigurationName": "hnsw-config"
        }
    ]
}
```

#### HNSW Parameters Explained

| Parameter | Value | Description |
|---|---|---|
| `m` | 4 | Number of bi-directional links per node. Lower = smaller index, higher = better recall. 4 is optimal for our 3072-dim vectors. |
| `efConstruction` | 400 | Size of the dynamic candidate list during index building. Higher = better index quality but slower indexing. 400 gives excellent recall for our corpus size. |
| `efSearch` | 500 | Size of the dynamic candidate list during search. Higher = better recall but slower queries. 500 provides >99% recall. |
| `metric` | cosine | Distance metric. Cosine is standard for normalised text embeddings. |

#### How HNSW Works

```
Layer 3 (sparse):   [A] ────────────────── [B]
                     │                       │
Layer 2:            [A] ── [C] ── [D] ── [B]
                     │      │      │       │
Layer 1:       [A]─[E]─[C]─[F]─[D]─[G]─[B]
                │   │   │   │   │   │   │
Layer 0:     [A][E][H][C][F][I][D][G][J][B]   ← all documents
```

- Documents are placed in multiple layers (like a skip list)
- Search starts from the topmost layer and greedily navigates toward the query vector
- At each layer, it follows links to the nearest neighbours
- Descends to the next layer for finer resolution
- Returns the closest `k` neighbours from Layer 0

**Time complexity:** O(log N) for search, vs O(N) for brute-force.

#### Example: Querying HNSW

```
Query: "Spark executor OOM during stage 12 of spark_etl_daily_ingest"
    → text-embedding-3-large → [3072-dim vector]
    → HNSW search with efSearch=500
    → Top 10 nearest neighbours returned in ~5ms
    → Combined with BM25 keyword scores (hybrid)
    → Semantic reranking applied (L2 model)
    → Top 5 returned to caller
```

---

## 3. Embedding Model: text-embedding-3-large

### Configuration

| Property | Value |
|---|---|
| Model | `text-embedding-3-large` |
| Dimensions | 3072 |
| Max input tokens | 8191 |
| Provider | OpenAI / Azure OpenAI |
| Encoding | `cl100k_base` (tiktoken) |

### Why 3072 Dimensions (vs 1536)

| Aspect | 1536 dims (text-embedding-3-small) | 3072 dims (text-embedding-3-large) |
|---|---|---|
| Semantic resolution | Good for general text | Excellent for technical content |
| Spark OOM subtypes | May conflate skew/broadcast/spill | Separates distinct OOM causes |
| Kusto error codes | Groups similar codes together | Distinguishes subtle format variations |
| Index size | ~6 KB/doc | ~12 KB/doc |
| Search latency | ~3ms | ~5ms |
| Embedding latency | ~20ms/batch | ~30ms/batch |

The 2x storage cost is justified because our corpus contains highly technical, semantically dense content where subtle differences matter for correct troubleshooting.

### Embedding Example

```python
from openai import AsyncAzureOpenAI

client = AsyncAzureOpenAI(
    api_key="...",
    api_version="2024-02-01",
    azure_endpoint="https://your-endpoint.openai.azure.com/",
)

response = await client.embeddings.create(
    model="text-embedding-3-large",
    input=["Spark executor OutOfMemory: Java heap space during shuffle write"],
    dimensions=3072,
)

embedding = response.data[0].embedding  # list[float] of length 3072
```

---

## 4. Semantic Reranking Configuration

Azure AI Search provides built-in semantic reranking using an L2 cross-encoder model.

```json
{
    "semantic": {
        "configurations": [
            {
                "name": "semantic-config",
                "prioritizedFields": {
                    "titleField": {
                        "fieldName": "source_title"
                    },
                    "contentFields": [
                        {
                            "fieldName": "content"
                        }
                    ]
                }
            }
        ]
    }
}
```

### How Semantic Reranking Works

1. Azure AI Search first retrieves candidates using BM25 + vector (hybrid)
2. The top 50 candidates are passed to the L2 semantic reranking model
3. The model reads the full query AND each document together (cross-attention)
4. Produces a reranker score (0-4 scale)
5. Results are re-ordered by reranker score

**Score interpretation:**
| Score Range | Meaning |
|---|---|
| 3.0 - 4.0 | Highly relevant — direct answer to the query |
| 2.0 - 3.0 | Relevant — related content with useful information |
| 1.0 - 2.0 | Marginally relevant — tangentially related |
| 0.0 - 1.0 | Not relevant |

---

## 5. Index Types Comparison

### 5.1 Vector Embedding Index (HNSW) — Used in Alert Whisperer

**What it is:** Approximate nearest neighbor index for high-dimensional vector search.

**How it works:** Builds a multi-layer graph where each node is a document vector. Navigates the graph to find nearest neighbours.

**Example document:**
```json
{
    "chunk_id": "confluence_runbook_42_parent_0_child_2",
    "content": "Resolution for Spark OutOfMemory Error: Increase executor memory to 8g using spark.executor.memory=8g. Enable AQE with spark.sql.adaptive.enabled=true. If data skew is the cause, add salt column to the join key.",
    "content_vector": [0.0123, -0.0456, 0.0789, ...],  // 3072 floats
    "source_type": "confluence",
    "source_title": "Spark OOM Runbook - Executor Memory"
}
```

**Query:**
```python
results = search_client.search(
    search_text=None,
    vector_queries=[VectorizedQuery(
        vector=query_embedding,          # 3072-dim float array
        k_nearest_neighbors=10,
        fields="content_vector",
    )],
    select=["chunk_id", "content", "source_type", "source_title"],
)
```

**Best for:** Semantic similarity search — finding documents with similar meaning even if they use different words.

---

### 5.2 BM25 Keyword Index — Used in Alert Whisperer (Hybrid)

**What it is:** Inverted index for lexical (keyword) matching using BM25 scoring.

**How it works:** Tokenises documents and builds an inverted index (term → document list). At query time, scores documents using BM25 formula with IDF weighting.

**Example:** Searching for `"Permanent_MappingNotFound"` — an exact Kusto error code that embedding-based search might miss.

```python
results = search_client.search(
    search_text="Permanent_MappingNotFound kusto ingestion",
    select=["chunk_id", "content", "source_type"],
)
```

**BM25 Scoring Formula:**
```
score(q, d) = Σ IDF(t) · (tf(t,d) · (k1 + 1)) / (tf(t,d) + k1 · (1 - b + b · |d|/avgdl))
```

Where:
- `IDF(t)` = log((N - df(t) + 0.5) / (df(t) + 0.5) + 1)
- `tf(t,d)` = term frequency of t in document d
- `k1 = 1.5` (term frequency saturation)
- `b = 0.75` (document length normalization)
- `|d|` = document length, `avgdl` = average document length

**Best for:** Exact match queries — error codes, config keys, stack trace class names, Kusto table names.

---

### 5.3 Hybrid Index (Vector + BM25) — Used in Alert Whisperer (Primary)

**What it is:** Combines HNSW vector search with BM25 keyword search in a single query.

**How it works:**
1. Vector search returns top-N candidates with cosine similarity scores
2. BM25 search returns top-N candidates with keyword relevance scores
3. Scores are fused: `final = α × vector_score + (1-α) × bm25_score` (α = 0.6)
4. Deduplicated, sorted by final score

**Example (Azure AI Search native hybrid):**
```python
results = search_client.search(
    search_text="Spark executor OOM during shuffle write",     # BM25
    vector_queries=[VectorizedQuery(                           # Vector
        vector=query_embedding,
        k_nearest_neighbors=20,
        fields="content_vector",
    )],
    top=5,
    query_type="semantic",                                     # + semantic reranking
    semantic_configuration_name="semantic-config",
)
```

**Best for:** General troubleshooting queries — combines semantic understanding with exact keyword matching.

---

### 5.4 Topic Tree Index — Used in Alert Whisperer

**What it is:** Hierarchical classification tree that narrows the search scope before vector/keyword retrieval.

**How it works:** Error messages are classified into tree branches using keyword matching. The matched branches constrain the retrieval to relevant document subsets.

**Example structure:**
```
Data Pipeline Errors
├── Spark Errors
│   ├── OutOfMemory
│   │   └── doc_ids: ["runbook_spark_oom_1", "runbook_spark_oom_2", ...]
│   ├── Shuffle Failures
│   │   └── doc_ids: ["runbook_shuffle_1", ...]
│   └── ...
├── Synapse Errors
│   └── ...
└── Kusto Errors
    └── ...
```

**Classification example:**
```python
tree = TopicTreeRetriever()
matches = tree.classify_error("java.lang.OutOfMemoryError: Java heap space")
# Returns: [TopicNode("OutOfMemory", confidence=0.85), TopicNode("Spark Errors", confidence=0.6)]
```

**Best for:** Narrowing search scope for error classification — prevents Synapse runbooks from being retrieved for Spark errors.

---

### 5.5 Knowledge Graph Index (GraphRAG) — Used in Alert Whisperer

**What it is:** Graph-based index that models pipeline dependencies and failure relationships.

**How it works:** Nodes represent pipelines, components, and error types. Edges represent relationships (FEEDS_INTO, TRIGGERS, RUNS_ON, COMMONLY_FAILS_WITH). Graph traversal (BFS) traces blast radius and failure chains.

**Example graph:**
```
┌─────────────────────┐     FEEDS_INTO     ┌──────────────────────────┐
│ spark_etl_daily     │───────────────────▶│ synapse_customer360      │
│ ingest              │                    │                          │
│ [RUNS_ON: Spark     │                    │ [RUNS_ON: Synapse Pool]  │
│  Cluster]           │                    └──────────┬───────────────┘
│                     │                               │ TRIGGERS
│ [READS_FROM: ADLS   │                    ┌──────────▼───────────────┐
│  Raw Zone]          │                    │ kusto_ingestion_         │
│                     │                    │ telemetry                │
│ COMMONLY_FAILS_WITH:│                    │ [RUNS_ON: ADX Cluster]   │
│ ├── OutOfMemoryError│                    └──────────────────────────┘
│ └── ShuffleFetch    │
│     Failure         │
└─────────────────────┘
```

**Query example:**
```python
graph = GraphRAGEngine()
blast = graph.trace_blast_radius("spark_etl_daily_ingest", depth=3)
# Returns: {
#     "affected_pipelines": ["synapse_pipeline_customer360", "kusto_ingestion_telemetry"],
#     "affected_components": ["ADLS Gen2 Curated Zone"],
#     "blast_radius": "2 pipelines, 1 components"
# }
```

**Best for:** Dependency tracing, blast radius analysis, root cause chains — understanding what else breaks when a pipeline fails.

---

### 5.6 Parent-Child Chunk Index — Used in Alert Whisperer

**What it is:** Two-tier indexing where small child chunks are indexed for precise retrieval, while large parent chunks are stored for context expansion.

**How it works:**
```
Original Document (Confluence Runbook, ~5000 tokens)
│
├── Parent Chunk 0 (~1200 tokens)
│   ├── Child Chunk 0 (~450 tokens) ← Indexed in Azure AI Search
│   ├── Child Chunk 1 (~450 tokens) ← Indexed in Azure AI Search
│   └── Child Chunk 2 (~300 tokens) ← Indexed in Azure AI Search
│
├── Parent Chunk 1 (~1200 tokens)
│   ├── Child Chunk 0 (~450 tokens) ← Indexed in Azure AI Search
│   └── Child Chunk 1 (~450 tokens) ← Indexed in Azure AI Search
│
└── Parent Chunk 2 (~800 tokens)
    └── Child Chunk 0 (~450 tokens) ← Indexed in Azure AI Search
```

**Retrieval flow:**
1. Child chunk `runbook_42_parent_0_child_2` is retrieved (high similarity)
2. `expand_to_parent=True` triggers lookup of `runbook_42_parent_0`
3. Full parent content (~1200 tokens) replaces the child content
4. LLM sees richer context including surrounding sections

**chunk_id format:**
```
{doc_id}_parent_{parent_index}_child_{child_index}
Example: "confluence_spark_oom_runbook_parent_0_child_2"
```

**Best for:** Confluence runbooks and ICM incidents where precise retrieval (child) needs surrounding context (parent) to be actionable.

---

### 5.7 Semantic Chunking Schema

Documents indexed via semantic chunking (`VectorStore.add_documents_with_semantic_chunking()`) use the same Azure AI Search index but with different metadata:

**Chunk-level metadata (differs from parent-child):**
| Field | Type | Value | Purpose |
|---|---|---|---|
| `chunk_id` | String (key) | `{doc_id}_semantic_{index}` | Unique identifier |
| `chunking_strategy` | String | `"semantic"` | Identifies chunking method used |
| `chunk_index` | String | `"0"`, `"1"`, ... | Position within the document |
| `token_count` | String | `"150"`, `"400"`, ... | Actual token count of this chunk |

**Key differences from parent-child chunking:**
| Property | Parent-Child | Semantic |
|---|---|---|
| Boundary detection | Fixed token window (450 tokens) | Embedding similarity drops |
| Parent context | Yes (1200-token parent stored) | No parent (each chunk is self-contained) |
| Chunk size range | 400-512 tokens (fixed) | 100-800 tokens (variable) |
| Code block handling | May split mid-block | Keeps code blocks intact |
| Use case | General-purpose indexing | Long-form Confluence runbooks, ICM incidents with variable sections |

**When to use which:**
- **Parent-Child:** Default for most content. Best when you need context expansion (child→parent) for short retrieved chunks.
- **Semantic:** Best for Confluence runbooks with distinct sections (Root Cause → Diagnosis → Resolution → Prevention), ICM incidents with variable-length fields, and Kusto docs that interleave KQL code with prose.

---

## 6. Index Sizing Estimates

| Metric | Estimate |
|---|---|
| Documents per Confluence space | ~200-500 pages |
| Chunks per page (child) | ~5-15 |
| Total child chunks (3 spaces) | ~3,000-20,000 |
| Vector storage per chunk | 3072 × 4 bytes = 12.3 KB |
| Total vector storage | 37 MB - 246 MB |
| BM25 index overhead | ~2x content size |
| Recommended Azure AI Search tier | Basic (for < 50K docs) or S1 (for > 50K docs) |

---

## 7. In-Memory Fallback Store

When Azure AI Search is not configured (local dev, CI/CD), the system uses an in-memory store:

| Feature | Azure AI Search | In-Memory Fallback |
|---|---|---|
| Vector search | HNSW (server-side) | Brute-force cosine similarity |
| Keyword search | BM25 (server-side) | Custom BM25 implementation |
| Hybrid fusion | Server-side | Client-side (alpha weighting) |
| Semantic reranking | L2 cross-encoder (server-side) | Not available (uses heuristic) |
| Persistence | Cloud-managed | Ephemeral (lost on restart) |
| Scalability | Enterprise-grade | Limited by RAM |
| Filter queries | OData syntax | Dict comparison |

The in-memory fallback is fully functional for development and testing but not suitable for production.
