# Alert Whisperer — Architecture Document

## System Design

### Design Principles
1. **MCP-First**: All external data access goes through MCP servers — no direct SDK calls to Azure services
2. **RAG-Augmented**: Every LLM call is enriched with retrieved context from the knowledge base
3. **Pydantic-Validated**: All data flows through typed Pydantic models — no untyped dicts at boundaries
4. **Technique-Adaptive**: The prompt engine automatically selects the best prompting technique based on context
5. **Demo-Ready**: The system runs fully in demo mode without any external connections
6. **Production-Hardened**: Response caching, circuit breakers, retry logic, and deterministic hashing throughout
7. **Data-Consistent**: Equivalent questions return the same results regardless of phrasing (query rewriting + response cache)

---

## Layer Architecture

### Layer 1: MCP Connector Layer (`src/connectors/`)

Provides a unified abstraction over four MCP servers:

| Connector | MCP Server | Data Accessed |
|-----------|-----------|---------------|
| `KustoMCPClient` | `@azure/mcp-server-kusto` | KQL queries, ingestion failures, cluster health |
| `ConfluenceMCPClient` | `@anthropic/mcp-server-confluence` | Runbook pages, search, structured step extraction |
| `ICMMCPClient` | Custom ICM MCP | Incidents, historical resolution notes, polling |
| `LogAnalyticsMCPClient` | `@azure/mcp-server-log-analytics` | Spark logs, Synapse runs, error trends |

**Base Client (`BaseMCPClient`):**
- JSON-RPC 2.0 transport over HTTP
- `initialize` → `tools/list` → `tools/call` lifecycle
- Automatic retries with tenacity (exponential backoff)
- Structured error hierarchy: `MCPClientError` → `MCPConnectionError` / `MCPToolError`
- Circuit breaker pattern (M7) with configurable thresholds and cooldowns
- Pagination support for large result sets (M1)
- Streaming query results in configurable batches (M2)

### Layer 2: RAG Engine (`src/rag/`)

**Vector Store (`VectorStore`):**
- Azure AI Search with text-embedding-3-large (3072 dimensions)
- Hybrid search: vector (60%) + BM25 keyword (40%), configurable via `hybrid_alpha`
- Semantic reranking via Azure AI Search built-in capability
- Deduplication by `chunk_id` for incremental indexing
- Metadata filtering by `source_type`, pipeline, severity
- **Document staleness tracking** (O4): fresh/aging/stale/expired labels based on last-modified date
- Batch ingestion from all data sources

**Advanced Techniques (`advanced_techniques.py`):**
- **Query Rewriting**: LLM rewrites user query into 3 optimized search variants
- **RAG Fusion**: Reciprocal Rank Fusion (k=60 configurable) merges results from multi-query search
- **Self-RAG**: LLM evaluates each chunk for relevance, filters low-quality results
- **Cross-Encoder Reranking**: LLM-as-reranker or dedicated model (ms-marco-MiniLM-L-12-v2) with latency budget
- **Semantic Chunking**: Embedding-similarity boundary detection with configurable percentile

**Retriever (`RAGRetriever`):**
- Multi-query search: error message + pipeline context + topic classification
- Topic tree navigation for hierarchical retrieval (dynamically updated from indexed docs)
- CRAG (Corrective RAG) with relevance grading
- HyDE (Hypothetical Document Embeddings) for cold-start queries
- FLARE (Forward-Looking Active Retrieval) with **dedup + summarization** (CE21)
- Agentic RAG for multi-index orchestration
- GraphRAG for cascading failure tracing
- Context window management (token budgeting)

**Topic Tree (`TopicTreeRetriever`):**
- 4-level hierarchy: Root → Platform → Error Category → Specific Pattern
- Keyword-based fast classification
- Boosts leaf nodes (more specific) over branch nodes
- Dynamic growth as new documents are indexed (R15)

### Layer 3: Prompt Engine (`src/prompts/`)

**Template Registry:**
- 11 prompt templates covering all use cases
- Each template is a Pydantic `PromptTemplate` with variables, technique metadata, version, and rendering
- Template versioning (P10) for audit trail

**Technique Selection (auto mode):**
```
Known simple error + no context → zero_shot
Any error + no context → few_shot (use curated examples)
Any error + rich RAG context → chain_of_thought
Ambiguous/intermittent error → tree_of_thought
```

