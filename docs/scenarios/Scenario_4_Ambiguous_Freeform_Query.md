# Scenario 4 — Ambiguous Freeform Query ("What's broken in the last hour?")

> **Audience:** Senior Data Engineer building Alert Whisperer — a conversational troubleshooting assistant backed by Azure AI Search, `text-embedding-3-large` (3072 dims), and 13 RAG techniques for Spark/Synapse/Kusto incident response.
>
> **Purpose:** A complete, concrete walkthrough of all 13 techniques processing an intentionally ambiguous, system-agnostic status query. This scenario is the canonical example of **Query Rewriting + Agentic RAG + CRAG dominance** — where the query contains no error codes, no pipeline names, and no system context, and the system must synthesize a coherent, consistent status summary from multiple live data sources. The scenario also demonstrates the **data consistency guarantee**: semantically equivalent phrasings must produce identical results.

---

## Scenario Setup

| Field | Value |
|---|---|
| **Query** | `"what's broken in the last hour?"` |
| **Query type** | Freeform natural language — no error code, no pipeline, no system |
| **Active ICM Incidents** | 2 (ICM-2847391: Spark OOM on `spark_etl_daily_ingest`; ICM-2849012: Kusto throttling on `kusto_ingestion_telemetry`) |
| **Failed Pipeline Runs (last hour)** | 5 (`spark_etl_daily_ingest`, `spark_bronze_to_silver_events`, `synapse_pipeline_customer360`, `synapse_dataflow_daily_summary`, `kusto_ingestion_telemetry`) |
| **SLA Breaches** | 1 (`kusto_ingestion_telemetry`, expected completion 11:00 AM CDT, now 30 minutes overdue) |
| **Time of Query** | 11:30 AM CDT |
| **Engineer** | On-call support engineer, no prior context on either incident |

**What the engineer could mean by "what's broken in the last hour?":**

1. Active ICM incidents — the formal incident record with severity, title, and ownership
2. Failed pipeline runs — individual execution failures visible in Log Analytics or ADF monitoring
3. Error log spikes — anomalous error rate increases in Log Analytics
4. SLA breaches — pipelines that missed their contractual completion deadline
5. All of the above simultaneously

The engineer does not specify. The system must handle all five interpretations.

**Current Alert Whisperer context window:**

```
active_alerts: [
  { id: "ICM-2847391", title: "spark_etl_daily_ingest FAILED", severity: 2, system: "Spark", error: "OutOfMemoryError" },
  { id: "ICM-2849012", title: "kusto_ingestion_telemetry THROTTLED", severity: 3, system: "Kusto", error: "ThrottlingException" }
]
recent_chat_history: []   ← no prior context in this session
```

---

## The Data Consistency Imperative

Before walking through the 13 techniques, it is critical to understand the explicit consistency requirement this scenario exists to demonstrate.

The system owner's requirement, verbatim:

> *"Equivalent questions must return the same results regardless of phrasing: 'issues' = 'incidents' = 'What's broken?'"*

And:

> *"I should be able to get the issues / incidents and all the details when I post a question in the chat — could be for an hour or in the last 90 days etc kind of scenarios."*

This means all five of the following queries, submitted independently in fresh sessions, must return the **same 7 retrieved chunks** and produce **indistinguishable answers**:

| Query | Surface form | Synonyms involved |
|---|---|---|
| `"what's broken in the last hour?"` | Status + time window | "broken" |
| `"show me recent issues"` | Status + recency | "issues" |
| `"any incidents in the last 60 minutes?"` | Status + time window | "incidents" |
| `"what failed recently?"` | Status + recency | "failed" |
| `"current problems"` | Status only | "problems" |

