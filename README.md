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
│  │   PROMPT ENGINE       │  │   RAG ENGINE                     │    │
│  │  - Template Registry  │  │  - Vector Store (ChromaDB)       │    │
│  │  - Few-Shot Examples  │  │  - Topic Tree Retrieval          │    │
│  │  - CoT / ToT / ReAct  │  │  - Multi-Source Aggregation      │    │
│  │  - Severity Classifier│  │  - Reranking                     │    │
│  │  - Routing Prompt     │  │  - Context Window Management     │    │
│  └──────────┬───────────┘  └──────────────┬───────────────────┘    │
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

### RAG Knowledge Base
- ChromaDB vector store for similarity search
- Multi-source indexing (Confluence, ICM, Log Analytics)
- Topic tree for hierarchical retrieval
- Context window management and reranking

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
│   ├── settings.py                 # Pydantic Settings configuration
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
│   ├── rag/                        # RAG pipeline
│   │   ├── vector_store.py        # ChromaDB vector store
│   │   └── retriever.py           # RAG retriever + topic tree
│   │
│   ├── prompts/                    # Prompt engineering
│   │   ├── templates.py           # All prompt templates
│   │   └── prompt_engine.py       # Prompt construction + LLM invocation
│   │
│   ├── engine/                     # Core processing
│   │   ├── alert_processor.py     # Alert detection + RCA + routing
│   │   └── chat_engine.py         # Conversational troubleshooting
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
│   └── PROMPT_ENGINEERING.md      # Prompt design documentation
│
└── data/
    ├── vector_store/              # ChromaDB persistence
    ├── cache/                     # Query cache
    └── logs/                      # Application logs
```

---

## Setup

### Prerequisites
- Python 3.10+
- MCP Servers running for each data source (Kusto, Confluence, ICM, Log Analytics)
- Azure OpenAI or OpenAI API key
- Azure AD credentials for Kusto/Log Analytics

### Installation

```bash
# Clone and install
cd alert-whisperer
pip install -e ".[dev]"

# Configure environment
cp .env.example .env
# Edit .env with your credentials

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

The application runs in demo mode by default (no MCP connections required). Demo data is generated automatically to showcase all features. To connect live data sources, configure the `.env` file and start the MCP servers.

---

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
| Persona/role | System prompt | Expert data pipeline diagnostician persona |
| Task breakdown | Log parsing | Multi-step extraction from noisy logs |
| Output indicators | All templates | Structured output format specifications |
| Text delimiters | All templates | Clear section markers (<<<LOGS>>>, etc.) |

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
