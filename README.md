# Alert Whisperer

**Real-Time Failure Detection, Routing & Conversational Troubleshooting**

Alert Whisperer is a GenAI-powered assistant that detects Spark, Synapse, and Kusto pipeline failures in real time, explains the root cause in plain English, and routes alerts to the right team. It supports interactive Q&A for the support team, retrieves similar historical incidents, and provides runbook steps for guided resolution.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        STREAMLIT UI                                  │
│  ┌──────────┐ ┌──────────────┐ ┌──────────┐ ┌─────────────────┐    │
│  │ Dashboard │ │  Alert Feed  │ │   Chat   │ │ Knowledge Base  │    │
│  └────┬─────┘ └──────┬───────┘ └────┬─────┘ └───────┬─────────┘    │
│       │               │              │               │              │
├───────┼───────────────┼──────────────┼───────────────┼──────────────┤
│       ▼               ▼              ▼               ▼              │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │                    ENGINE LAYER                              │    │
│  │  ┌──────────────────┐  ┌───────────────────────────────┐   │    │
│  │  │ Alert Processor   │  │ Chat Engine                    │   │    │
│  │  │  - Detection      │  │  - Intent Classification       │   │    │
│  │  │  - Classification │  │  - Context-Aware Q&A           │   │    │
│  │  │  - RCA            │  │  - Session Management          │   │    │
│  │  │  - Routing        │  │  - Active Prompting            │   │    │
│  │  └────────┬─────────┘  └──────────────┬────────────────┘   │    │
│  └───────────┼───────────────────────────┼────────────────────┘    │
│              │                           │                          │
├──────────────┼───────────────────────────┼──────────────────────────┤
│              ▼                           ▼                          │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐    │
│  │   PROMPT ENGINE       │  │   RAG ENGINE (12 Techniques)     │    │
│  │  - Template Registry  │  │  ┌──────────────────────────┐   │    │
│  │  - Few-Shot Examples  │  │  │ Azure AI Search           │   │    │
│  │  - CoT / ToT / ReAct  │  │  │ + text-embedding-3-large │   │    │
│  │  - Severity Classifier│  │  │ + HNSW 3072d + BM25      │   │    │
│  │  - Routing Prompt     │  │  │ + Semantic Reranking      │   │    │
│  │  - Reflexion          │  │  └──────────────────────────┘   │    │
│  │  - Chain-of-Verify    │  │  ┌──────────────────────────┐   │    │
│  └──────────┬───────────┘  │  │ Pre: Query Rewriting,     │   │    │
│             │              │  │  RAG Fusion, HyDE,         │   │    │
│             │              │  │  Topic Tree, Semantic Chunk│   │    │
│             │              │  ├────────────────────────────┤   │    │
│             │              │  │ Retrieval: Hybrid Search,  │   │    │
│             │              │  │  Agentic RAG, Parent-Child │   │    │
│             │              │  ├────────────────────────────┤   │    │
│             │              │  │ Post: CRAG, Cross-Encoder, │   │    │
│             │              │  │  GraphRAG                  │   │    │
│             │              │  ├────────────────────────────┤   │    │
│             │              │  │ Gen: FLARE, Self-RAG       │   │    │
│             │              │  └──────────────────────────┘   │    │
│             │              └──────────────┬───────────────────┘    │
│             │                              │                        │
├─────────────┼──────────────────────────────┼────────────────────────┤
│             ▼                              ▼                        │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                    MCP CONNECTOR LAYER                         │  │
│  │  ┌──────────┐ ┌────────────┐ ┌─────────┐ ┌──────────────┐   │  │
│  │  │  Kusto   │ │ Confluence │ │   ICM   │ │Log Analytics │   │  │
│  │  │  MCP     │ │   MCP      │ │   MCP   │ │    MCP       │   │  │
│  │  └────┬─────┘ └─────┬──────┘ └────┬────┘ └──────┬───────┘   │  │
│  └───────┼──────────────┼─────────────┼─────────────┼───────────┘  │
│          ▼              ▼             ▼             ▼               │
│    Kusto/ADX     Confluence API   ICM System   Log Analytics       │
│    Cluster       (Runbooks)       (Incidents)  Workspace           │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Features

### Real-Time Detection
- Polls Spark driver/executor logs, Synapse pipeline runs, and Kusto ingestion errors
- Detects new/updated ICM tickets
- Classifies errors automatically (OOM, Timeout, Connection, Auth, Schema, etc.)

### Root Cause Analysis
- LLM-powered analysis with automatic technique selection
- Chain-of-Thought reasoning for complex failures
- Tree-of-Thought exploration for ambiguous errors
- Few-shot prompting with curated enterprise examples

### RAG Knowledge Base (12 Techniques)