**Production Hardening:**
- **LLM retry with backoff** (PE2): Exponential backoff with jitter, configurable max retries
- **Parse fallback** (PE1): Multi-pattern regex parsing with graceful degradation
- **Stack trace truncation** (O1): Retains first/last lines + "Caused by" chains
- **"I don't know" instruction** (PE6): All system prompts instruct honest uncertainty disclosure
- **Configurable DSPy optimizer** (PE3): Minimum 20 samples before optimization (was 5)
- **Configurable cache TTL** (PE5): Prompt cache TTL from settings

**Structured Output:**
- `RootCauseOutput`: Classification, summary, technical details, severity, components
- `RoutingOutput`: Channel, contacts, severity, escalation, confidence
- `SeverityOutput`: Severity, confidence, justification, multi-assessment

### Layer 4: Engine Layer (`src/engine/`)

**Alert Processor (`AlertProcessor`):**
```
Raw Log / ICM Ticket
    → Parse into ParsedFailure
    → RAG retrieval for context
    → LLM root cause analysis (technique: auto)
    → Find similar historical incidents
    → Fetch runbook steps from Confluence
    → Route to correct team
    → Build AnalysisResult
```

**Alert Triage Engine (`AlertTriageEngine`):**
- Alert deduplication with cascade detection
- **Rolling window** (AT1): Configurable max 10K alerts in memory
- **Indexed search** (AT2): O(1) lookups by pipeline, error class, severity
- **Time-windowed 24h counts** (AT3): Accurate rolling counts replace increment-only counters
- **Alert suppression rules** (AT7): Configurable patterns to suppress known-noisy alerts
- **Severity time-decay** (AT9): Auto-downgrades severity when no new high-sev alerts in decay window
- **Cluster acknowledgment** (AT10): Mark clusters as acknowledged to separate from new alerts
- **ICM ticket correlation** (O5): Auto-links ICM tickets to alert clusters
- Configurable cascade window multiplier (AT5)
- Proactive greeting with cluster ranking

**Chat Engine (`ChatEngine`):**
```
User Message
    → Command handling (/new, /reset, /help, /export) [CE17]
    → Session cleanup (TTL + max sessions) [CE7]
    → Multi-intent detection [CE6]
    → Response cache lookup [CE4]
    → Intent classification (unified _route_intent) [CE24]
    → Confidence threshold validation [CE8]
    → Per-intent temperature selection [CE3]
    → Route to specialized handler:
        - SIMILAR_INCIDENTS → RAG search + Self-RAG verification [CE15]
        - RUNBOOK_LOOKUP → Confluence fetch + staleness check [CE16]
        - ROOT_CAUSE_DETAIL → LLM with CoT
        - VIEW_LOGS → Display log snippet
        - RERUN_PIPELINE → Show rerun URL + confirmation [CE19]
        - ESCALATION → Show options
        - TREND_ANALYSIS → RAG + LLM
        - GENERAL_QA → RAG + conversational prompt
    → CoVE verification (configurable intents) [CE1]
    → Regex-based uncertainty detection [CE2]
    → Follow-up suggestions [CE22]
    → User-facing error classification [CE13]
    → Response cache store [CE4]
    → Real token-level streaming [CE23]
    → Return response
```

**Data Refresh Engine (`DataRefreshEngine`):**
- **Parallel MCP polling** (DR5): asyncio.gather across all 4 connectors
- **Delta deduplication** (DR6): SHA-256 fingerprinting prevents re-ingesting same alerts
- **Timestamp cursors** (O2): High-watermark tracking for incremental fetches
- **Deterministic hashing** (DR3): hashlib replaces Python hash() for cross-process consistency
- **Data source tagging** (DR4): Each response tagged as demo/live_mcp/cached
- **Event loop safety** (DR1): Proper get-or-create pattern
- **Division-by-zero guard** (DR2): timespan_hours always ≥ 1
- **Confluence re-index trigger** (DR8): On-demand KB refresh
- Circuit breakers inherited from MCP connectors (M7)

---

## Data Flow