The consistency is not accidental — it is deliberately engineered through a combination of synonym normalization, LLM rewriting, and context enrichment. The mechanism is described in detail in the [Data Consistency Deep Dive](#data-consistency-deep-dive) section and is demonstrated concretely at each technique step where it applies.

---

## Phase 1: Pre-Retrieval

*Before any document is fetched, three techniques transform the raw ambiguous query into a rich, multi-perspective set of retrieval signals. For specific error queries like Scenario 1, this phase is a precision sharpening step. For ambiguous status queries like this one, it is a **meaning reconstruction step** — the query carries almost no information, and these techniques must supply the missing signal.*

---

### Technique 1: Topic Tree Classification

**What it is:** A hierarchical taxonomy of error categories maintained as a lightweight in-memory tree. Every incoming query is routed to one or more leaf nodes before retrieval begins. This scopes retrieval to a relevant subset of the knowledge base.

**Input:**
```
"what's broken in the last hour?"
```

**Topic tree structure (all branches shown):**

```
Infrastructure Errors
└── Data Pipeline Errors                   ← ROOT MATCH (score: 0.61)
    ├── Spark Errors
    │   ├── OutOfMemory
    │   ├── ShuffleFetchFailure
    │   └── StageFailure
    ├── Shuffle Failures
    │   ├── FetchFailed
    │   └── ShuffleMapOutputLost
    ├── Synapse Errors
    │   ├── DWU Exhaustion
    │   └── PolyBase Failures
    └── Kusto Errors
        ├── Throttling
        └── IngestionFailure
```

**Classification result:**

No leaf node match is possible. The classifier cannot identify a specific error type because the query contains no error code, no stack trace, no pipeline name, and no system identifier. Keyword matching finds no tokens in the SYNONYM_MAP that resolve to a specific node.

The root node `Data Pipeline Errors` scores 0.61 because the system detects a "status inquiry" intent pattern (the query matches the intent classifier's `STATUS_QUERY` heuristic: queries containing "broken", "failed", "issues", or "problems" in the absence of a specific error token are classified as status queries, not troubleshooting queries). Score 0.61 exceeds the branch-match suppression threshold of 0.50, so the root node is returned.

**Output:**

```python
topic_context = "Data Pipeline Errors"   # root only — no branch narrowing
topic_filter_tags = []                   # no leaf-level tags applicable
excluded_tags = []                       # nothing excluded either
classification_mode = "STATUS_QUERY"     # special mode: broad retrieval
```

**Why this matters:**

Topic Tree is deliberately ineffective here, and that is the correct outcome. It is designed to narrow the search space for specific, classifiable errors — not broad status questions. By detecting the `STATUS_QUERY` mode and returning zero `topic_filter_tags`, the Topic Tree signals to the downstream pipeline: *do not gate retrieval — retrieve broadly.*

This graceful degradation is a critical design decision. A system that forces a topic branch on every query would catastrophically mis-scope this query — for example, routing it only to Spark runbooks because the active alerts context contains `OutOfMemoryError`. Topic Tree correctly does not overshoot its competence boundary.

The `STATUS_QUERY` classification mode acts as a flag that subsequent techniques (especially Agentic RAG) can check to adjust their retrieval strategy from "targeted depth search" to "breadth-first multi-source scan."

---

### Technique 2: Query Rewriting

**What it is:** A multi-stage query transformation pipeline that converts the raw, terse, or ambiguous user query into multiple rich retrieval variants. For specific error queries, it expands abbreviations and adds technical synonyms. For ambiguous status queries, it does something far more powerful: it reconstructs the query's missing intent and injects live system context to produce a concrete, structured retrieval signal.

**This is the star technique for this scenario.**

**Input:**
```
"what's broken in the last hour?"
```

---

#### Stage 2a — Synonym Expansion

The synonym dictionary maps natural language status terms to their data platform equivalents:

```python
SYNONYM_MAP = {
    # Status synonyms
    "broken":    ["job failed", "pipeline failure", "activity error", "run failed", "execution error"],
    "issues":    ["job failed", "pipeline failure", "activity error", "run failed", "execution error"],
    "incidents": ["job failed", "pipeline failure", "activity error", "run failed", "execution error"],
    "failed":    ["job failed", "pipeline failure", "activity error", "run failed", "execution error"],
    "problems":  ["job failed", "pipeline failure", "activity error", "run failed", "execution error"],
    "errors":    ["job failed", "pipeline failure", "activity error", "run failed", "execution error"],
    # Time synonyms
    "last hour":      ["last 60 minutes", "past hour", "T-60m", "last 1 hour"],
    "recently":       ["last hour", "last 60 minutes", "last 6 hours"],
    "recent issues":  ["job failed", "pipeline failure", "activity error", "run failed", "execution error"],
    "current":        ["active", "ongoing", "open", "unresolved"],
}
```

**KEY INSIGHT — DATA CONSISTENCY:**

Notice that `"broken"`, `"issues"`, `"incidents"`, `"failed"`, `"problems"`, and `"errors"` all map to **the identical synonym expansion string**: `"job failed pipeline failure activity error run failed execution error"`.

This is not coincidence. The synonym map was deliberately designed so that all natural language status terms collapse to the same canonical representation. The effect: a query containing "broken" and a query containing "issues" produce the same expanded token set, and therefore the same BM25 and vector retrieval signals. Data consistency begins here, at the first transformation step.

After synonym expansion, all five equivalent queries expand to the same string:

```
# Input: "what's broken in the last hour?"
# Input: "show me recent issues"
# Input: "any incidents in the last 60 minutes?"
# Input: "what failed recently?"
# Input: "current problems"
#
# All expand to the same canonical form:
"job failed pipeline failure activity error run failed execution error [last 60 minutes / active / ongoing]"
```

**Variant 1 — Synonym expanded:**
```
"what's broken in the last hour? job failed pipeline failure activity error run failed execution error last 60 minutes"
```

---

#### Stage 2b — Abbreviation Expansion

No abbreviations are present in the raw query. No expansion needed.

---

#### Stage 2c — Context Enrichment from Active Alerts

The active alerts context window contains 2 live ICM incidents. The query rewriter reads the current context and injects specific pipeline and error identifiers:

```
pipeline:spark_etl_daily_ingest error:OutOfMemoryError
pipeline:kusto_ingestion_telemetry error:ThrottlingException
```

**Variant 2 — Context enriched:**
```
"pipeline failures and incidents in the last hour pipeline:spark_etl_daily_ingest error:OutOfMemoryError pipeline:kusto_ingestion_telemetry error:ThrottlingException job failed activity error run failed"
```

**Why context enrichment is critical for consistency:**

All five equivalent queries see the same active alerts context window (the 2 ICM incidents are system-state, not query-derived). Therefore, all five queries receive the same context enrichment injection, regardless of how the question was phrased. The phrasing variation is neutralized before it can affect retrieval.

---

#### Stage 2d — LLM Rewriting (GPT-4o-mini)

For a specific error query, LLM rewriting adds likely root cause vocabulary. For this ambiguous status query, LLM rewriting does something fundamentally different: it **reconstructs the multi-faceted intent** that the ambiguous surface form implied but didn't state.

**Prompt sent to GPT-4o-mini:**
```
System: You are a search query optimizer for a data platform incident management system.
The engineer's query is ambiguous — it does not specify a system, error, or pipeline.
Rewrite it as a structured natural language query that covers ALL possible interpretations:
active ICM incidents, failed pipeline runs, error log spikes, and SLA breaches.
Use the active alert context to make the query concrete.

Query: "what's broken in the last hour?"
Active alerts context: [ICM-2847391: spark_etl_daily_ingest OutOfMemoryError, ICM-2849012: kusto_ingestion_telemetry ThrottlingException]
Time: 11:30 AM CDT
Time window: last 60 minutes (10:30 AM - 11:30 AM CDT)
```

**GPT-4o-mini output (Variant 3 — LLM rewritten):**
```
"List all active ICM incidents, failed pipeline runs, and error log spikes from the last 60 minutes
across Spark, Synapse, and Kusto systems. Include: active incident summaries for ICM-2847391
(spark_etl_daily_ingest OutOfMemoryError) and ICM-2849012 (kusto_ingestion_telemetry
ThrottlingException), pipeline execution failures in the ADF monitoring log between 10:30 AM and
11:30 AM CDT, error count anomalies in Log Analytics from the same window, and any SLA breach
notifications for pipelines with completion deadlines between 10:30 AM and 11:30 AM CDT."
```

This rewrite is the most important transformation in the entire pipeline. It converts a 9-word vague question into a 85-word structured multi-system query that explicitly enumerates every data source and time filter needed to answer the question completely. A vector search against the Knowledge Base using this rewritten query will retrieve incident summaries, pipeline failure logs, and SLA breach records — exactly the right sources.

**Final output — 4 query variants:**

| Variant | Type | Key purpose |
|---|---|---|
| `V1` | Original preserved | BM25 exact match on "broken", "hour" |
| `V2` | Synonym expanded | Canonical status token matching |
| `V3` | Context enriched | Injects specific pipeline/error identifiers |
| `V4` | LLM rewritten | Multi-faceted intent reconstruction |

All four variants are passed to the retrieval layer. V2 is the consistency anchor — it is identical for all five equivalent phrasings.

---

### Technique 3: HyDE (Hypothetical Document Embeddings)

**What it is:** The LLM generates a hypothetical "ideal answer document" — a text that looks like it would appear in the Knowledge Base if the answer were stored there. The embedding of this hypothetical document is used as an additional retrieval vector alongside the actual query embedding.

**Input:**
```
V4 (LLM-rewritten query): "List all active ICM incidents, failed pipeline runs..."
```

**GPT-4o-mini generates the hypothetical document:**

```
HYPOTHETICAL DOCUMENT (generated, not real):

"Data Platform Status Summary — 10:30 AM to 11:30 AM CDT

ACTIVE INCIDENTS:

[ICM-2847391] spark_etl_daily_ingest — FAILED
Severity: 2 | System: Azure Databricks
Error: java.lang.OutOfMemoryError: Java heap space
Stage: 12 (ShuffleMapStage) | Executor: executor-008
Status: Under investigation | Owner: data-platform-team@contoso.com

[ICM-2849012] kusto_ingestion_telemetry — THROTTLED
Severity: 3 | System: Azure Data Explorer (Kusto)
Error: ThrottlingException — ingestion rate exceeded cluster capacity
Ingest queue depth: 847 pending messages
Status: Active | Owner: data-platform-team@contoso.com

FAILED PIPELINE RUNS (last 60 minutes):
1. spark_etl_daily_ingest — FAILED at 10:52 AM (OutOfMemoryError)
2. spark_bronze_to_silver_events — FAILED at 11:03 AM (dependency blocked by #1)
3. synapse_pipeline_customer360 — FAILED at 11:15 AM (timeout, downstream of #1)
4. synapse_dataflow_daily_summary — FAILED at 11:18 AM (timeout, downstream of #1)
5. kusto_ingestion_telemetry — FAILED at 11:01 AM (ThrottlingException)

SLA BREACHES:
- kusto_ingestion_telemetry: SLA deadline 11:00 AM CDT — BREACHED by 30 minutes

BLAST RADIUS: 4 downstream pipelines impacted by spark_etl_daily_ingest failure."
```

**Why HyDE works here:**

The hypothetical document is structured as an operational status report. The Azure AI Search Knowledge Base contains exactly this type of document: daily status summaries, ICM incident reports, and shift handoff notes. The embedding of the hypothetical document — dense in status report vocabulary ("active incidents," "failed pipeline runs," "blast radius," "SLA breach") — is close in 3072-dimensional space to real status summaries stored in the KB.

HyDE bridges the gap between the query's sparse signal ("what's broken?") and the KB's dense status documents. Without HyDE, the original query embedding might be closest to generic "how to troubleshoot" documents. The hypothetical document's embedding is specifically tuned to status summaries.

**HyDE vector:** A 3072-dim embedding of the hypothetical document above is passed to Azure AI Search as a third retrieval vector alongside the V2 and V4 query embeddings.

---

## Phase 2: Retrieval

*The retrieval phase fetches candidate chunks from all sources. For specific error queries, the goal is precision — retrieving the right runbook and the right incident history. For this ambiguous status query, the goal is breadth-first coverage — retrieving the right types of documents from the right sources within the right time window.*

---

### Technique 4: RAG Fusion

**What it is:** Instead of issuing a single retrieval query, RAG Fusion decomposes the question into multiple independent sub-queries, each targeting a different facet of the answer. Results are retrieved in parallel and merged using Reciprocal Rank Fusion (RRF).

**Input:**
```
V4 (LLM-rewritten): "List all active ICM incidents, failed pipeline runs, and error log
spikes from the last 60 minutes across Spark, Synapse, and Kusto systems..."
```

**Decomposition (GPT-4o-mini generates 3 sub-queries):**

```python
sub_queries = [
    "active incidents ICM alerts last hour current status severity open",          # Facet 1: ICM
    "pipeline failures errors last 60 minutes all systems run failed execution",   # Facet 2: Pipeline runs
    "SLA breaches monitoring alerts deadline missed recent kusto synapse spark",   # Facet 3: SLA/monitoring
]
```

This decomposition is the correct behavior for "what's broken." The question has three distinct answer components:
- The formal incident record (ICM)
- The operational failure log (ADF/Databricks run history)
- The SLA/contractual breach record (monitoring)

Each sub-query retrieves independently. RRF then merges the three ranked lists using the formula `1 / (k + rank_i)` for each document across sub-queries, where `k = 60` (standard RRF constant).

**Documents appearing in all three ranked lists receive a significant RRF score boost.** In practice, the ICM incident summaries appear in both sub-query 1 and sub-query 2 results (they describe a failure AND they are the formal incident record), so they rise to the top of the merged list.

**Merged retrieval from RAG Fusion:**

| Rank | Document | RRF Score | Sub-queries where it appeared |
|---|---|---|---|
| 1 | ICM-2847391 incident summary (Spark OOM) | 0.042 | 1, 2 |
| 2 | ICM-2849012 incident summary (Kusto throttling) | 0.039 | 1, 3 |
| 3 | Pipeline failure log: spark_etl_daily_ingest 11:30 AM run | 0.031 | 2 |
| 4 | Pipeline failure log: kusto_ingestion_telemetry 11:01 AM run | 0.029 | 2, 3 |
| 5 | SLA breach record: kusto_ingestion_telemetry 11:00 AM deadline | 0.027 | 3 |
| 6 | Pipeline failure log: spark_bronze_to_silver_events 11:03 AM run | 0.024 | 2 |
| 7 | Pipeline failure log: synapse_pipeline_customer360 11:15 AM run | 0.021 | 2 |

These 7 documents are the **core retrieval set**. They are the same 7 chunks that must be retrieved by all five equivalent phrasings — this is the consistency guarantee that the synonym expansion + LLM rewriting ensures.

---

### Technique 5: Azure AI Search Hybrid Retrieval

**What it is:** Azure AI Search's hybrid retrieval mode combines BM25 (keyword/lexical) scoring with vector (semantic) similarity scoring. For most queries, the combination outperforms either method alone. For this ambiguous query, the two modes have dramatically different performance profiles.

**BM25 performance (keyword matching):**

The original query `"what's broken in the last hour?"` contains no specific technical tokens. "Broken" does not appear in ICM incident titles (they use "FAILED", "THROTTLED", "OOM"). "Last hour" is a time window, not a keyword. BM25 retrieves noise: old resolved incidents containing "broken" in their post-mortem text, Confluence FAQ entries titled "What's broken in my Spark job?", and irrelevant monitoring dashboard descriptions.

**BM25 contribution: WEAK for the original query.**

However, after Query Rewriting, the V2 and V3 variants contain highly BM25-matchable tokens:
- `"OutOfMemoryError"` → exact BM25 match to ICM-2847391 title and description
- `"ThrottlingException"` → exact BM25 match to ICM-2849012 title and description
- `"pipeline failure"` → partial BM25 match to all 5 failed pipeline run documents

**BM25 contribution: STRONG for the enriched query variants.**

**Vector search performance:**

Vector search uses `text-embedding-3-large` (3072 dims). The embedding of "what's broken in the last hour?" is semantically closest to:
- Status summary documents ("data platform health check", "current incident overview")
- Operational standup notes
- Shift handoff reports

This is correct — but these documents are summary-level and may not contain granular failure details. The HyDE vector (the hypothetical status report embedding) performs better than the raw query embedding: it is semantically close to the ICM incident summaries because it uses the same operational vocabulary.

**Vector contribution: STRONG via HyDE vector. MODERATE via raw query vector.**

**Hybrid fusion result:**

Azure AI Search's hybrid scoring (`@search.score = BM25_score * α + vector_score * β`, where `α = 0.4`, `β = 0.6` for this query profile) with the four query variants and HyDE vector collectively returns a robust 7-document candidate set that matches the RAG Fusion output above.

**Critical design insight:** For this scenario, the hybrid retrieval's quality depends almost entirely on the Query Rewriting step. Without the context-enriched V3 variant injecting `"OutOfMemoryError"` and `"ThrottlingException"`, BM25 would be near-useless. This is why Query Rewriting is rated ★★★★★ for this scenario — it directly enables effective BM25 retrieval.

---

### Technique 6: Agentic RAG

**What it is:** A planning layer that decides which external tools (MCP servers) to query, in what order, with what parameters. For specific error queries, it routes to a single targeted source (e.g., Confluence runbook for OOM resolution). For this ambiguous status query, it plans a **broad multi-source retrieval sweep**.

**Input:**
```
Classification mode: STATUS_QUERY
Topic context: "Data Pipeline Errors" (root only)
Time window extracted: "last hour" → 10:30 AM to 11:30 AM CDT
```

**Agentic RAG plan:**

```python
retrieval_plan = {
    "primary_sources": [
        {
            "source": "ICM_MCP",
            "query": "Show all active and recently opened incidents in the last 60 minutes",
            "params": {
                "status": ["Active", "Acknowledged", "Mitigating"],
                "time_range": "2026-03-08T10:30:00Z to 2026-03-08T11:30:00Z",
                "severity": [1, 2, 3],
            },
            "rationale": "Primary source for formal incident records. STATUS_QUERY always queries ICM first.",
        },
        {
            "source": "LogAnalytics_MCP",
            "query": "Error count spikes and pipeline failures in the last 60 minutes",
            "params": {
                "kql": """
                    union AzureDiagnostics, ADFActivityRun, DatabricksJobs
                    | where TimeGenerated >= ago(1h)
                    | where Level == 'Error' or Status == 'Failed'
                    | summarize ErrorCount = count() by bin(TimeGenerated, 5m), PipelineName
                    | where ErrorCount > 0
                    | order by TimeGenerated desc
                """,
                "time_range": "last_60_minutes",
            },
            "rationale": "Secondary source for operational failure detail not yet in ICM.",
        },
    ],
    "secondary_sources": [
        {
            "source": "Kusto_MCP",
            "query": "Pipeline run summary last hour",
            "params": {
                "kql": """
                    PipelineRunHistory
                    | where StartTime >= ago(1h)
                    | where RunStatus in ('Failed', 'Cancelled', 'TimedOut')
                    | project PipelineName, StartTime, EndTime, RunStatus, ErrorMessage
                    | order by StartTime desc
                """,
            },
            "rationale": "Tertiary source for pipeline execution history with error messages.",
        },
    ],
    "skipped_sources": [
        {
            "source": "Confluence_MCP",
            "reason": "No specific error to look up a runbook for. STATUS_QUERY mode skips runbook retrieval — it would inject troubleshooting content that doesn't answer 'what's broken'.",
        },
    ],
}
```

**Why skipping Confluence is the right call:**

This is a significant routing decision. In Scenario 1 (Spark OOM), Confluence is the primary source — it contains the resolution runbook. Here, the engineer asked a status question, not a troubleshooting question. If Agentic RAG incorrectly queried Confluence, it would retrieve OOM resolution steps and Kusto throttling runbooks — high-quality documents that are completely wrong for the intent. CRAG would catch them later (see Technique 9), but it is better to route correctly upfront.

The `STATUS_QUERY` classification flag from Topic Tree is what enables this correct routing decision. Topic Tree's graceful degradation directly informs Agentic RAG's source selection.

**Time window extraction:**

```python
def extract_time_window(query: str) -> dict:
    patterns = {
        r"last hour|last 60 minutes|past hour": {"window": "1h", "label": "last_hour"},
        r"last (\d+) minutes": {"window": "{n}m", "label": "last_{n}_minutes"},
        r"last (\d+) days": {"window": "{n}d", "label": "last_{n}_days"},
        r"last 90 days": {"window": "90d", "label": "last_90_days"},
        r"today": {"window": "since_midnight", "label": "today"},
        r"recently|recent": {"window": "6h", "label": "recent_default"},
    }
    # "last hour" → {"window": "1h", "label": "last_hour"}
```

The extracted `window: "1h"` is passed as a filter parameter to both the ICM MCP and Log Analytics MCP queries. This is how freeform time language ("last hour", "last 90 days", "today") translates to precise query parameters across all MCP server calls.

**Agentic RAG output:**

- ICM MCP returns: 2 active incidents (ICM-2847391, ICM-2849012) with full metadata
- Log Analytics MCP returns: 5 pipeline failure records with error messages and timestamps
- Kusto MCP returns: Confirmation of failure records (overlaps with Log Analytics)

Total: 9 raw documents returned to the retrieval pipeline (before CRAG filtering).

---

### Technique 7: Parent-Child Chunking

**What it is:** Large documents (runbooks, incident post-mortems, architecture docs) are indexed in two layers: small "child" chunks for precise retrieval, and larger "parent" chunks that include surrounding context. When a child chunk is retrieved, its parent chunk is loaded to provide context.

**Impact for this scenario: MODERATE-LOW.**

For specific error troubleshooting queries (Scenarios 1–3), parent-child chunking is high-impact: the engineer needs the full runbook context around a retrieved resolution step. For this status query, the engineer needs breadth across many documents, not depth within any single document.

**Where it does apply:**

The ICM incident summaries were indexed with parent-child structure:
- **Child chunk**: Title + Severity + Error type (48 tokens) — retrieved by vector search
- **Parent chunk**: Full incident record including impact assessment, affected systems, timeline, and owner contact (312 tokens) — loaded when child is retrieved

When ICM-2847391's title chunk is retrieved (scoring 0.92 in cross-encoder ranking), its parent chunk is loaded, providing the full incident context including the blast radius note: "2 downstream pipelines affected: `spark_bronze_to_silver_events`, `synapse_pipeline_customer360`." This downstream impact data feeds into the GraphRAG prioritization step (Technique 10).

**Parent-Child summary for this scenario:**

| Document | Child retrieved | Parent loaded | Additional context gained |
|---|---|---|---|
| ICM-2847391 | Yes | Yes | Full incident record + blast radius |
| ICM-2849012 | Yes | Yes | Full incident record + queue depth |
| Pipeline failure logs | Yes | No (flat structure — no parent) | N/A |

---

### Technique 8: Semantic Chunking

**What it is:** During indexing, documents are split at semantic boundaries (paragraph breaks, section headers, topic shifts) rather than fixed token counts. This ensures that semantically coherent units of information — like an incident's full description or a runbook section on memory configuration — remain intact as single retrievable chunks.

**Impact for this scenario: MODERATE.**

**Where it matters:**

The ICM incident summaries are stored in a semi-structured format:

```
[INCIDENT TITLE]
[Severity and classification]
[Technical description of the error]
[Impact assessment — systems and data affected]
[Current status and owner]
```

A fixed-size chunker (e.g., 512 tokens with overlap) would potentially split an incident summary between the "Technical description" and "Impact assessment" sections. The retrieved chunk would contain the error details but not the downstream impact — meaning the engineer would not know how many pipelines are affected without a follow-up query.

Semantic chunking preserves the full incident summary — Title through Impact Assessment — as one coherent chunk. When ICM-2847391 is retrieved, the engineer immediately sees: error type, affected pipeline, severity, AND the 2 downstream pipelines blocked. This completeness matters for a status question.

**Semantic boundary detection for ICM records:**

```python
# Semantic chunker configuration for ICM documents
SEMANTIC_BOUNDARIES = [
    r"^\[ICM-\d+\]",          # New incident header
    r"^Impact Assessment",     # Impact section start
    r"^Resolution Steps",      # Resolution section start (split here — different intent)
    r"^Post-Mortem",           # Post-mortem section start (split here — different time context)
]
```

The chunker splits at "Resolution Steps" — ensuring that the retrieval chunk for a status query contains the incident description and impact, but not the resolution runbook steps. This improves CRAG precision (Technique 9) because the retrieved chunk's content matches the status-oriented query intent rather than containing mixed status + troubleshooting content.

---

## Phase 3: Post-Retrieval

*After retrieval, three techniques work together to filter noise, add structural context, and re-rank for relevance. For specific error queries, post-retrieval is a refinement step. For ambiguous status queries, it is a noise elimination step — the broad retrieval necessarily brings in marginally relevant content that must be filtered.*

---

### Technique 9: CRAG (Corrective RAG)

**What it is:** A post-retrieval evaluation step that scores each retrieved chunk on relevance to the query, categorizes it as CORRECT / AMBIGUOUS / INCORRECT, and decides whether to include it in the context window, include it with a warning, or discard it. For AMBIGUOUS and INCORRECT chunks, CRAG optionally triggers additional targeted retrieval to fill the gap.

**This technique is high-impact for this scenario** because the broad retrieval (STATUS_QUERY mode) inevitably returns chunks that are tangentially relevant (correct error type, wrong time window or wrong intent).

**CRAG scoring for the 9 retrieved documents:**

| Document | CRAG score | Category | Decision |
|---|---|---|---|
| ICM-2847391 incident summary (Spark OOM, active) | 0.94 | CORRECT | Include |
| ICM-2849012 incident summary (Kusto throttling, active) | 0.91 | CORRECT | Include |
| Pipeline failure log: spark_etl_daily_ingest 10:52 AM | 0.89 | CORRECT | Include |
| Pipeline failure log: kusto_ingestion_telemetry 11:01 AM | 0.87 | CORRECT | Include |
| SLA breach record: kusto_ingestion_telemetry 11:00 AM | 0.83 | CORRECT | Include |
| Pipeline failure log: spark_bronze_to_silver_events 11:03 AM | 0.79 | CORRECT | Include |
| Pipeline failure log: synapse_pipeline_customer360 11:15 AM | 0.76 | CORRECT | Include |
| ICM-2831204 incident (Spark OOM, resolved, Feb 22) | 0.31 | AMBIGUOUS | Discard |
| Confluence runbook: "Kusto ThrottlingException resolution guide" | 0.28 | INCORRECT | Discard |

**Why the 8th and 9th documents are discarded:**

- **ICM-2831204 (resolved, Feb 22):** This document matched BM25 on "OutOfMemoryError" and "spark_etl_daily_ingest" but is from 14 days ago and is in RESOLVED status. CRAG's evaluation prompt detects this mismatch: the query asks for the current state ("last hour"), and this document describes a past incident. Score 0.31 — below the AMBIGUOUS threshold of 0.40. Discarded.

- **Confluence runbook (Kusto throttling resolution):** This document was retrieved because the query enrichment injected "ThrottlingException" and the vector similarity to the hypothetical document was moderate. But the document answers "how do I fix Kusto throttling?" not "what's currently broken." CRAG's evaluation prompt identifies this intent mismatch. Score 0.28 — INCORRECT. Discarded.

**CRAG evaluation prompt (internal, per document):**

```
System: You are a relevance evaluator for an incident management assistant.
Score this retrieved document's relevance to the user's query.

User query: "what's broken in the last hour?"
Query intent: STATUS_QUERY — the user wants to know the CURRENT STATE of the system,
              not how to fix problems or what happened historically.

Document: [document text]

Score from 0.0 to 1.0:
- 1.0 = directly describes the current broken state (active incident, recent failure, current SLA breach)
- 0.5 = related but indirect (historical similar incident, general monitoring alert)
- 0.0 = describes how to fix problems, past incidents, or unrelated systems

Return JSON: {"score": 0.XX, "category": "CORRECT|AMBIGUOUS|INCORRECT", "reason": "..."}
```

**CRAG output: 7 CORRECT chunks.** These are the canonical 7 chunks that must be identical for all five equivalent phrasings.

---

### Technique 10: GraphRAG

**What it is:** A knowledge graph layer that augments retrieved chunks with relationship context — upstream dependencies, downstream impacts, and shared infrastructure ownership. For a status query, this provides the prioritization context the engineer needs: not just "what's broken" but "what impact does each broken thing have?"

**Graph nodes activated by the 7 CRAG-approved chunks:**

```
spark_etl_daily_ingest (FAILED)
├── UPSTREAM: adls_raw_zone (operational — not the cause)
├── WRITES_TO: delta_curated_events (stale since 10:52 AM — 38 minutes stale)
└── DOWNSTREAM (2 nodes BLOCKED):
    ├── spark_bronze_to_silver_events (FAILED — dependency blocked)
    └── synapse_pipeline_customer360 (FAILED — timeout waiting for delta_curated_events)
        └── DOWNSTREAM:
            └── synapse_dataflow_daily_summary (FAILED — 2 hops from root cause)

kusto_ingestion_telemetry (THROTTLED + SLA BREACH)
├── UPSTREAM: None (standalone ingestion pipeline — no dependencies)
└── DOWNSTREAM: None
    (Impact: TelemetryEvents table stale — 30 minutes overdue on SLA)
```

**GraphRAG blast radius computation:**

```python
blast_radius = {
    "spark_etl_daily_ingest": {
        "direct_downstream_failures": 2,   # spark_bronze_to_silver + synapse_customer360
        "indirect_downstream_failures": 1, # synapse_daily_summary (2 hops)
        "stale_delta_table": "delta_curated_events",
        "staleness_minutes": 38,
        "total_impacted_pipelines": 3,
        "priority": "HIGH",
    },
    "kusto_ingestion_telemetry": {
        "direct_downstream_failures": 0,
        "indirect_downstream_failures": 0,
        "sla_breach": True,
        "breach_minutes": 30,
        "total_impacted_pipelines": 0,
        "priority": "MEDIUM",   # SLA breach, but no downstream cascade
    },
}
```

**GraphRAG output injected into context:**

```
BLAST RADIUS CONTEXT:
- spark_etl_daily_ingest: ROOT CAUSE for 3 additional pipeline failures.
  Fix this first. 38 minutes of stale delta table data.
- kusto_ingestion_telemetry: ISOLATED failure. SLA breached by 30 minutes.
  No downstream impact, but SLA violation requires notification.
```

This prioritization context is what differentiates Alert Whisperer from a simple query tool. The engineer does not just get a list of broken things — they get a prioritized remediation order derived from the dependency graph. The answer to "what's broken" becomes not just a list, but a triage roadmap.

---

## Phase 4: Generation

*The generation phase synthesizes the 7 retrieved chunks + GraphRAG context into a coherent, structured response. Two techniques govern generation quality: FLARE ensures the LLM doesn't hallucinate when uncertain, and Self-RAG verifies that every factual claim in the response is grounded in retrieved evidence.*

---

### Technique 11: Cross-Encoder Re-ranking

**What it is:** A computationally expensive but highly accurate relevance model that takes each (query, document) pair and scores them jointly — unlike bi-encoder vector search, which scores query and document independently. Cross-encoder scores are the final ranking signal before generation.

**Input:** 7 CRAG-approved chunks.

**Scoring dimension — STATUS RELEVANCE:**

The cross-encoder is evaluated against the intent-aware query: "Current status of active incidents and recent pipeline failures." This intent enrichment is critical — without it, the cross-encoder would rank the Spark OOM runbook content (from the parent chunk) above the incident summary, because the runbook text is more semantically dense for OOM queries.

**Cross-encoder scores:**

| Rank | Document | Score | Reasoning |
|---|---|---|---|
| 1 | ICM-2847391 incident summary (Spark OOM) | 0.92 | Directly answers "what's broken" — active incident, current status |
| 2 | ICM-2849012 incident summary (Kusto throttling) | 0.89 | Same — second active incident |
| 3 | Pipeline failure log: spark_etl_daily_ingest 10:52 AM | 0.88 | Directly answers "in the last hour" — specific failure record |
| 4 | SLA breach record: kusto_ingestion_telemetry 11:00 AM | 0.85 | Answers SLA dimension of "what's broken" |
| 5 | Pipeline failure log: kusto_ingestion_telemetry 11:01 AM | 0.84 | Supports SLA breach context |
| 6 | Pipeline failure log: spark_bronze_to_silver_events 11:03 AM | 0.81 | Downstream failure chain context |
| 7 | Pipeline failure log: synapse_pipeline_customer360 11:15 AM | 0.78 | Downstream failure chain context |

Note the Spark OOM runbook (from parent-child context) would score ~0.45 — "relevant to a broken pipeline but doesn't answer 'what's broken right now'." It is excluded from the generation context because it answered the wrong query intent. This is the cross-encoder correctly discriminating between relevance to the topic (OOM) versus relevance to the intent (current status).

**Final 7 chunks in rank order are passed to generation.**

---

### Technique 12: FLARE (Forward-Looking Active Retrieval)

**What it is:** During generation, FLARE monitors the LLM's own output tokens for low-confidence hedging language. When detected, it pauses generation, issues a targeted retrieval query for the missing information, and injects the retrieved fact before continuing.

**Impact for this scenario: MINIMAL.**

FLARE is most useful for diagnostic narratives (Scenario 1: "The skew is likely caused by…") where the LLM is generating inference-heavy reasoning. Here, the response is a structured enumeration:

```
- Active incident 1: [retrieved data]
- Active incident 2: [retrieved data]
- Failed pipelines: [retrieved data list]
- SLA breach: [retrieved data]
```

All facts are directly pulled from retrieved chunks. There is no inference gap that FLARE needs to fill.

**FLARE trigger scan results:**

```python
flare_triggers_found = 0
hedging_tokens_detected = []
# No tokens like "probably", "might be", "I believe", "approximately" 
# appear in the structured response — all values are retrieved facts.
```

FLARE fires zero times. The response is factually complete from the retrieved context.

---

### Technique 13: Self-RAG (Self-Reflective Retrieval-Augmented Generation)

**What it is:** After generating the response, Self-RAG evaluates each factual claim against the retrieved evidence, labeling each claim as [Supported], [Partially Supported], [Not Supported], or [Inference]. Claims scored as Not Supported are flagged with warnings.

**Self-RAG evaluation of the final response:**

| Claim | Evidence source | Self-RAG label | Confidence |
|---|---|---|---|
| "2 active ICM incidents" | ICM-2847391 + ICM-2849012 summaries | [Supported] | 0.97 |
| "5 failed pipeline runs in the last hour" | 5 Log Analytics / Kusto failure records | [Supported] | 0.95 |
| "spark_etl_daily_ingest: OutOfMemoryError at 10:52 AM" | ICM-2847391 + pipeline failure log | [Supported] | 0.98 |
| "kusto_ingestion_telemetry: ThrottlingException, SLA breached 30 minutes" | ICM-2849012 + SLA breach record | [Supported] | 0.96 |
| "3 downstream pipelines impacted by Spark OOM" | GraphRAG blast radius computation | [Supported via GraphRAG] | 0.91 |
| "Fix spark_etl_daily_ingest first (higher blast radius)" | GraphRAG prioritization output | [Supported via GraphRAG] | 0.88 |

All 6 claims are supported. Self-RAG issues no warnings. The response is finalized with an overall confidence of 0.94.

---

## Data Consistency Deep Dive

This section demonstrates, step by step, how the consistency guarantee is implemented and why it holds across all five equivalent phrasings.

### The Problem

Natural language is ambiguous. Engineers ask the same question in dozens of different ways depending on their communication style, vocabulary, and mood. A system that returns different results for "show me recent issues" versus "any incidents in the last hour?" is not a reliable diagnostic tool — engineers cannot trust it, and they will stop using it.

The system owner's requirement is explicit and non-negotiable:

> *"Equivalent questions must return the same results regardless of phrasing: 'issues' = 'incidents' = 'What's broken?'"*

### The Consistency Mechanism

Four components work in combination to achieve this guarantee:

**Component 1: Synonym Map Normalization**

```python
SYNONYM_MAP = {
    "broken":    ["job failed", "pipeline failure", "activity error", "run failed", "execution error"],
    "issues":    ["job failed", "pipeline failure", "activity error", "run failed", "execution error"],
    "incidents": ["job failed", "pipeline failure", "activity error", "run failed", "execution error"],
    "failed":    ["job failed", "pipeline failure", "activity error", "run failed", "execution error"],
    "problems":  ["job failed", "pipeline failure", "activity error", "run failed", "execution error"],
}
```

Every status synonym maps to the **same canonical expansion string**: `"job failed pipeline failure activity error run failed execution error"`. This is the single most important consistency mechanism. After synonym expansion, all five phrasings produce an identical V2 variant.

**Component 2: LLM Rewriting Normalization**

GPT-4o-mini is given the same system prompt and the same active alerts context for all five phrasings. Because the expanded query is already near-identical (Component 1 ensures this), and because the LLM prompt explicitly instructs multi-faceted decomposition, the LLM-rewritten V4 variant converges to the same structured query regardless of the original phrasing.

The LLM prompt includes a normalization instruction:

```
System: Rewrite the query to cover ALL interpretations of a status question:
active incidents, failed pipeline runs, error spikes, SLA breaches.
Use only the active alerts context, not the query's surface form, to derive specifics.
```

By anchoring the rewrite to the active alerts context (system state) rather than the query surface form, the LLM output is de-coupled from phrasing variation.

**Component 3: Context Enrichment Invariance**

The active alerts context (`ICM-2847391: OutOfMemoryError`, `ICM-2849012: ThrottlingException`) is system state — it is the same for all five phrasings submitted in the same time window. The context enrichment injection is therefore identical for all five queries.

**Component 4: CRAG Stability**

After synonym expansion + LLM rewriting + context enrichment converge the five queries to the same retrieval signal, CRAG applies the same scoring to the same retrieved chunks. The CRAG evaluation is deterministic for identical input, so the 7-chunk output is stable.

### Consistency Proof Table

| Query | V2 Synonym Expanded | V3 Context Enriched | V4 LLM Rewritten | CRAG Output |
|---|---|---|---|---|
| "what's broken in the last hour?" | `job failed pipeline failure activity error run failed execution error last 60 minutes` | + `pipeline:spark_etl_daily_ingest error:OutOfMemoryError pipeline:kusto_ingestion_telemetry error:ThrottlingException` | "List all active ICM incidents, failed pipeline runs..." | Same 7 chunks |
| "show me recent issues" | `job failed pipeline failure activity error run failed execution error last 6 hours` | + `pipeline:spark_etl_daily_ingest error:OutOfMemoryError pipeline:kusto_ingestion_telemetry error:ThrottlingException` | "List all active ICM incidents, failed pipeline runs..." | Same 7 chunks |
| "any incidents in the last 60 minutes?" | `job failed pipeline failure activity error run failed execution error last 60 minutes` | + `pipeline:spark_etl_daily_ingest error:OutOfMemoryError pipeline:kusto_ingestion_telemetry error:ThrottlingException` | "List all active ICM incidents, failed pipeline runs..." | Same 7 chunks |
| "what failed recently?" | `job failed pipeline failure activity error run failed execution error last 6 hours` | + `pipeline:spark_etl_daily_ingest error:OutOfMemoryError pipeline:kusto_ingestion_telemetry error:ThrottlingException` | "List all active ICM incidents, failed pipeline runs..." | Same 7 chunks |
| "current problems" | `job failed pipeline failure activity error run failed execution error active ongoing` | + `pipeline:spark_etl_daily_ingest error:OutOfMemoryError pipeline:kusto_ingestion_telemetry error:ThrottlingException` | "List all active ICM incidents, failed pipeline runs..." | Same 7 chunks |

**Minor variation and why it does not matter:**

"Show me recent issues" and "what failed recently?" use `"recently"` which maps to a 6-hour window rather than 1-hour. However, the same 7 core chunks appear in the 6-hour window — the 5 failed pipeline runs all occurred within the last hour, so they are retrieved in both windows. The broader window may retrieve additional older records, but CRAG scores them as AMBIGUOUS and discards them. The 7-chunk CRAG output is identical.

### Edge Case: Time Window Variation

The time range `"last 90 days"` is the upper bound of the system owner's stated requirement:

> *"I should be able to get the issues / incidents and all the details when I post a question in the chat — could be for an hour or in the last 90 days etc kind of scenarios."*

For the same five phrasings with a 90-day window:
- Synonym expansion is identical (time synonyms expand "recently" → 6 hours or 90 days based on context, but the canonical query form is consistent)
- Context enrichment is identical (active alerts are current state)
- LLM rewriting adjusts the time filter parameter ("last 90 days" instead of "last 60 minutes")
- Agentic RAG passes `time_range: "90d"` to the ICM and Log Analytics MCP calls
- A wider result set is returned, but the currently active incidents still appear at the top

The consistency guarantee is preserved at the phrasing level. Different time windows naturally return different data (more historical incidents over 90 days), but equivalent phrasings with the same time window return the same data.

---

## Time Range Handling

Alert Whisperer's time range extraction is a first-class feature of the query pipeline, not an afterthought. The engineer's natural language time expression must be reliably converted to a precise timestamp filter passed to every data source.

### Extraction Patterns

```python
TIME_RANGE_PATTERNS = {
    r"last hour|last 60 minutes|past hour|in the last hour": {
        "window": "1h",
        "from_offset": "-60m",
        "label": "last_60_minutes",
    },
    r"last (\d+) minutes": {
        "window": "{n}m",
        "from_offset": "-{n}m",
        "label": "last_{n}_minutes",
    },
    r"last (\d+) hours": {
        "window": "{n}h",
        "from_offset": "-{n}h",
        "label": "last_{n}_hours",
    },
    r"last (\d+) days": {
        "window": "{n}d",
        "from_offset": "-{n}d",
        "label": "last_{n}_days",
    },
    r"last 90 days|past 90 days": {
        "window": "90d",
        "from_offset": "-90d",
        "label": "last_90_days",
    },
    r"today|this morning|since midnight": {
        "window": "today",
        "from_offset": "midnight_local_tz",
        "label": "today",
    },
    r"recently|recent": {
        "window": "6h",
        "from_offset": "-6h",
        "label": "recent_default",
        "note": "Default fallback: 6 hours when no time window specified",
    },
    r"currently|now|active|current": {
        "window": "open_incidents",
        "from_offset": None,
        "label": "current_state",
        "note": "No time filter — retrieve by status=Active rather than time window",
    },
}
```

### Propagation to MCP Calls

The extracted time window is passed as a typed parameter to every MCP server call in the Agentic RAG plan:

```python
# ICM MCP query with time filter
icm_query = {
    "endpoint": "/incidents/search",
    "params": {
        "opened_after": "2026-03-08T10:30:00Z",   # computed from "last hour" at 11:30 AM CDT
        "opened_before": "2026-03-08T11:30:00Z",
        "status": ["Active", "Acknowledged"],
    }
}

# Log Analytics MCP query with KQL time filter
log_analytics_query = {
    "kql": "... | where TimeGenerated >= ago(1h) | ...",
    "time_range": {
        "start": "2026-03-08T10:30:00Z",
        "end": "2026-03-08T11:30:00Z",
    }
}
```

### Time Zone Awareness

The engineer's query is processed in the context of their local timezone (CDT, UTC-5). The system resolves "11:30 AM CDT" to "2026-03-08T16:30:00Z" before constructing MCP query parameters. All stored incident data uses UTC, and all MCP queries use UTC parameters. The display layer converts back to local time for the engineer's response.

For "today since midnight": midnight CDT = 05:00 AM UTC. The system uses the engineer's profile timezone (set during onboarding) or detects it from the browser session.

---

## Execution Trace (Millisecond-Level)

The complete pipeline execution for "what's broken in the last hour?" at 11:30 AM CDT:

```
T+0ms     Query received: "what's broken in the last hour?"
T+2ms     [T1] Topic Tree classification: STATUS_QUERY, root node match (0.61)
           topic_filter_tags = [], classification_mode = "STATUS_QUERY"
T+4ms     [T2a] Synonym expansion: "broken" → canonical status tokens
T+6ms     [T2b] Abbreviation expansion: none found
T+8ms     [T2c] Context enrichment: active alerts injected (ICM-2847391, ICM-2849012)
T+45ms    [T2d] GPT-4o-mini LLM rewriting: V4 generated (39ms call)
T+48ms    [T3] HyDE: hypothetical status document generated (3ms, cached model)
T+55ms    [T6] Agentic RAG: plan constructed — ICM, Log Analytics, Kusto; Confluence skipped
T+58ms    [T4] RAG Fusion: 3 sub-queries decomposed, retrieval dispatched in parallel
T+60ms    [T5] Azure AI Search hybrid: V2 + V3 + V4 + HyDE vector submitted
          ├── BM25: matches on "OutOfMemoryError", "ThrottlingException", "pipeline failure"
          └── Vector: HyDE embedding closest to ICM incident summaries
T+180ms   [T6] ICM MCP returns: 2 active incidents (125ms MCP call latency)
T+210ms   [T6] Log Analytics MCP returns: 5 pipeline failure records (150ms MCP call latency)
T+220ms   [T6] Kusto MCP returns: 5 failure records (confirmation) (160ms MCP call latency)
T+225ms   [T4+T5] Azure AI Search hybrid results merged with RAG Fusion RRF
           → 9 candidate documents
T+230ms   [T7] Parent-Child: ICM-2847391, ICM-2849012 parent chunks loaded
T+235ms   [T8] Semantic Chunking: confirmed — incident chunks are intact (indexing-time benefit)
T+280ms   [T9] CRAG: 9 documents evaluated, 7 CORRECT, 2 discarded (45ms evaluation)
T+290ms   [T10] GraphRAG: blast radius computed for both active incidents
           spark_etl_daily_ingest: 3 downstream impacted; kusto: 0 downstream
T+340ms   [T11] Cross-Encoder: 7 chunks re-ranked by status relevance (50ms scoring)
T+345ms   Context window assembled: 7 chunks + GraphRAG context
T+1.8s    [T12] Generation begins: GPT-4o structured status summary
T+1.8s    [T12] FLARE scan: 0 hedging tokens detected, no retrieval triggered
T+2.4s    [T13] Generation complete: 6 factual claims identified
T+2.5s    [T13] Self-RAG: all 6 claims validated as [Supported], confidence 0.94
T+2.5s    Response displayed: confidence 0.94, 2.5s total latency
```

**Total latency: ~2.5 seconds** from user input to complete response.

The 2.5 second latency is notably faster than Scenario 3 (cascading failure with complex graph traversal). MCP calls to ICM and Log Analytics run in parallel, and CRAG evaluation is simpler with fewer candidates.

---

## Full Response Delivered to Engineer

The following is the complete response Alert Whisperer delivers to the engineer at T+2.5s:

---

> **Alert Whisperer** *(confidence: 0.94 | 11:30 AM CDT)*
>
> **Current Status: 2 Active Incidents, 5 Pipeline Failures in the Last Hour**
>
> ---
>
> **ACTIVE INCIDENTS**
>
> 🔴 **[ICM-2847391]** `spark_etl_daily_ingest` — **FAILED** (Severity 2)
> - **Error:** `java.lang.OutOfMemoryError: Java heap space`
> - **Since:** 10:52 AM CDT (38 minutes ago)
> - **Blast radius: HIGH** — 3 additional pipelines are blocked downstream
> - Owner: data-platform-team@contoso.com
>
> 🟡 **[ICM-2849012]** `kusto_ingestion_telemetry` — **THROTTLED + SLA BREACH** (Severity 3)
> - **Error:** `ThrottlingException — ingestion rate exceeded cluster capacity`
> - **Since:** 11:01 AM CDT (29 minutes ago)
> - **SLA BREACHED:** Deadline was 11:00 AM CDT — now 30 minutes overdue
> - **Blast radius: LOW** — no downstream dependencies, but SLA breach requires notification
> - Owner: data-platform-team@contoso.com
>
> ---
>
> **FAILED PIPELINE RUNS (last 60 minutes)**
>
> | Pipeline | Time | Status | Cause |
> |---|---|---|---|
> | `spark_etl_daily_ingest` | 10:52 AM | FAILED | OutOfMemoryError (root cause) |
> | `kusto_ingestion_telemetry` | 11:01 AM | FAILED | ThrottlingException (independent) |
> | `spark_bronze_to_silver_events` | 11:03 AM | FAILED | Blocked — dependency on spark_etl_daily_ingest |
> | `synapse_pipeline_customer360` | 11:15 AM | FAILED | Timeout — delta_curated_events table stale |
> | `synapse_dataflow_daily_summary` | 11:18 AM | FAILED | Blocked — dependency on synapse_pipeline_customer360 |
>
> ---
>
> **RECOMMENDED TRIAGE ORDER**
>
> 1. **Fix `spark_etl_daily_ingest` first** — resolving this unblocks 3 pipelines automatically once `delta_curated_events` is refreshed.
> 2. **Escalate `kusto_ingestion_telemetry` SLA breach** — notify SLA stakeholders; the breach is already 30 minutes old. This pipeline requires independent remediation (Kusto throttling is unrelated to the Spark OOM).
>
> ---
>
> *Want details on either incident? Ask: "show me the runbook for the Spark OOM" or "how do I fix Kusto throttling?"*

---

*Note: The downstream impact cascade (3 pipelines blocked by spark_etl_daily_ingest) was derived from the dependency graph — not directly from the ICM incident record.*

---

## Technique Impact Comparison

| Technique | Impact | Why |
|---|---|---|
| **Query Rewriting** | ★★★★★ | Transforms a 9-word vague question into a structured multi-faceted retrieval signal. Without it, BM25 is near-useless and vector search returns generic troubleshooting docs rather than status summaries. Directly enables data consistency through the canonical synonym map. |
| **Agentic RAG** | ★★★★★ | Correctly routes to ICM + Log Analytics + Kusto. Critically skips Confluence (no runbook lookup for a status query). Extracts the time window and passes it as a filter parameter to every MCP call. The `STATUS_QUERY` flag from Topic Tree informs this routing decision. |
| **CRAG** | ★★★★☆ | Filters out the 2 irrelevant documents (old resolved incident, Kusto runbook). Ensures the generation context contains only status-oriented content. Without CRAG, the response would include "how to fix Kusto throttling" information that the engineer didn't ask for. |
| **GraphRAG** | ★★★★☆ | Transforms a flat list of broken things into a prioritized triage plan. The blast radius computation (3 downstream pipelines blocked by Spark OOM vs. 0 for Kusto throttling) is what enables the "fix this first" recommendation. This context is not in any single retrieved document. |
| **Cross-Encoder** | ★★★★☆ | Correctly promotes status-oriented result chunks over troubleshooting-oriented content from parent chunks. Ranks ICM incident summaries above Spark OOM runbook steps even though both are "about" the OOM incident. |
| **RAG Fusion** | ★★★★☆ | Correctly decomposes "what's broken" into 3 facets (ICM incidents, pipeline failures, SLA breaches). Without RAG Fusion, a single query might miss the SLA breach dimension entirely. The 3-facet decomposition ensures completeness. |
| **HyDE** | ★★★☆☆ | The hypothetical status report document bridges the gap between the sparse original query and the dense status summary documents in the KB. Useful but not critical — the LLM-rewritten query achieves similar coverage. |
| **Self-RAG** | ★★★☆☆ | Validates all 6 factual claims as [Supported]. Provides the confidence score (0.94). Would surface a warning if the GraphRAG blast radius computation made an unsupported claim. |
| **Semantic Chunking** | ★★★☆☆ | Ensures ICM incident summaries are retrieved as complete, coherent units (Title + Description + Impact Assessment together). Prevents the retrieved chunk from containing the error description but not the downstream impact. |
| **Topic Tree** | ★★☆☆☆ | Correctly identifies `STATUS_QUERY` mode and sets `topic_filter_tags = []`. Gracefully degrades rather than forcing an incorrect branch classification. The `STATUS_QUERY` flag it sets is used by Agentic RAG for routing. |
| **Parent-Child Chunking** | ★★☆☆☆ | Loads the full ICM incident records (including blast radius notes) when child title chunks are retrieved. Less impactful here than in troubleshooting scenarios — the engineer needs breadth, not depth. |
| **FLARE** | ★★☆☆☆ | Fires zero times. The structured enumeration response has no inference gaps. FLARE is designed for diagnostic narratives, not status reports. It correctly does nothing here. |
| **Azure AI Search Hybrid** | ★★★★☆ (with rewriting) / ★★☆☆☆ (without) | BM25 is very weak on the original "what's broken" query but becomes strong once Query Rewriting injects specific error tokens. Hybrid scoring's value here is entirely dependent on Query Rewriting quality. |

### Winner for This Scenario

**Query Rewriting + Agentic RAG + CRAG**, with GraphRAG as the critical value-add.

- **Query Rewriting** makes the ambiguous query actionable by reconstructing its multi-faceted intent and normalizing all equivalent phrasings to the same canonical retrieval signal.
- **Agentic RAG** routes to the right sources (ICM, Log Analytics, Kusto) with the right time filter, and deliberately skips Confluence.
- **CRAG** removes the noise (old incidents, runbooks) that the broad retrieval necessarily brings in.
- **GraphRAG** turns the list of broken things into a prioritized remediation plan with blast radius context.

Without Query Rewriting, the system would return vague results and data consistency would fail. Without Agentic RAG, the system would not reach live ICM data. Without CRAG, the response would include irrelevant troubleshooting content. Without GraphRAG, the engineer would not know which incident to fix first.

---

## Contrast with Scenario 1

| Dimension | Scenario 1 (Spark OOM — Specific Error) | Scenario 4 (Freeform Status Query) |
|---|---|---|
| **Query specificity** | High — error code + pipeline name | Zero — no error, no pipeline, no system |
| **Topic Tree** | High impact (routes to exact leaf node) | Low impact (graceful degradation to STATUS_QUERY mode) |
| **Query Rewriting** | Moderate impact (expands abbreviations, adds technical synonyms) | Critical impact (reconstructs missing intent from scratch) |
| **HyDE** | Useful (hypothetical runbook retrieval) | Useful (hypothetical status report retrieval) |
| **RAG Fusion** | 3 sub-queries by error facet | 3 sub-queries by information type (ICM, pipeline, SLA) |
| **Agentic RAG** | Routes to Confluence (runbook needed) | Routes to ICM + Log Analytics (runbooks NOT needed) |
| **CRAG** | Filters out non-Spark content | Filters out troubleshooting content (wrong intent) |
| **GraphRAG** | High impact (blast radius planning) | High impact (prioritization of multiple broken things) |
| **FLARE** | High impact (fills inference gaps in diagnostic narrative) | Minimal impact (enumeration response needs no inference) |
| **Data consistency challenge** | Low (specific query has low phrasing variation) | High (vague queries have maximum phrasing variation) |
| **Primary failure mode without techniques** | Wrong runbook retrieved | Vague, inconsistent, incomplete status summary |

---

## Implementation Notes for Alert Whisperer

**Synonym map maintenance:**

The synonym map must be treated as a first-class system configuration, not a hardcoded constant. As the organization adds new terminology (e.g., "outages" becomes common in incident reports), the synonym map must be updated to include the new term mapping to the canonical status token set. A `synonyms.yaml` configuration file with a hot-reload mechanism (polling every 60 seconds) allows updates without service restart.

**STATUS_QUERY detection heuristics:**

The heuristic for detecting a status query (no error token present + status vocabulary present) should be expanded over time based on observed query patterns. Early detection allows the correct routing decision (broad retrieval, Confluence skipped) to be made before the expensive LLM rewriting step.

**MCP time filter propagation:**

Every MCP server query that accepts a time filter must receive the extracted time window as a typed parameter, not as a natural language string. The time extraction step must run before the Agentic RAG planning step so that the plan can be constructed with concrete parameter values.

**Context window size for multi-source status queries:**

The 7-chunk context for this scenario totals approximately 1,800 tokens (ICM summaries + pipeline failure logs + SLA breach record + GraphRAG blast radius context). This is well within the GPT-4o context window. For "last 90 days" queries returning many more incidents, the CRAG step must be tuned more aggressively (raising the CORRECT threshold) to prevent context overflow.

**Confidence calibration for status responses:**

Self-RAG confidence of 0.94 reflects that all claims are directly supported by retrieved data. The GraphRAG-derived claims (blast radius, prioritization recommendation) are labeled [Supported via GraphRAG] rather than [Supported], and the slightly lower confidence (0.88–0.91) reflects that graph traversal introduces a single additional inference step. This labeling provides auditability: an engineer reviewing a status response can see which claims came from live data versus graph computation.

---

*This document is part of the Alert Whisperer architecture reference. See `/docs/ARCHITECTURE.md` for system design and the other scenario files in `/docs/scenarios/` for individual technique deep-dives in different query contexts.*