| Phase | Technique | Description |
|---|---|---|
| **Pre-Retrieval** | Query Rewriting | Synonym/abbreviation expansion + LLM-based rewriting |
| | RAG Fusion | Multi-query decomposition + Reciprocal Rank Fusion |
| | HyDE | Hypothetical document embedding for better recall |
| | Topic Tree | Hierarchical error classification |
| | Semantic Chunking | Embedding-similarity boundary detection |
| **Retrieval** | Azure AI Search Hybrid | Vector (HNSW 3072d) + BM25 keyword, server-side |
| | Agentic RAG | Multi-source orchestration (Confluence/ICM/Logs) |
| | Parent-Child Chunking | Token-based (450 tokens) with parent expansion |
| **Post-Retrieval** | CRAG | Corrective retrieval with relevance grading |
| | Cross-Encoder Reranking | Joint query-document scoring (~15-20% precision) |
| | GraphRAG | Pipeline dependency graph context injection |
| **During Generation** | FLARE | Forward-looking active retrieval on low confidence |
| | Self-RAG | Self-reflective [Supported]/[Relevant]/[Useful] eval |

### Smart Routing
- Pipeline ownership mapping with automatic routing
- LLM fallback for unknown pipelines
- Severity classification with self-consistency
- Auto-escalation based on SLA thresholds

### Conversational Troubleshooting
- Context-aware chat bound to active alerts
- Intent classification for specialized handlers
- Quick actions: similar incidents, runbooks, logs, escalation
- Active prompting with uncertainty detection

---

## Project Structure

```
alert-whisperer/
├── app.py                          # Streamlit entry point
├── pyproject.toml                  # Python project configuration
├── .env.example                    # Environment variable template
├── README.md                       # This file
│
├── config/
│   ├── settings.py                 # Pydantic Settings (incl. Azure AI Search + RAG toggles)
│   └── pipeline_ownership.json     # Pipeline → team mapping
│
├── src/
│   ├── models.py                   # All Pydantic models
│   │
│   ├── connectors/                 # MCP Server clients
│   │   ├── mcp_base.py            # Base MCP client (JSON-RPC 2.0)
│   │   ├── kusto_connector.py     # Kusto / ADX MCP client
│   │   ├── confluence_connector.py # Confluence MCP client
│   │   ├── icm_connector.py       # ICM MCP client
│   │   └── loganalytics_connector.py # Log Analytics MCP client
│   │
│   ├── rag/                        # RAG pipeline (12 techniques)
│   │   ├── vector_store.py        # Azure AI Search + text-embedding-3-large (3072d)
│   │   │                           #   BM25Index, EmbeddingClient, ParentChildChunkManager,
│   │   │                           #   SemanticChunker, VectorStore
│   │   ├── retriever.py           # RAG retriever orchestrating all techniques
│   │   │                           #   TopicTreeRetriever, CRAGEvaluator, LLMJudgeCRAG,
│   │   │                           #   HyDEGenerator, FLARERetriever, GraphRAGEngine,
│   │   │                           #   AgenticRAGOrchestrator, RAGRetriever
│   │   └── advanced_techniques.py # New RAG techniques
│   │                               #   CrossEncoderReranker, QueryRewriter,
│   │                               #   RAGFusion, SelfRAG
│   │
│   ├── prompts/                    # Prompt engineering
│   │   ├── templates.py           # All prompt templates
│   │   └── prompt_engine.py       # Prompt construction + LLM invocation
│   │
│   ├── engine/                     # Core processing
│   │   ├── alert_processor.py     # Alert detection + RCA + routing
│   │   ├── chat_engine.py         # Conversational troubleshooting
│   │   └── data_refresh.py        # Live data refresh engine
│   │
│   └── utils/
│       ├── logging_config.py      # Structured logging (structlog)
│       └── demo_data.py           # Realistic demo data generator
│
├── ui/
│   └── components/                 # Streamlit UI components
│       ├── sidebar.py             # Alert feed + navigation
│       ├── dashboard.py           # Metrics + charts
│       ├── chat_panel.py          # Chat interface
│       ├── alert_detail.py        # Alert deep-dive view
│       ├── knowledge_base.py      # KB management
│       └── settings_page.py       # Configuration page
│
├── tests/                          # Test suite
├── docs/                           # Documentation
│   ├── ARCHITECTURE.md            # Detailed architecture doc
│   ├── PROMPT_ENGINEERING.md      # Prompt design documentation
│   ├── RAG_Pipeline_Flow.md       # Complete RAG pipeline flow (query → response)
│   ├── Vector_Search_Schema.md    # Index schemas, HNSW, embeddings, examples
│   └── scenarios/                 # End-to-end scenario walkthroughs
│       ├── Scenario_01_Spark_OOM.md
│       ├── Scenario_02_Kusto_Mapping.md
│       ├── Scenario_03_Cascading_Failure.md
│       └── Scenario_04_Ambiguous_Query.md
│
└── data/
    ├── vector_store/              # Local fallback store (dev only)
    ├── cache/                     # Query cache
    └── logs/                      # Application logs
```