### Alert Processing Pipeline
```
1. Detection: Poll Log Analytics + Kusto + ICM for new errors (parallel)
2. Delta Dedup: Fingerprint check against seen-set
3. Parsing: RawLogEntry → ParsedFailure (Pydantic validation)
4. Source Tagging: Mark as demo/live_mcp
5. Suppression Check: Filter against suppression rules
6. Classification: Infer pipeline type, initial severity
7. RAG Retrieval: Topic tree → vector search → rerank → context string
8. Root Cause Analysis: Auto-select technique → build prompt → invoke LLM (with retry) → parse output (with fallback)
9. Similar Incidents: Vector search + Self-RAG verification
10. Runbook Fetch: Ownership lookup → Confluence page → staleness check → extract steps
11. Routing: Direct ownership match → LLM fallback → RoutingDecision (confidence validated)
12. Result: AnalysisResult with full context for UI and notifications
```

### Chat Message Pipeline
```
1. User types message in Streamlit chat
2. Command handler checks for /new, /reset, /help, /export
3. Session cleanup removes stale sessions (TTL + max limit)
4. Multi-intent detector splits compound queries
5. Response cache checked for cached answer
6. Unified intent router classifies intent with confidence
7. Per-intent temperature selected
8. Specialized handler fetches relevant data
9. RAG retriever provides knowledge base context
10. Prompt engine builds messages with system prompt + context + history
11. LLM generates response (with retry + exponential backoff)
12. CoVE verification for configured intents
13. Uncertainty detection (regex patterns)
14. Follow-up suggestions appended
15. Response cached and displayed with real token-level streaming
```

---

## Prompt Engineering Design

### Global Knowledge Prompt
Injected as the system message in every LLM call. Defines:
- Core knowledge domains (Spark, Synapse, Kusto, Azure infra)
- Operational context (100+ pipelines, SLA definitions)
- Behavioral guidelines (plain English first, concrete actions, uncertainty disclosure)
- **"I don't know" instruction**: Explicit instruction to admit insufficient data rather than guess

### Style Guide Prompt
Controls output formatting:
- Lead with impact
- Severity-appropriate urgency
- Structured sections: Summary → Root Cause → Impact → Actions → References
- Consistent terminology
- **"I don't know" instruction** appended

### Solution Prompt
Framework for resolution recommendations:
- Immediate Mitigation → Root Cause Analysis → Permanent Fix → Prevention
- **"I don't know" instruction** appended

### Few-Shot Examples
Three curated examples covering:
1. Spark OOM during shuffle (data skew)
2. Synapse missing folder path (timing dependency)
3. Kusto mapping not found (schema migration)

Each includes input, expected output, and explanation of key insights.

---

## Configuration Model

Uses Pydantic Settings with `.env` file loading and **hot-reload** (O7):

```python
AppSettings
├── AzureSettings (AZURE_*)
├── KustoSettings (KUSTO_*)
├── ConfluenceSettings (CONFLUENCE_*)
├── ICMSettings (ICM_*)
├── LogAnalyticsSettings (LOG_ANALYTICS_*)
├── LLMSettings (LLM_*)
├── TeamsSettings (TEAMS_*)
├── RAGSettings (RAG_*)
├── AzureSearchSettings (AZURE_SEARCH_*)
├── RoutingSettings (ROUTING_*)
├── ChatEngineSettings (CHAT_*)        # NEW — R2
├── AlertTriageSettings (ALERT_TRIAGE_*)  # NEW — R2
└── DataRefreshSettings (DATA_REFRESH_*)  # NEW — R2
```

All secrets use `SecretStr` to prevent accidental logging.
Settings auto-reload when `.env` file is modified (O7).

---

## Scalability Considerations

| Component | Current | Scale Path |
|-----------|---------|-----------| 
| Vector Store | Azure AI Search (3072-dim) | Already production-grade; add replicas for throughput |
| LLM | Single Azure OpenAI deployment | Add load balancing across deployments |
| Polling | Parallel async in-process (DR5) | Azure Functions / Event Grid triggers |
| Chat Sessions | In-memory with TTL + max limit (CE7) | Redis session store |
| Response Cache | In-memory LRU with TTL (CE4) | Redis cache layer |
| Alert History | Rolling window (AT1, 10K max) | Azure Table Storage / Cosmos DB |
| Logging | structlog to stdout | Azure Monitor / Application Insights |
| Notifications | Webhook-based | Azure Bot Service for full Teams integration |
| Config | Hot-reload from .env (O7) | Azure App Configuration |
