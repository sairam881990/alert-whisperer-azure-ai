# Alert Whisperer — Architecture Document

## System Design

### Design Principles
1. **MCP-First**: All external data access goes through MCP servers — no direct SDK calls to Azure services
2. **RAG-Augmented**: Every LLM call is enriched with retrieved context from the knowledge base
3. **Pydantic-Validated**: All data flows through typed Pydantic models — no untyped dicts at boundaries
4. **Technique-Adaptive**: The prompt engine automatically selects the best prompting technique based on context
5. **Demo-Ready**: The system runs fully in demo mode without any external connections

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

### Layer 2: RAG Engine (`src/rag/`)

**Vector Store (`VectorStore`):**
- ChromaDB with cosine similarity and persistent storage
- Deduplication by `chunk_id` for incremental indexing
- Metadata filtering by `source_type`, pipeline, severity
- Batch ingestion from all data sources

**Retriever (`RAGRetriever`):**
- Multi-query search: error message + pipeline context + topic classification
- Topic tree navigation for hierarchical retrieval
- Reranking with multi-factor scoring (pipeline match, error class match, content type)
- Context window management (token budgeting)

**Topic Tree (`TopicTreeRetriever`):**
- 4-level hierarchy: Root → Platform → Error Category → Specific Pattern
- Keyword-based fast classification
- Boosts leaf nodes (more specific) over branch nodes

### Layer 3: Prompt Engine (`src/prompts/`)

**Template Registry:**
- 11 prompt templates covering all use cases
- Each template is a Pydantic `PromptTemplate` with variables, technique metadata, and rendering

**Technique Selection (auto mode):**
```
Known simple error + no context → zero_shot
Any error + no context → few_shot (use curated examples)
Any error + rich RAG context → chain_of_thought
Ambiguous/intermittent error → tree_of_thought
```

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

**Chat Engine (`ChatEngine`):**
```
User Message
    → Intent classification (regex patterns)
    → Route to specialized handler:
        - SIMILAR_INCIDENTS → RAG search + format
        - RUNBOOK_LOOKUP → Confluence fetch + format
        - ROOT_CAUSE_DETAIL → LLM with CoT
        - VIEW_LOGS → Display log snippet
        - RERUN_PIPELINE → Show rerun URL
        - ESCALATION → Show options
        - TREND_ANALYSIS → RAG + LLM
        - GENERAL_QA → RAG + conversational prompt
    → Active prompting: detect uncertainty, suggest clarification
    → Return response
```

---

## Data Flow

### Alert Processing Pipeline
```
1. Detection: Poll Log Analytics + Kusto + ICM for new errors
2. Parsing: RawLogEntry → ParsedFailure (Pydantic validation)
3. Classification: Infer pipeline type, initial severity
4. RAG Retrieval: Topic tree → vector search → rerank → context string
5. Root Cause Analysis: Auto-select technique → build prompt → invoke LLM → parse output
6. Similar Incidents: Vector search with error message + pipeline context
7. Runbook Fetch: Ownership lookup → Confluence page → extract steps
8. Routing: Direct ownership match → LLM fallback → RoutingDecision
9. Result: AnalysisResult with full context for UI and notifications
```

### Chat Message Pipeline
```
1. User types message in Streamlit chat
2. Intent classifier (10 patterns × regex) identifies intent
3. Specialized handler fetches relevant data
4. RAG retriever provides knowledge base context
5. Prompt engine builds messages with system prompt + context + history
6. LLM generates response
7. Active prompting checks for uncertainty
8. Response displayed in chat with Markdown formatting
```

---

## Prompt Engineering Design

### Global Knowledge Prompt
Injected as the system message in every LLM call. Defines:
- Core knowledge domains (Spark, Synapse, Kusto, Azure infra)
- Operational context (100+ pipelines, SLA definitions)
- Behavioral guidelines (plain English first, concrete actions, uncertainty disclosure)

### Style Guide Prompt
Controls output formatting:
- Lead with impact
- Severity-appropriate urgency
- Structured sections: Summary → Root Cause → Impact → Actions → References
- Consistent terminology

### Solution Prompt
Framework for resolution recommendations:
- Immediate Mitigation → Root Cause Analysis → Permanent Fix → Prevention

### Few-Shot Examples
Three curated examples covering:
1. Spark OOM during shuffle (data skew)
2. Synapse missing folder path (timing dependency)
3. Kusto mapping not found (schema migration)

Each includes input, expected output, and explanation of key insights.

---

## Configuration Model

Uses Pydantic Settings with `.env` file loading:

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
└── RoutingSettings (ROUTING_*)
```

All secrets use `SecretStr` to prevent accidental logging.

---

## Scalability Considerations

| Component | Current | Scale Path |
|-----------|---------|-----------|
| Vector Store | ChromaDB (local) | Migrate to Azure AI Search or Weaviate |
| LLM | Single Azure OpenAI deployment | Add load balancing across deployments |
| Polling | In-process timer | Azure Functions / Event Grid triggers |
| Chat Sessions | In-memory dict | Redis session store |
| Logging | structlog to stdout | Azure Monitor / Application Insights |
| Notifications | Webhook-based | Azure Bot Service for full Teams integration |