---

## Setup

### Prerequisites
- Python 3.10+
- MCP Servers running for each data source (Kusto, Confluence, ICM, Log Analytics)
- Azure OpenAI or OpenAI API key
- Azure AI Search service (or use in-memory fallback for local dev)
- Azure AD credentials for Kusto/Log Analytics

### Installation

```bash
# Clone and install
cd alert-whisperer
pip install -e ".[dev]"

# Configure environment
cp .env.example .env
# Edit .env with your credentials (Azure AI Search, OpenAI, MCP endpoints)

# Run the application
streamlit run app.py
```

### MCP Server Setup

Each data source requires a running MCP server. Start them before the application:

```bash
# Kusto MCP Server
npx @azure/mcp-server-kusto --port 3001

# Confluence MCP Server
npx @anthropic/mcp-server-confluence --port 3002

# ICM MCP Server (custom)
python mcp_servers/icm_server.py --port 3003

# Log Analytics MCP Server
npx @azure/mcp-server-log-analytics --port 3004
```

### Demo Mode

The application runs in demo mode by default (no MCP connections or Azure AI Search required). Demo data is generated automatically. The in-memory vector store with BM25 provides full retrieval capability without any cloud dependencies. To connect production services, configure the `.env` file.

---

## RAG Techniques

| # | Technique | Phase | Latency | Purpose |
|---|---|---|---|---|
| 1 | Query Rewriting | Pre-retrieval | 0-250ms | Synonym/abbreviation expansion for Spark/Synapse/Kusto vocabulary |
| 2 | RAG Fusion (RRF) | Pre-retrieval | 0-500ms | Multi-query decomposition for complex questions |
| 3 | HyDE | Pre-retrieval | 0-2ms | Hypothetical document embedding for vocabulary bridging |
| 4 | Topic Tree | Pre-retrieval | 0-1ms | Hierarchical error classification |
| 5 | Semantic Chunking | Pre-retrieval | 10-50ms/doc | Embedding-similarity boundary detection for natural chunk breaks |
| 6 | Azure AI Search Hybrid | Retrieval | 50-150ms | Vector (3072d HNSW) + BM25 keyword + semantic reranking |
| 7 | Agentic RAG | Retrieval | 0-50ms | Multi-source orchestration (Confluence/ICM/Log Analytics) |
| 8 | Parent-Child Chunking | Retrieval | 0-5ms | Token-based (450 tokens) child retrieval with parent expansion |
| 9 | CRAG | Post-retrieval | 1-500ms | Corrective relevance grading and filtering |
| 10 | Cross-Encoder Reranking | Post-retrieval | 1-500ms | Joint query-doc scoring (LLM or dedicated model) |
| 11 | GraphRAG | Post-retrieval | 0-5ms | Pipeline dependency graph blast radius / failure chains |
| 12 | FLARE | During generation | 0-500ms | Forward-looking active retrieval on low confidence |
| 13 | Self-RAG | During generation | 0-800ms | Self-reflective [Supported]/[Relevant] evaluation |

## Prompt Engineering Techniques Used

| Technique | Where Used | Purpose |
|-----------|-----------|---------| 
| Zero-shot | Simple error classification | Direct classification of known error patterns |
| Few-shot | Root cause analysis | Curated examples guide analysis of new failures |
| Chain-of-Thought | Complex RCA | Step-by-step reasoning through failure chains |
| Tree-of-Thought | Ambiguous errors | Explore multiple hypotheses, converge on best |
| ReAct | Interactive troubleshooting | Alternate reasoning and tool actions |
| Self-consistency | Severity classification | Three independent assessments, majority vote |
| Active prompting | Chat responses | Detect uncertainty, suggest clarification |
| Reflexion | Response refinement | Self-critique loop to improve initial responses |
| Chain-of-Verification | Factual safety | Claim extraction → verification → correction |
| Persona/role | System prompt | Expert data pipeline diagnostician persona |

---

## MCP Integration

Alert Whisperer communicates with all data sources through the Model Context Protocol (MCP), providing:

- **Standardized interface**: All connectors use the same JSON-RPC 2.0 transport
- **Tool discovery**: Automatic discovery of available tools on each MCP server
- **Retry logic**: Exponential backoff for transient failures
- **Health checks**: Connection validation before operations
- **Structured I/O**: Pydantic models for all request/response types

---

## License

MIT
