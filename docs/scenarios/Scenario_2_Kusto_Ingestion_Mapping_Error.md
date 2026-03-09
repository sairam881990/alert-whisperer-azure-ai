# Scenario 2 — Kusto Ingestion Mapping Error (BM25-Dominant Path)

> **Audience:** Senior Data Engineer building Alert Whisperer — a conversational troubleshooting assistant backed by Azure AI Search, `text-embedding-3-large` (3072 dims), and 13 RAG techniques for Spark/Synapse/Kusto incident response.
>
> **Purpose:** A complete, concrete walkthrough of every technique firing in sequence on a Kusto ingestion mapping error. This scenario is the canonical example of **BM25 dominance** — where exact lexical matching outperforms semantic vector search because the error contains specific, non-decomposable identifiers that embedding models cannot meaningfully distinguish.

---

## Scenario Setup

| Field | Value |
|---|---|
| **Pipeline** | `kusto_ingestion_telemetry` on Azure Data Explorer (ADX) |
| **Error Code** | `Permanent_MappingNotFound` |
| **Error Message** | `Ingestion mapping 'telemetry_json_mapping_v2' was not found. EntityName='TelemetryEvents'` |
| **Data Source** | Azure Event Hub → ADX ingestion pipeline |
| **Target Table** | `TelemetryEvents` (cluster: `adx-prod-eastus2.eastus2.kusto.windows.net`) |
| **Detection Time** | 09:12 AM CDT via ICM alert (ICM-3091847) |
| **Root Cause** | Schema migration changed column types in `TelemetryEvents` but the ingestion mapping definition `telemetry_json_mapping_v2` was not updated or recreated |
| **Impact** | All Event Hub messages silently dropped; no new telemetry rows appearing in `TelemetryEvents` for 47 minutes before alerting |
| **Triggering Query** | `"kusto mapping not found telemetry_json_mapping_v2"` |

**Raw ICM Alert Payload:**
```
ALERT: kusto_ingestion_telemetry FAILED
Time: 2026-03-08T14:12:03Z
Severity: 2
Component: ADX Ingestion
Message: Permanent_MappingNotFound: Ingestion mapping 'telemetry_json_mapping_v2' was not found.
  EntityName='TelemetryEvents'
  Details: Ingestion source=EventHub, Database=TelemetryDB, Table=TelemetryEvents
  FailureKind=Permanent
  OperationId=8f3a1c9e-2b4d-4f87-ac12-1e6b3d7f4a90
Pipeline owner: platform-infra-team@contoso.com
Consecutive failures: 47
```

**Background — What broke and why:**

Two days before this alert, a data engineer on the platform-infra team ran a schema migration on the `TelemetryEvents` table to add a `SessionDurationMs` column (int64) and change `EventPayload` from `string` to `dynamic` type. The migration statement:

```kql
.alter table TelemetryEvents (
    EventId:       guid,
    EventTimestamp: datetime,
    UserId:        string,
    EventType:     string,
    EventPayload:  dynamic,      -- changed from string
    SessionDurationMs: long,     -- new column
    Region:        string,
    AppVersion:    string
)
```

The column type change for `EventPayload` invalidated the existing ingestion mapping `telemetry_json_mapping_v2`, which contained a hard-coded type reference:

```json
{
  "MappingName": "telemetry_json_mapping_v2",
  "MappingKind": "Json",
  "Properties": [
    { "column": "EventId",         "path": "$.event_id",         "datatype": "guid"     },
    { "column": "EventTimestamp",  "path": "$.timestamp",        "datatype": "datetime" },
    { "column": "UserId",          "path": "$.user_id",          "datatype": "string"   },
    { "column": "EventType",       "path": "$.event_type",       "datatype": "string"   },
    { "column": "EventPayload",    "path": "$.payload",          "datatype": "string"   },  ← stale type
    { "column": "Region",          "path": "$.region",           "datatype": "string"   },
    { "column": "AppVersion",      "path": "$.app_version",      "datatype": "string"   }
    // SessionDurationMs mapping MISSING entirely
  ]
}
```

The engineer deleted the old mapping definition as part of the cleanup but never created a new one. The Event Hub data connection was referencing `telemetry_json_mapping_v2` by name, and ADX returned `Permanent_MappingNotFound` on every ingestion attempt.

**The support engineer's opening message to Alert Whisperer:**

> **User:** "kusto mapping not found telemetry_json_mapping_v2"

What follows is the complete trace of all 13 techniques processing this message — and an analysis of **why BM25 is the decisive factor** in surfacing the correct resolution.

---

## Why BM25 Dominates This Scenario

Before the technique walkthrough, it is worth understanding the fundamental retrieval challenge this error creates.

### The Lexical Specificity Problem

The error string `"telemetry_json_mapping_v2"` is a **proper noun in the Kusto namespace** — a specific configuration object name. It has three properties that make it ideal for BM25 and treacherous for embedding-based retrieval:

1. **Non-decomposable token:** The embedding model `text-embedding-3-large` sees `telemetry_json_mapping_v2` as a sequence of subword tokens: `["tele", "metry", "_json", "_map", "ping", "_v2"]`. None of these subwords carry semantic meaning that distinguishes *this specific mapping object* from any other JSON ingestion mapping in the knowledge base.

2. **High IDF weight in BM25:** The exact string `telemetry_json_mapping_v2` appears in exactly two documents in the knowledge base: the mapping creation runbook and the past ICM incident that documented this same error six months ago. BM25's Inverse Document Frequency (IDF) component assigns this term a weight of `log((12,000 - 2 + 0.5) / (2 + 0.5)) ≈ 8.4` — among the highest in the entire corpus. This means BM25 essentially treats it as a near-unique fingerprint.

3. **Semantic collision problem:** An embedding-based search for "kusto mapping not found" will assign similar cosine similarity scores to *any* document about Kusto ingestion configuration — including the generic ADX onboarding guide, the `telemetry_json_mapping_v1` runbook (a predecessor mapping), and a separate mapping error on a completely different table (`UserActivityEvents`). Embeddings cannot distinguish between these documents because the semantic *concept* is identical; only the specific *identifier* differs.

### Score Prediction (Before Detailed Walkthrough)

| Document | BM25 Score | Vector Score | Hybrid Score | Rank |
|---|---|---|---|---|
| Doc A — `telemetry_json_mapping_v2` creation runbook | **0.92** | 0.71 | **0.79** | **#1** |
| Doc B — Generic Kusto JSON ingestion guide | 0.45 | **0.78** | 0.65 | #3 |
| Doc C — `user_activity_mapping_v3` error incident | 0.68 | 0.74 | 0.72 | #2 |
| Doc D — ADX schema migration guide | 0.31 | 0.69 | 0.54 | #5 |
| Doc E — Spark OOM (false positive via embedding drift) | 0.04 | 0.61 | 0.38 | #8 |

Doc A wins because BM25 catches the exact mapping name. Without BM25, Doc B would be ranked #1 — a generic guide that explains Kusto ingestion in general terms but contains zero information about `telemetry_json_mapping_v2` specifically.

---

## Phase 1: Pre-Retrieval

*Before a single document is fetched, three techniques work together to transform the raw error message into a rich, multi-perspective set of retrieval signals.*

---

### Technique 1: Topic Tree Classification

**What it is:** A hierarchical taxonomy of error categories maintained as a lightweight in-memory tree. Every incoming query is routed to one or more leaf nodes before retrieval begins. This scopes all subsequent retrieval to a relevant subset of the knowledge base.

**Input:**
```
"kusto mapping not found telemetry_json_mapping_v2"
```

**Topic tree structure (full relevant branch):**
```
Infrastructure Errors
└── Data Pipeline Errors
    ├── Spark Errors
    │   ├── OutOfMemory
    │   ├── ShuffleFetchFailure
    │   └── StageFailure
    ├── Synapse Errors
    │   ├── DWU Exhaustion
    │   └── PolyBase Failures
    └── Kusto Errors                           ← PRIMARY BRANCH (score: 0.94)
        ├── Throttling
        ├── IngestionFailure                   ← PRIMARY MATCH (score: 0.91)
        │   ├── MappingNotFound                ← EXACT LEAF MATCH (score: 0.97)
        │   ├── SchemaViolation
        │   ├── FormatException
        │   └── EventHubConnectionError
        └── QueryFailure
            ├── PartialQueryFailure
            └── QueryThrottling
```

**Classification logic:**

- Keyword match: `"kusto"` → `Kusto Errors` branch (exact node keyword, score boost +0.4)
- Keyword match: `"mapping not found"` → `IngestionFailure > MappingNotFound` (compound keyword match, score boost +0.35)
- Keyword match: `"telemetry_json_mapping_v2"` → recognized as a mapping object name pattern `[a-z_]+_mapping_v[0-9]+` (regex match, score boost +0.15)
- Nodes with score < 0.5 suppressed: Spark Errors (0.02), Synapse Errors (0.03) → excluded

**Output:**
```python
topic_context = "Kusto Errors | Ingestion Failures | MappingNotFound"
topic_filter_tags = ["kusto", "ingestion", "mapping", "adx", "event_hub"]
excluded_tags = ["spark", "synapse", "adls_permissions", "networking", "databricks"]
```

**Effective search space reduction:**

| Corpus | Document Count |
|---|---|
| Full knowledge base | ~12,000 chunks |
| After Kusto branch filter | ~1,840 chunks |
| After `mapping` + `ingestion` tag filter | ~290 chunks |

**Why This Matters:**

The 290-chunk filtered corpus contains exactly the right neighborhood of documents: ADX mapping creation runbooks, past `MappingNotFound` incidents from ICM, Event Hub data connection configuration guides, and KQL reference pages for `.create-or-alter ingestion mapping`. Without this filter, the vector search for "mapping not found" would pull in chunks about Python `dict` key errors (common in Confluence developer docs), missing field mappings in Elasticsearch (high semantic overlap), and Synapse Analytics column mapping failures — all of which score well on embedding similarity but are completely irrelevant to ADX.

The critical distinction is `MappingNotFound` as a leaf node. The topic tree was built by extracting ADX error code names from historical ICM incidents and cross-referencing the [ADX ingestion error codes documentation](https://learn.microsoft.com/en-us/azure/data-explorer/error-codes). This means the tree knows `MappingNotFound` is a distinct Kusto failure mode, not a generic "missing configuration" problem.

---

### Technique 2: Query Rewriting

**What it is:** A multi-stage query transformation pipeline that converts the raw, often terse alert message into multiple rich query variants. For this scenario, the synonym expansion stage is unusually critical — the error code `Permanent_MappingNotFound` has several aliases used across ADX documentation, Confluence runbooks, and ICM incident titles.

**Input:**
```
"kusto mapping not found telemetry_json_mapping_v2"
```

**Stage 2a — Synonym Expansion:**

The synonym dictionary maps known Kusto/ADX error tokens to their documentation aliases:

```python
SYNONYM_MAP = {
    "MappingNotFound":      ["mapping error", "ingestion mapping missing", "mapping definition not found",
                             "Permanent_MappingNotFound", "mapping reference error"],
    "ingestion mapping":    ["json mapping", "csv mapping", "avro mapping", "ingestion policy mapping",
                             "data connection mapping"],
    "not found":            ["missing", "does not exist", "undefined", "deleted", "dropped"],
    "kusto":                ["ADX", "Azure Data Explorer", "KQL"],
    "telemetry_json_mapping_v2": [],  # ← NO SYNONYMS — this is a proper noun, must be preserved verbatim
}
```

The synonym expander correctly identifies `telemetry_json_mapping_v2` as a proper noun (matches the pattern `[a-z]+_[a-z]+_mapping_v[0-9]+`) and **does not attempt synonym expansion on it**. Expanding a proper noun like this would dilute BM25's ability to do exact-match retrieval on it.

After synonym expansion:
```
"kusto mapping not found ADX MappingNotFound Permanent_MappingNotFound ingestion mapping missing
 mapping definition not found telemetry_json_mapping_v2 schema mismatch"
```

**Stage 2b — Abbreviation Expansion:**

Abbreviation scanner identifies one candidate:
- `v2` in `telemetry_json_mapping_v2` — intentionally **not** expanded (it is part of a proper noun, not a standalone abbreviation)

No expansions applied.

**Stage 2c — Context Enrichment:**

Structured metadata from the ICM alert payload is extracted and appended:
```
pipeline:kusto_ingestion_telemetry
error:Permanent_MappingNotFound
mapping_name:telemetry_json_mapping_v2
table:TelemetryEvents
source:event_hub
platform:adx
```

Enriched query:
```
"Permanent_MappingNotFound ingestion mapping telemetry_json_mapping_v2 TelemetryEvents
 pipeline:kusto_ingestion_telemetry platform:adx source:event_hub"
```

**Stage 2d — LLM Rewriting (GPT-4o-mini):**

Prompt sent to GPT-4o-mini:
```
System: You are a search query optimizer for an Azure Data Explorer / Kusto knowledge base.
Rewrite the following alert into a natural language query that would match runbooks,
incident reports, and KQL reference guides. Include likely root causes, resolution areas,
and related configuration concepts. Preserve specific identifiers exactly as written.

Alert: Permanent_MappingNotFound: Ingestion mapping 'telemetry_json_mapping_v2' was not found.
       EntityName='TelemetryEvents'
Pipeline: kusto_ingestion_telemetry
Context: Event Hub ingestion into ADX, mapping deleted after schema migration
```

GPT-4o-mini output:
```
"Kusto ADX ingestion failure MappingNotFound error where ingestion mapping definition
 telemetry_json_mapping_v2 is missing from TelemetryEvents table, likely caused by schema
 migration deleting the mapping reference, requires recreating ingestion mapping using
 .create-or-alter ingestion mapping KQL command with correct column-to-JSON-path bindings,
 or updating Event Hub data connection to reference a valid mapping name"
```

**Final output — 4 query variants:**

| Variant | Type | Key contribution |
|---|---|---|
| `V1` | Synonym-expanded | Catches `Permanent_MappingNotFound` variant spellings in doc titles |
| `V2` | Context-enriched | Adds `pipeline:` and `platform:` filters for metadata-aware search |
| `V3` | LLM-rewritten | Adds `.create-or-alter ingestion mapping` — the KQL command that appears in the resolution doc |
| `V4` | Original (preserved verbatim) | **Critical for BM25** — exact string `telemetry_json_mapping_v2` gets maximum IDF weight |

```python
query_variants = [
    # V1: Synonym-expanded
    "kusto ADX MappingNotFound Permanent_MappingNotFound ingestion mapping missing definition not found telemetry_json_mapping_v2 schema mismatch",
    # V2: Context-enriched
    "Permanent_MappingNotFound ingestion mapping telemetry_json_mapping_v2 TelemetryEvents pipeline:kusto_ingestion_telemetry platform:adx source:event_hub",
    # V3: LLM-rewritten (natural language, resolution-oriented)
    "Kusto ADX ingestion mapping MappingNotFound schema migration deleted mapping recreate .create-or-alter ingestion mapping Event Hub data connection TelemetryEvents column JSON path bindings",
    # V4: Original (kept verbatim — maximum BM25 exact-match weight for proper noun)
    "kusto mapping not found telemetry_json_mapping_v2",
]
```

**Why This Matters:**

The LLM rewrite introduces `.create-or-alter ingestion mapping` — the specific KQL command that appears in the resolution runbook. This is a case where the LLM's knowledge of the ADX ecosystem directly seeds the retrieval with a term that a support engineer under pressure might not know to search for. The rewrite essentially answers the question "what KQL command fixes a `MappingNotFound` error?" and uses that answer as a retrieval probe.

V4 (the original, verbatim) is the highest-BM25-value variant: `telemetry_json_mapping_v2` as a standalone query token triggers maximum IDF scoring on the two documents that mention this exact mapping name. Without V4, BM25 would never assign the extreme discriminative weight this rare string deserves.

---

### Technique 3: HyDE (Hypothetical Document Embeddings)

**What it is:** Instead of embedding the query and searching for similar chunks, HyDE generates a hypothetical document that looks like the kind of answer you want to retrieve — then embeds *that* document and uses it as the retrieval vector. The intuition: answer-space embeddings are closer to actual answer chunks than question-space embeddings.

**Input:**
```python
error_class = "Permanent_MappingNotFound"
pipeline = "kusto_ingestion_telemetry"
mapping_name = "telemetry_json_mapping_v2"
table = "TelemetryEvents"
```

**Template matching:**

```python
HYDE_TEMPLATES = {
    "OutOfMemoryError":          "spark_oom",
    "Permanent_MappingNotFound": "kusto_mapping_error",    # ← MATCHED
    "SchemaViolation":           "kusto_schema_error",
    "ThrottlingException":       "kusto_throttling",
    "EventHubConnectionError":   "adx_eventhub_connection",
}
selected_template = "kusto_mapping_error"
```

**Hypothetical document generated (via GPT-4o-mini with `kusto_mapping_error` template):**

```
## Kusto Ingestion Mapping Error — Runbook

**Error:** Permanent_MappingNotFound
**Table:** TelemetryEvents
**Mapping:** telemetry_json_mapping_v2

### Root Cause
The ingestion mapping 'telemetry_json_mapping_v2' referenced by the Event Hub data connection
no longer exists. This typically occurs after a table schema migration where:
1. A column type was changed (e.g., string → dynamic)
2. A new column was added without updating the mapping
3. The mapping was explicitly dropped via .drop ingestion mapping

### Diagnosis
Run the following KQL command to list all existing mappings for the table:
  .show table TelemetryEvents ingestion mappings

If 'telemetry_json_mapping_v2' is absent from the output, the mapping must be recreated.

### Resolution
Step 1: Create a new mapping with corrected column types:
  .create-or-alter table TelemetryEvents ingestion json mapping "telemetry_json_mapping_v2"
  '[
    { "column": "EventId",        "path": "$.event_id",     "datatype": "guid"     },
    { "column": "EventTimestamp", "path": "$.timestamp",    "datatype": "datetime" },
    { "column": "UserId",         "path": "$.user_id",      "datatype": "string"   },
    { "column": "EventType",      "path": "$.event_type",   "datatype": "string"   },
    { "column": "EventPayload",   "path": "$.payload",      "datatype": "dynamic"  },
    { "column": "SessionDurationMs", "path": "$.session_duration_ms", "datatype": "long" },
    { "column": "Region",         "path": "$.region",       "datatype": "string"   },
    { "column": "AppVersion",     "path": "$.app_version",  "datatype": "string"   }
  ]'

Step 2: Verify ingestion resumes by checking recent rows:
  TelemetryEvents | where EventTimestamp > ago(5m) | count
```

**HyDE embedding quality analysis:**

```python
hyde_doc_embedding = embed("text-embedding-3-large", hypothetical_document)
# Embedding dimension: 3072
# Cosine similarity between hyde_doc and actual runbook Doc A:
cosine_similarity(hyde_doc_embedding, doc_a_embedding) = 0.847
# vs. raw query embedding:
cosine_similarity(raw_query_embedding, doc_a_embedding) = 0.710
# HyDE improvement: +0.137 cosine similarity points
```

**Why This Matters — and Where HyDE Falls Short Here:**

HyDE provides a measurable improvement for the resolution section of the runbook (the `.create-or-alter ingestion mapping` portion), because the hypothetical document's embedding is semantically close to the actual runbook's resolution chunk.

However, HyDE has an important limitation in this scenario: **the hypothetical document is generic**. The template generates a plausible-looking mapping definition, but it cannot know the exact JSON paths (`$.session_duration_ms`, `$.payload`) without access to the actual Event Hub schema or the original mapping JSON. The hypothetical uses reasonable defaults.

This means HyDE's embedding is closer to the resolution *pattern* (how to fix a mapping error) than to the specific document (the exact runbook for `telemetry_json_mapping_v2`). For retrieving that exact document, BM25's exact-string matching on `telemetry_json_mapping_v2` is still more discriminative than HyDE's embedding proximity.

**Verdict for this scenario:** HyDE is a **supporting technique** — it improves recall of resolution-oriented chunks but is not the primary discriminator. BM25 remains decisive.

---

## Phase 2: Retrieval

*The transformed queries hit Azure AI Search. This is where the BM25 vs. vector balance is most visible.*

---

### Technique 4: RAG Fusion

**What it is:** Instead of issuing a single retrieval query, RAG Fusion decomposes the problem into multiple sub-queries representing different facets of the question. Each sub-query retrieves independently; results are then merged using Reciprocal Rank Fusion (RRF) to produce a combined ranked list.

**Input:**
```
"kusto mapping not found telemetry_json_mapping_v2"
```

**Sub-query decomposition (via GPT-4o-mini):**

```python
sub_queries = [
    # Causal — why does this error happen?
    "Kusto Permanent_MappingNotFound root cause ingestion mapping definition deleted",
    # Resolution — how do you fix it?
    "fix Kusto ingestion mapping not found .create-or-alter ingestion mapping KQL command",
    # Historical — has this happened before on this pipeline?
    "kusto_ingestion_telemetry mapping error incident telemetry_json_mapping_v2 TelemetryEvents",
    # Schema context — what changed that caused this?
    "ADX table schema migration column type change invalidate ingestion mapping",
    # Verification — how do you confirm the fix worked?
    "verify Kusto ingestion mapping exists .show table ingestion mappings",
]
```

**Per-sub-query retrieval results (top-2 docs each):**

| Sub-query | Top 1 Doc | Top 2 Doc |
|---|---|---|
| Causal | ADX Ingestion Error Codes Reference (chunk: MappingNotFound section) | Mapping Migration Runbook |
| Resolution | **telemetry_json_mapping_v2 Creation Runbook** ← Doc A | ADX JSON Mapping Best Practices |
| Historical | **ICM-2714903: telemetry_json_mapping_v2 failure (6 months ago)** | ICM-2801234: UserActivity mapping error |
| Schema context | ADX Schema Migration Guide | TelemetryEvents Schema History (Confluence) |
| Verification | ADX `.show table` command reference | Kusto Ingestion Monitoring Dashboard guide |

**RRF Fusion Score Computation:**

RRF score for a document `d` across `N` sub-queries: `RRF(d) = Σ 1 / (k + rank(d, qᵢ))` where `k=60` (standard constant).

```python
# Doc A (telemetry_json_mapping_v2 Creation Runbook)
rrf_a = 1/(60+1) + 1/(60+1) + 1/(60+3) + 1/(60+4) + 1/(60+6)
      = 0.0164 + 0.0164 + 0.0159 + 0.0156 + 0.0152
      = 0.0795

# Doc D (Generic Kusto JSON Ingestion Guide)  
rrf_d = 1/(60+5) + 1/(60+2) + 0 + 1/(60+2) + 1/(60+2)
      = 0.0154 + 0.0161 + 0 + 0.0161 + 0.0161
      = 0.0637

# ICM Past Incident (ICM-2714903)
rrf_icm = 0 + 0 + 1/(60+1) + 1/(60+7) + 0
        = 0.0164 + 0.0149
        = 0.0313
```

**Final RAG Fusion ranked list (top 5):**

| Rank | Document | RRF Score |
|---|---|---|
| 1 | Doc A — `telemetry_json_mapping_v2` Creation Runbook | 0.0795 |
| 2 | ADX Ingestion Error Codes (MappingNotFound section) | 0.0711 |
| 3 | Doc D — Generic Kusto JSON Ingestion Guide | 0.0637 |
| 4 | ADX Schema Migration Guide | 0.0589 |
| 5 | ICM-2714903 Past Incident | 0.0313 |

**Why This Matters:**

RAG Fusion promotes Doc A not because any single sub-query dominates, but because it appears in the top-3 results of *three different sub-queries* (Resolution, Historical context via its sibling ICM incident, and Verification). The historical ICM document (ICM-2714903) is retrieved via the "Historical" sub-query, which the original single query would have missed entirely. ICM-2714903 contains the previous resolution steps including the exact KQL commands used six months ago — a high-value context addition.

The sub-query `"fix Kusto ingestion mapping not found .create-or-alter ingestion mapping KQL command"` is particularly powerful: it pre-seeds the query with the resolution verb (`.create-or-alter`) that the original alert message doesn't contain, dramatically improving recall of the KQL reference chunk.

---

### Technique 5: Azure AI Search Hybrid Retrieval

**What it is:** Azure AI Search's hybrid search runs BM25 keyword scoring and vector cosine similarity in parallel, then fuses them using Reciprocal Rank Fusion. For this scenario, this is the **single most important technique** — the BM25 component is the decisive factor in surfacing the correct document.

**Query issued to Azure AI Search:**

```python
search_request = {
    "search": "kusto mapping not found telemetry_json_mapping_v2",      # BM25 query
    "vector": embed("text-embedding-3-large", query_variant_v3),         # semantic vector (LLM-rewritten)
    "vectorFields": "content_vector",
    "filter": "topic_tags/any(t: t eq 'kusto') and topic_tags/any(t: t eq 'ingestion')",
    "top": 10,
    "queryType": "semantic",
    "semanticConfiguration": "alert-whisperer-semantic"
}
```

**Detailed Score Breakdown — All Candidate Documents:**

```
Document A: telemetry_json_mapping_v2 Creation Runbook
  Source: Confluence → ADX Runbooks → Ingestion Mappings
  Content snippet: "...to recreate the mapping after schema changes, run:
                    .create-or-alter table TelemetryEvents ingestion json mapping
                    'telemetry_json_mapping_v2'..."

  BM25 scoring breakdown:
    "kusto"                     → tf=2, idf=1.31  → contribution: 0.089
    "mapping"                   → tf=8, idf=2.14  → contribution: 0.187
    "not found"                 → tf=1, idf=3.21  → contribution: 0.108
    "telemetry_json_mapping_v2" → tf=4, idf=8.40  → contribution: 0.537  ← DOMINANT TERM
    ─────────────────────────────────────────────────────────────────────
    BM25 Score (normalized):                          0.921

  Vector scoring:
    Query embedding (V3 LLM-rewritten) cosine similarity: 0.714
    The generic ADX ingestion guide has similar cosine similarity (0.781) because
    both describe JSON ingestion configuration concepts in similar semantic space.
    Vector Score: 0.714

  Hybrid RRF Fusion:
    BM25 rank: #1 → 1/(60+1) = 0.01639
    Vector rank: #3 → 1/(60+3) = 0.01587
    Hybrid Score: 0.01639 + 0.01587 = 0.03226  → normalized → 0.792
```

```
Document B: Generic Kusto JSON Ingestion Guide
  Source: Microsoft Learn mirrored in Confluence
  Content snippet: "...JSON ingestion mappings define the path from source JSON fields to
                    Kusto table columns. Create a mapping with .create ingestion mapping..."

  BM25 scoring breakdown:
    "kusto"                     → tf=5, idf=1.31  → contribution: 0.112
    "mapping"                   → tf=12, idf=2.14 → contribution: 0.218
    "not found"                 → tf=0, idf=N/A   → contribution: 0.000  ← PENALIZED
    "telemetry_json_mapping_v2" → tf=0, idf=8.40  → contribution: 0.000  ← PENALIZED
    ─────────────────────────────────────────────────────────────────────
    BM25 Score (normalized):                          0.449

  Vector scoring:
    The generic guide covers JSON mapping concepts broadly — high semantic
    overlap with any Kusto mapping query.
    Vector Score: 0.781  ← HIGHEST vector score of all candidates

  Hybrid RRF Fusion:
    BM25 rank: #4 → 1/(60+4) = 0.01563
    Vector rank: #1 → 1/(60+1) = 0.01639
    Hybrid Score: 0.01563 + 0.01639 = 0.03202  → normalized → 0.649
```

```
Document C: ICM-2801234 — user_activity_mapping_v3 Failure Incident
  Source: ICM incident archive
  Content snippet: "...Permanent_MappingNotFound for 'user_activity_mapping_v3' on table
                    UserActivityEvents. Resolution: recreated mapping definition via
                    .create-or-alter ingestion mapping..."

  BM25 scoring breakdown:
    "kusto"                     → tf=1, idf=1.31  → contribution: 0.041
    "mapping"                   → tf=6, idf=2.14  → contribution: 0.153
    "not found"                 → tf=1, idf=3.21  → contribution: 0.108
    "telemetry_json_mapping_v2" → tf=0, idf=8.40  → contribution: 0.000  ← PENALIZED
    "Permanent_MappingNotFound" → tf=1, idf=4.70  → contribution: 0.179
    ─────────────────────────────────────────────────────────────────────
    BM25 Score (normalized):                          0.681

  Vector scoring:
    Different mapping name but same error pattern — high semantic similarity.
    Vector Score: 0.744

  Hybrid RRF Fusion:
    BM25 rank: #2 → 1/(60+2) = 0.01613
    Vector rank: #2 → 1/(60+2) = 0.01613
    Hybrid Score: 0.01613 + 0.01613 = 0.03226  → normalized → 0.718
```

**Final Hybrid Ranking:**

| Rank | Document | BM25 | Vector | Hybrid | Notes |
|---|---|---|---|---|---|
| **#1** | **Doc A — `telemetry_json_mapping_v2` Runbook** | **0.921** | 0.714 | **0.792** | BM25 drives ranking via exact mapping name match |
| #2 | Doc C — `user_activity_mapping_v3` ICM Incident | 0.681 | 0.744 | 0.718 | Same error type, different mapping name |
| #3 | Doc B — Generic Kusto JSON Ingestion Guide | 0.449 | **0.781** | 0.649 | Vector ranks highest but BM25 correctly penalizes |
| #4 | ADX Ingestion Error Codes Reference | 0.612 | 0.698 | 0.663 | Good reference doc, not pipeline-specific |
| #5 | ADX Schema Migration Guide | 0.314 | 0.691 | 0.541 | Relevant to root cause but not to fix |

**The Critical Inversion — Vector-Only Would Get This Wrong:**

If this system used vector-only search (no BM25), the ranking would be:

```
#1 Doc B — Generic Guide      (vector: 0.781)  ← WRONG: no mention of this mapping name
#2 Doc C — Different Mapping  (vector: 0.744)  ← WRONG: different mapping, different table
#3 Doc A — Correct Runbook    (vector: 0.714)  ← DEMOTED: correct doc ranked third
```

A support engineer receiving the response based on Doc B would get a technically correct explanation of *how Kusto mapping errors work in general*, but would never find the specific KQL command for `telemetry_json_mapping_v2` with the correct JSON paths. They would attempt to recreate the mapping manually, almost certainly getting the column types wrong (especially the `string` → `dynamic` change for `EventPayload`) because that information exists only in Doc A.

**Why This Matters:**

The BM25 `idf` weight for `telemetry_json_mapping_v2` (≈8.4) is the highest of any query term in this retrieval. This is the mathematical formalization of a simple insight: a term that appears in only 2 out of 12,000 documents is an extraordinary discriminator — any document containing it is almost certainly the right document. BM25 was designed to exploit this property. Embedding models were not.

---

## Phase 2 (Continued): Specialized Retrieval Techniques

---

### Technique 6: Agentic RAG

**What it is:** Instead of passive retrieval from a single index, Agentic RAG dispatches autonomous sub-agents to specialized knowledge sources. Each agent has tool access to a different system and can query, filter, and reason over that system's native interface.

**Agent routing decision (orchestrator LLM):**

```python
agent_routing = {
    # Primary: Confluence mapping runbooks — the mapping definition lives here
    "confluence_agent": {
        "priority": 1,
        "query": "telemetry_json_mapping_v2 creation runbook TelemetryEvents ingestion mapping",
        "space": "ADX-RUNBOOKS",
        "tool": "confluence_search(query, space_key='ADXRB')"
    },
    # Secondary: ICM for past incidents with this exact mapping name
    "icm_agent": {
        "priority": 2,
        "query": "Permanent_MappingNotFound telemetry_json_mapping_v2 kusto_ingestion_telemetry",
        "tool": "icm_search(title_contains='MappingNotFound', labels=['adx', 'telemetry'])"
    },
    # Tertiary: Kusto connector for live cluster state
    "kusto_agent": {
        "priority": 3,
        "query": ".show table TelemetryEvents ingestion mappings",
        "tool": "kusto_query(cluster='adx-prod-eastus2', db='TelemetryDB', kql=query)"
    }
}
```

**Confluence Agent Result:**

```python
# Confluence MCP connector call
confluence_result = confluence_search(
    query="telemetry_json_mapping_v2 TelemetryEvents ingestion json mapping",
    space_key="ADXRB"
)

# Returns: Page "ADX Ingestion Mapping Reference — TelemetryDB Tables"
# Last updated: 2 weeks ago (before the schema migration — important signal!)
# Relevant section excerpt:
"""
## telemetry_json_mapping_v2

Created: 2025-09-14 | Owner: platform-infra-team
Table: TelemetryEvents | Format: JSON

This mapping was created for the v2 schema of TelemetryEvents.
It supersedes telemetry_json_mapping_v1 (deprecated 2025-06-01).

KQL to recreate:
.create-or-alter table TelemetryEvents ingestion json mapping "telemetry_json_mapping_v2"
'[{"column":"EventId","path":"$.event_id","datatype":"guid"},...]'

Note: If the table schema changes, this mapping MUST be recreated. The datatype fields
must match the current column types exactly.
"""
```

**ICM Agent Result:**

```python
# ICM connector call
icm_result = icm_search(
    filters={"title": "MappingNotFound", "labels": ["adx", "telemetry"]},
    date_range="last_12_months"
)

# Returns: ICM-2714903 — "kusto_ingestion_telemetry: MappingNotFound after schema update"
# Filed: 2025-09-03 (6 months ago — same pipeline, same mapping, previous schema migration)
# Resolution notes:
"""
Root cause: mapping 'telemetry_json_mapping_v1' deleted when migrating to v2 schema.
Fix: ran .create-or-alter ingestion mapping with updated column types.
Time to resolve: 23 minutes.
Postmortem action item: add automated check that verifies mapping exists after schema changes.
ACTION ITEM STATUS: OPEN (not implemented)
"""
```

**Kusto Agent Result:**

```python
# Live ADX query via kusto_connector.py
kusto_result = kusto_query(
    cluster="adx-prod-eastus2.eastus2.kusto.windows.net",
    database="TelemetryDB",
    kql=".show table TelemetryEvents ingestion mappings"
)

# Returns: Empty result set
# This confirms the mapping does not currently exist on the cluster
# → Consistent with Permanent_MappingNotFound error
```

**Why This Matters:**

Agentic RAG discovers three things that pure document retrieval cannot:

1. **The authoritative KQL** for this specific mapping from the Confluence runbook (including correct JSON paths)
2. **The historical context** — this exact failure mode happened 6 months ago on the same pipeline, and the postmortem action item to prevent it was never implemented
3. **Live confirmation** — the Kusto agent runs `.show table TelemetryEvents ingestion mappings` in real-time and returns an empty set, confirming the mapping is absent (vs. the scenario where the mapping exists but is misconfigured)

The historical ICM discovery is particularly valuable: the past incident confirms the resolution path, provides an estimated time-to-resolve (23 minutes), and surfaces the embarrassing fact that the same mistake was supposed to be prevented by a postmortem action item that was never closed.

---

## Phase 2 (Continued): Document Processing Techniques

---

### Technique 7: Parent-Child Chunking

**What it is:** Documents are indexed at two granularities simultaneously. "Child" chunks are small (128-256 tokens) and used for retrieval precision. "Parent" chunks are larger (512-1024 tokens) and provide the full context when a child chunk is matched. Retrieval finds the right child; generation reads the full parent.

**Document structure for the Confluence mapping runbook:**

```
Parent Chunk P-0847: Introduction to TelemetryDB ingestion mappings
  ├── Child C-0847-A: Overview of mapping versioning policy (128 tokens)
  ├── Child C-0847-B: When to update an ingestion mapping (156 tokens)
  └── Child C-0847-C: Mapping naming conventions (89 tokens)

Parent Chunk P-0848: telemetry_json_mapping_v2 — Reference Definition      ← TARGET
  ├── Child C-0848-A: Mapping creation KQL command (248 tokens)            ← RETRIEVED FIRST
  ├── Child C-0848-B: Column-to-JSON-path reference table (203 tokens)
  └── Child C-0848-C: Validation and rollback procedures (178 tokens)

Parent Chunk P-0849: Migrating from telemetry_json_mapping_v1
  ├── Child C-0849-A: Step-by-step migration guide (312 tokens)
  └── Child C-0849-B: Known compatibility issues (94 tokens)
```

**Retrieval result:**

Child chunk C-0848-A is retrieved first (highest BM25 + hybrid score) because it contains both the mapping name `telemetry_json_mapping_v2` and the resolution verb `.create-or-alter ingestion mapping`.

```kql
-- Child C-0848-A content (248 tokens):
-- Mapping creation command for telemetry_json_mapping_v2

.create-or-alter table TelemetryEvents ingestion json mapping "telemetry_json_mapping_v2"
'[
  {"column": "EventId",           "path": "$.event_id",              "datatype": "guid"},
  {"column": "EventTimestamp",    "path": "$.timestamp",             "datatype": "datetime"},
  {"column": "UserId",            "path": "$.user_id",               "datatype": "string"},
  {"column": "EventType",         "path": "$.event_type",            "datatype": "string"},
  {"column": "EventPayload",      "path": "$.payload",               "datatype": "dynamic"},
  {"column": "SessionDurationMs", "path": "$.session_duration_ms",   "datatype": "long"},
  {"column": "Region",            "path": "$.region",                "datatype": "string"},
  {"column": "AppVersion",        "path": "$.app_version",           "datatype": "string"}
]'
```

**Parent expansion:**

The parent chunk P-0848 is retrieved in full, adding C-0848-B (the column reference table) and C-0848-C (validation and rollback), giving the LLM the complete resolution context:

```
Parent P-0848 adds:
  - C-0848-B: "Note: EventPayload was changed from string → dynamic in the Mar 2026
                schema migration. Ensure the mapping reflects dynamic, not string."
  - C-0848-C: "To verify: run .show table TelemetryEvents ingestion mappings and confirm
                'telemetry_json_mapping_v2' appears. To test ingestion: use
                .ingest inline into table TelemetryEvents with a sample JSON event."
```

**Why This Matters:**

If child-only chunking were used, the response would contain the KQL creation command but would miss the critical note in C-0848-B: *"EventPayload was changed from string → dynamic"*. This note is exactly what distinguishes a correct fix from an incorrect one. An engineer who recreates the mapping with `"datatype": "string"` for `EventPayload` will see the ingestion resume but produce corrupt `EventPayload` values (JSON serialized as a string literal instead of a structured dynamic object). Parent expansion catches this gotcha.

---

### Technique 8: Semantic Chunking

**What it is:** Instead of splitting documents at fixed token boundaries, semantic chunking uses embedding similarity between consecutive sentences to detect natural topic boundaries. When the semantic similarity between sentence N and sentence N+1 drops below a threshold, a chunk boundary is inserted.

**Challenge for this document:**

The Kusto ingestion mapping documentation interleaves KQL code blocks with prose explanations. A naive fixed-token chunker operating at 256 tokens would split the mapping JSON definition mid-object:

```
NAIVE SPLIT (WRONG):
Chunk A (256 tokens ends mid-JSON):
  "...create-or-alter table TelemetryEvents ingestion json mapping 'telemetry_json_mapping_v2'
  '[{"column":"EventId","path":"$.event_id","datatype":"guid"},
    {"column":"EventTimestamp","path":"$.timestamp","datatype":"datetime"},
    {"column":"UserId","path":"$.user_id","datatype":"string"},
    {"column":"EventType","path":"$.event_type","datatype":"string"},"  ← TRUNCATED

Chunk B continues:
  "{"column":"EventPayload","path":"$.payload","datatype":"dynamic"},   ← ORPHANED
   {"column":"SessionDurationMs"..."
```

**Semantic chunking boundary detection:**

```python
# Similarity scores between consecutive text segments:
sim(prose_intro, kql_command_header)     = 0.71  # topic shift: prose → code intro
sim(kql_command_header, kql_json_block)  = 0.88  # continuous: command + body
sim(kql_json_block[0:4], kql_json_block[4:8]) = 0.95  # highly continuous: same JSON structure
sim(kql_json_block[-1], column_reference_table) = 0.82  # boundary: code → structured table
sim(column_reference_table, validation_prose)  = 0.69  # topic shift: table → validation prose

# Boundary threshold: 0.75
# Boundaries inserted at segments where sim < 0.75:
#   → Between prose_intro and kql_command_header (sim=0.71)  ← boundary
#   → Between column_reference_table and validation_prose (sim=0.69)  ← boundary
# kql_json_block kept INTACT (sim=0.95 throughout)
```

**Result — semantic chunks:**
```
Semantic Chunk SC-A: Prose intro explaining when mapping creation is needed (145 tokens)
Semantic Chunk SC-B: Complete .create-or-alter KQL command with full JSON body (intact, 287 tokens)
Semantic Chunk SC-C: Column-to-path reference table (intact, 203 tokens)
Semantic Chunk SC-D: Validation and rollback procedures (intact, 178 tokens)
```

**Why This Matters:**

By keeping the entire KQL JSON array in a single chunk (SC-B), semantic chunking ensures that when the LLM reads the retrieved context, it sees the *complete and syntactically valid KQL command*. A split mapping definition is not just incomplete — it is syntactically broken and would produce a `SyntaxError` if the LLM reproduced it verbatim in its response.

Semantic chunking also means the code block boundary detection preserves the association between the `.create-or-alter` command header and its JSON body. If these were in separate chunks and only the JSON body was retrieved (but not the header), the LLM would see a JSON array with no surrounding KQL context, making it ambiguous what command it belongs to.

---

## Phase 3: Post-Retrieval

*After retrieval, three techniques work to filter, rerank, and validate the retrieved documents.*

---

### Technique 9: CRAG (Corrective RAG)

**What it is:** CRAG evaluates the relevance of each retrieved document against the query and applies one of three actions: **Accept** (highly relevant, use as-is), **Ambiguous** (partially relevant, strip irrelevant sections), or **Reject** (not relevant, discard entirely). CRAG also triggers a web search fallback if the accepted corpus is insufficient.

**Relevance evaluation of the retrieval set:**

```python
crag_evaluation = {
    "Doc A — telemetry_json_mapping_v2 Runbook": {
        "relevance_score": 0.94,
        "action": "ACCEPT",
        "reason": "Exact mapping name match, correct pipeline, actionable KQL"
    },
    "Doc C — user_activity_mapping_v3 ICM Incident": {
        "relevance_score": 0.71,
        "action": "AMBIGUOUS",
        "reason": "Same error type, different mapping — resolution pattern useful but specific values wrong",
        "strip_sections": ["specific KQL commands with user_activity paths",
                           "table-specific column definitions"],
        "keep_sections": ["error diagnosis workflow", "general .create-or-alter syntax explanation",
                          "post-fix verification steps"]
    },
    "Doc B — Generic Kusto JSON Ingestion Guide": {
        "relevance_score": 0.52,
        "action": "AMBIGUOUS",
        "reason": "Correct domain (Kusto JSON mapping) but no specifics for this pipeline",
        "strip_sections": ["ADX onboarding setup", "mapping format introduction",
                           "CSV and Avro mapping sections"],
        "keep_sections": [".create-or-alter syntax reference", "mapping validation commands"]
    },
    "ADX Schema Migration Guide": {
        "relevance_score": 0.41,
        "action": "REJECT",
        "reason": "Describes how to perform schema migrations, not how to fix mapping errors post-migration"
    },
    # Spark OOM chunk retrieved via weak vector similarity (embedding drift)
    "Spark executor OOM — memoryOverhead tuning": {
        "relevance_score": 0.08,
        "action": "REJECT",
        "reason": "Completely unrelated — Spark, not Kusto; memory, not mapping. Likely false positive from 'executor' token overlap in embedding space."
    }
}
```

**Why the Spark OOM doc appeared:**

The query V3 (LLM-rewritten) contains the phrase `"ingestion failures in downstream monitoring"` (added during context enrichment). The term `"failures"` + `"downstream"` in the embedding space pulls in the Spark OOM incident, which contains language about `"pipeline failures downstream of executor OOM"`. This is a classic embedding false positive: two semantically adjacent but operationally irrelevant documents.

**CRAG correctly rejects the Spark OOM doc** with a relevance score of 0.08 — the lowest possible — because the CRAG evaluator checks for keyword co-occurrence between the query's critical terms (`Permanent_MappingNotFound`, `telemetry_json_mapping_v2`, `ingestion mapping`) and the document, finding zero matches.

**Post-CRAG corpus:**
```python
accepted_context = [
    doc_a_full,                        # ACCEPTED: primary resolution document
    doc_c_stripped,                    # AMBIGUOUS → stripped: error pattern only
    doc_b_stripped,                    # AMBIGUOUS → stripped: syntax reference only
    icm_2714903_full,                  # ACCEPTED: historical incident (from Agentic RAG)
]
# Rejected: ADX Schema Migration Guide, Spark OOM chunk
# Total context tokens before CRAG: 6,840
# Total context tokens after CRAG: 3,920 (43% reduction)
```

**Why This Matters:**

The 43% context reduction is not cosmetic. The Spark OOM content is actively harmful: it would cause the LLM to produce a hedged, confused response that discusses both Kusto mapping errors and Spark memory configurations. Engineers reading the response would waste time parsing irrelevant Spark tuning advice.

More subtly, the stripped version of Doc C (the different mapping incident) provides valuable *diagnostic workflow* content (how to confirm the mapping is missing, how to verify the fix) without polluting the context with wrong KQL paths. This is what the "Ambiguous" category was designed for.

---

### Technique 10: GraphRAG

**What it is:** GraphRAG maintains a knowledge graph where nodes are entities (pipelines, tables, clusters, teams) and edges are typed relationships (`RUNS_ON`, `TRIGGERED_BY`, `WRITES_TO`, `DEPENDS_ON`). When a query is processed, GraphRAG traverses the graph from the identified entity nodes to surface related entities that would not appear in document retrieval.

**Entity recognition from the alert:**

```python
entities_identified = {
    "pipeline":  "kusto_ingestion_telemetry",   # → node in graph
    "table":     "TelemetryEvents",              # → node in graph
    "mapping":   "telemetry_json_mapping_v2",    # → node in graph (config object)
    "cluster":   "adx-prod-eastus2",             # → node in graph
}
```

**Graph traversal (2-hop):**

```
kusto_ingestion_telemetry
│
├── RUNS_ON → adx-prod-eastus2 (ADX Kusto Cluster)
│              └── HOSTED_IN → eastus2 (Azure Region)
│
├── CONSUMES → eventhub-telemetry-prod (Event Hub namespace)
│              └── OWNED_BY → platform-infra-team
│
├── WRITES_TO → TelemetryEvents (table)
│              └── USES_MAPPING → telemetry_json_mapping_v2  ← MAPPING REFERENCE
│
└── TRIGGERED_BY → synapse_pipeline_customer360 (upstream Synapse pipeline)
                   └── MONITORS → kusto_ingestion_telemetry (monitors downstream health)
```

**Critical GraphRAG finding — upstream impact:**

The graph reveals that `synapse_pipeline_customer360` has a `MONITORS` edge to `kusto_ingestion_telemetry`. This means:

```python
graph_insight = """
UPSTREAM IMPACT ALERT:
  synapse_pipeline_customer360 monitors kusto_ingestion_telemetry health.
  If TelemetryEvents ingestion fails for >60 minutes (current duration: 47 min),
  synapse_pipeline_customer360 will trigger its own failure alert at T+13 minutes.
  
  Additionally, synapse_pipeline_customer360 has a downstream dependency:
    synapse_pipeline_customer360 → READS_FROM → TelemetryEvents (for customer session analytics)
  
  This means the customer360 session analytics dashboard will start showing
  stale data as of 14:12 UTC (47 minutes ago). Customer-facing impact likely in ~2 hours
  if ingestion is not restored.
"""
```

**Why This Matters:**

Document retrieval cannot surface this impact chain — no runbook explicitly states "if kusto_ingestion_telemetry fails, synapse_pipeline_customer360 will also fail." This relationship exists only in the system topology graph, which was built by parsing infrastructure-as-code repositories and Synapse pipeline dependency declarations.

The GraphRAG finding changes the **urgency framing** of the response. Without it, Alert Whisperer would describe this as a self-contained ADX mapping fix that can be resolved at normal priority. With it, the response correctly flags that a second, downstream ICM incident is ~13 minutes away and that customer-facing analytics is at risk, making this a Severity 1 escalation.

---

### Technique 11: Cross-Encoder Reranking

**What it is:** A cross-encoder model processes the query and each candidate document *jointly* (query + document concatenated as a single input), producing a fine-grained relevance score. Unlike bi-encoder embeddings (which encode query and document separately), cross-encoders can model exact interactions between specific tokens in the query and the document.

**Why cross-encoders are decisive for keyword-heavy scenarios:**

In a bi-encoder setup, `text-embedding-3-large` encodes `"kusto mapping not found telemetry_json_mapping_v2"` into a single 3072-dimensional vector. When this vector is compared against the encoded Doc A and Doc B, the distance calculation happens in a compressed representation where `telemetry_json_mapping_v2` has been folded into a dense embedding — losing its identity as a specific token.

A cross-encoder model sees:
```
INPUT: [CLS] kusto mapping not found telemetry_json_mapping_v2 [SEP]
       ...to recreate the mapping after schema changes, run:
       .create-or-alter table TelemetryEvents ingestion json mapping
       'telemetry_json_mapping_v2'... [SEP]
```

The self-attention mechanism can directly compute the interaction between the query token `telemetry_json_mapping_v2` and the document token `telemetry_json_mapping_v2` — which produces a very high attention weight because they are identical strings.

**Cross-encoder scoring — top candidates:**

```python
cross_encoder_model = "ms-marco-MiniLM-L-12-v2"  # fine-tuned for passage relevance

scores = cross_encoder.predict([
    ("kusto mapping not found telemetry_json_mapping_v2", doc_a_text),  # → 0.961
    ("kusto mapping not found telemetry_json_mapping_v2", doc_c_text),  # → 0.783
    ("kusto mapping not found telemetry_json_mapping_v2", doc_b_text),  # → 0.641
    ("kusto mapping not found telemetry_json_mapping_v2", doc_d_text),  # → 0.512
    ("kusto mapping not found telemetry_json_mapping_v2", icm_text),    # → 0.847
])

# Re-ranked order:
# 1. Doc A      — 0.961  ← decisive win
# 2. ICM-2714903— 0.847  ← historical incident elevated
# 3. Doc C      — 0.783  ← same error type but different mapping
# 4. Doc B      — 0.641  ← generic guide demoted
# 5. Doc D      — 0.512  ← schema guide remains low
```

**Score differential analysis:**

The gap between Doc A (0.961) and Doc B (0.641) in cross-encoder scores — a difference of **0.320 points** — is significantly larger than the gap in hybrid retrieval scores (0.792 vs. 0.649, difference of 0.143). The cross-encoder is more confident in the distinction because:

1. It sees `telemetry_json_mapping_v2` appear four times in Doc A and zero times in Doc B
2. It sees `.create-or-alter ingestion mapping "telemetry_json_mapping_v2"` as an exact command in Doc A, matching the LLM-rewritten query that includes `.create-or-alter`
3. It sees `TelemetryEvents` in Doc A matching the entity in the query context

**Why This Matters:**

Without cross-encoder reranking, there is a scenario where — depending on the exact query variant used — Doc C (the different mapping incident) could edge past Doc A in the hybrid score because its BM25 score for `Permanent_MappingNotFound` is nearly equal to Doc A's. The cross-encoder breaks this tie decisively in favor of Doc A because it sees the exact mapping name match.

The ICM-2714903 document being elevated to rank #2 is also significant: the cross-encoder recognizes that the historical incident title (`"kusto_ingestion_telemetry: MappingNotFound after schema update"`) is a strong match for the query context — both the pipeline name and the error type match exactly. Without cross-encoder reranking, this document was rank #5 (below Doc C and Doc B) because its body text doesn't contain `telemetry_json_mapping_v2` explicitly (it references `telemetry_json_mapping_v1`, the predecessor).

---

## Phase 4: Generation

*With a high-quality, correctly ranked context assembled, two techniques govern how the final response is generated.*

---

### Technique 12: FLARE (Forward-Looking Active Retrieval)

**What it is:** During response generation, FLARE monitors the LLM's token-level confidence (log probability). When confidence drops below a threshold on a factual claim, FLARE pauses generation, uses the low-confidence sentence as a new query, retrieves additional context, and resumes generation with the new information incorporated.

**FLARE monitoring during generation:**

```python
# Response generation begins with the assembled context (post-CRAG, post-cross-encoder)
# FLARE threshold: token confidence < 0.65 triggers re-retrieval

# Sentence 1: "The mapping 'telemetry_json_mapping_v2' is missing from the TelemetryEvents table."
# Token confidence: 0.97 (confirmed by Kusto agent live query returning empty set)
# FLARE action: CONTINUE

# Sentence 2: "Run the following KQL command to recreate it: .create-or-alter table..."
# Token confidence: 0.95 (exact KQL pulled verbatim from Doc A)
# FLARE action: CONTINUE

# Sentence 3: "The mapping should include 8 columns with the following JSON paths:"
# Token confidence: 0.91 (full column list in Doc A)
# FLARE action: CONTINUE

# Sentence 4: "Note that EventPayload was changed from string to dynamic in the recent schema migration."
# Token confidence: 0.88 (from Parent Chunk C-0848-B)
# FLARE action: CONTINUE

# Sentence 5: "SessionDurationMs uses path $.session_duration_ms and datatype long."
# Token confidence: 0.93 (explicit in Doc A)
# FLARE action: CONTINUE

# Sentence 6 (attempted): "The Event Hub consumer group used by this pipeline is..."
# Token confidence: 0.43  ← BELOW THRESHOLD
# FLARE action: RETRIEVE
# Re-retrieval query: "kusto_ingestion_telemetry Event Hub consumer group configuration"
# Result: No relevant document found in time budget → sentence SUPPRESSED
# FLARE action: SKIP this claim rather than hallucinate
```

**Why This Matters — FLARE as a Minimal Trigger:**

For this scenario, FLARE is a **safety net rather than a primary contributor**. The response is specific and well-grounded: the correct mapping definition is in Doc A, the root cause is confirmed by the Kusto live query, and the resolution KQL is explicit. The LLM generates the core response at high confidence throughout.

The one FLARE trigger — the Event Hub consumer group name — is correctly suppressed. This is the ideal outcome: FLARE prevents a confident-sounding but hallucinated consumer group name from appearing in the response, which could send an engineer to the wrong Event Hub configuration.

In contrast, for a generic "explain Kusto mapping errors" query where the correct answer requires combining several partially relevant documents, FLARE would trigger multiple times and meaningfully improve the response. The low trigger count here is a sign that the pre-retrieval and retrieval phases did their job.

---

### Technique 13: Self-RAG (Self-Reflective Retrieval-Augmented Generation)

**What it is:** After generating a response, Self-RAG uses the LLM to perform a structured self-assessment, evaluating: (a) whether retrieval was needed, (b) whether the retrieved documents are relevant, (c) whether each factual claim is supported by the retrieved context, and (d) whether the overall response is useful. Responses with low self-assessment scores are revised or flagged.

**Self-RAG assessment of the generated response:**

```python
self_rag_evaluation = {
    "retrieval_needed": True,                    # ✓ factual knowledge required
    "retrieved_docs_relevant": True,             # ✓ Doc A directly addresses the query
    "factual_grounding_per_claim": {
        "mapping_is_missing": {
            "claim": "Ingestion mapping 'telemetry_json_mapping_v2' does not exist on the cluster",
            "supported_by": ["Kusto agent live query (empty result set)", "ICM alert payload"],
            "score": 1.0
        },
        "kql_command_correct": {
            "claim": ".create-or-alter table TelemetryEvents ingestion json mapping 'telemetry_json_mapping_v2'",
            "supported_by": ["Doc A — exact KQL from Confluence runbook"],
            "score": 1.0,
            "validation": "KQL syntax verified: .create-or-alter is valid ADX DDL; JSON array structure correct; column names match current table schema from Kusto agent"
        },
        "eventpayload_type_is_dynamic": {
            "claim": "EventPayload column must use datatype 'dynamic' (not 'string') due to recent schema change",
            "supported_by": ["Parent chunk C-0848-B note about schema migration"],
            "score": 0.97
        },
        "sessiondurationms_is_new": {
            "claim": "SessionDurationMs is a new column added in the schema migration and was missing from the original mapping",
            "supported_by": ["ICM alert payload (OperationId)", "Confluence TelemetryEvents schema history (via Agentic RAG)"],
            "score": 0.91
        },
        "downstream_impact_customer360": {
            "claim": "synapse_pipeline_customer360 will be affected if ingestion is not restored within ~13 minutes",
            "supported_by": ["GraphRAG graph traversal — MONITORS edge"],
            "score": 0.89
        }
    },
    "overall_response_useful": True,
    "overall_score": 0.95,
    "revision_needed": False
}
```

**Key Self-RAG validation — KQL syntax check:**

Self-RAG includes a specialized validator for KQL commands in the response. It checks:

```python
kql_validator.validate(".create-or-alter table TelemetryEvents ingestion json mapping 'telemetry_json_mapping_v2' '[...]'")

# Validation checks:
# ✓ Command prefix: .create-or-alter is valid ADX DDL command
# ✓ Table name: TelemetryEvents matches the table referenced in the error
# ✓ Mapping format: ingestion json mapping → valid for JSON event sources
# ✓ Mapping name: 'telemetry_json_mapping_v2' matches the name in the error message exactly
# ✓ JSON array structure: syntactically valid JSON array of mapping objects
# ✓ Required fields per object: "column", "path", "datatype" all present
# ✓ Datatype values: all datatypes (guid, datetime, string, dynamic, long) are valid ADX types
# ✓ Column count: 8 columns (matches .alter table statement from root cause analysis)
# Result: VALID
```

**Why This Matters:**

The KQL syntax validation is essential for a troubleshooting tool. If Alert Whisperer produces a `.create-or-alter` command with a typo in the JSON (e.g., missing closing bracket, wrong field name), the engineer who copies and pastes it will get a second Kusto error — now from the remediation command itself — and lose confidence in the tool.

Self-RAG also catches the downstream impact claim about `synapse_pipeline_customer360` and correctly notes its confidence is 0.89 (slightly lower than direct-evidence claims). This causes the response to include a qualifier: "Based on pipeline dependency graph analysis, synapse_pipeline_customer360 may be affected — recommend verifying in the Synapse monitoring dashboard." This is precisely the right epistemic hedge: confident enough to include the finding, honest about the evidence chain.

---

## Technique Comparison: BM25-Centric View

The table below shows what would happen to the final ranked document list under three search configurations:

| Configuration | Doc A Rank | Doc B Rank | Correct Runbook Found? | Risk |
|---|---|---|---|---|
| **Vector-only** | #3 | #1 | Demoted to 3rd | Wrong generic guide surfaces as primary context; resolution uses wrong JSON paths |
| **BM25-only** | #1 | #4 | Found correctly | Misses conceptually related documents; no ICM historical context; no schema migration context |
| **Hybrid (BM25 + Vector)** | **#1** | #3 | **Found at #1** | Best balance — exact match anchors the correct doc, vector fills adjacent context |
| **Hybrid + Cross-Encoder** | **#1** | #4 | **Found at #1 with high confidence** | **Optimal** — cross-encoder widens the gap between correct and generic docs |

**Vector-only failure mode — detailed:**

Under pure vector search, the ranked list is:
1. Doc B (Generic Kusto JSON Ingestion Guide) — cosine: 0.781
2. Doc C (user_activity_mapping_v3 incident) — cosine: 0.744
3. Doc A (telemetry_json_mapping_v2 Runbook) — cosine: 0.714

The LLM generating a response from this context would produce technically correct general advice — explaining what `.create-or-alter ingestion mapping` does, providing a template JSON mapping structure — but would use the *wrong column definitions*. Specifically, it would not know that `EventPayload` should be `dynamic` (not `string`) and would not know about `SessionDurationMs`. The engineer would create a mapping that allows ingestion to resume but produces malformed `EventPayload` values — a silent data quality bug that might not be caught for days.

**BM25-only failure mode — detailed:**

Under pure BM25 search, Doc A is correctly ranked #1, but the corpus lacks:
- The semantic connection to the schema migration guide (which explains *why* the mapping was invalidated)
- The ICM historical incident (which provides resolution time estimates and the failed postmortem action item)
- The KQL verification commands from related mapping reference docs

The response would be correct but shallow — fixing the immediate error without surfacing the root cause (schema migration without mapping update) or the systemic issue (the postmortem action item that was never implemented).

---

## What Made BM25 Dominant Here

### The Four Properties of BM25-Dominant Errors

This scenario illustrates a general class of errors where BM25 significantly outperforms pure semantic retrieval. These errors share four properties:

**1. Non-decomposable proper nouns**

`telemetry_json_mapping_v2` is a configuration object identifier. Embedding models decompose it into subword tokens (`tele`, `metry`, `_json`, `_map`, `ping`, `_v2`) and encode each subword's meaning in context. But these subwords have no specific semantic meaning that differentiates *this* mapping from *any other JSON mapping*. The embedding for `telemetry_json_mapping_v2` lives in nearly the same vector neighborhood as `telemetry_json_mapping_v1`, `user_activity_mapping_v2`, and `eventhub_ingestion_mapping`. BM25, by contrast, treats the full string as a single term with a unique IDF weight.

**2. High IDF weight for rare strings**

BM25's IDF formula `log((N - n_t + 0.5) / (n_t + 0.5))` assigns maximum discriminative weight to terms that appear in very few documents. `telemetry_json_mapping_v2` appears in exactly 2 out of 12,000 documents (IDF ≈ 8.4). The generic term `mapping` appears in hundreds of documents (IDF ≈ 2.1). BM25 automatically weights the rare term 4× more than the common term — a property that is not replicated in embedding similarity, which weighs both tokens by their contextual meaning, not their rarity in the corpus.

**3. Error codes as opaque tokens**

`Permanent_MappingNotFound` is an ADX error code — a formal identifier from the ADX error catalog. Its semantic interpretation (it means "the mapping was not found permanently") is decomposable, but its identity as a *specific code* among ADX's error taxonomy is not. If an engineer knows this exact code, BM25 can find documents that reference it precisely. Embeddings conflate it with `TransientMappingNotFound`, `MappingValidationError`, and `MappingFormatException` — all in the same semantic neighborhood but with different causes and different resolutions.

**4. Version numbers as discriminators**

The `_v2` suffix in `telemetry_json_mapping_v2` is a version discriminator. The mapping `telemetry_json_mapping_v1` (the deprecated predecessor) has a different KQL definition. An embedding model sees `v1` and `v2` as nearly identical semantic tokens (both are "version number" concepts). BM25 sees them as different terms with different IDF weights. If `telemetry_json_mapping_v1` documents are in the corpus, BM25 correctly ranks `v2` documents higher for a `v2` query — embeddings may not.

### Embedding Model Behavior on Identifier Tokens

To illustrate the embedding collapse problem concretely:

```python
# Cosine similarities between mapping name embeddings
# (text-embedding-3-large, 3072 dims)
sim("telemetry_json_mapping_v2", "telemetry_json_mapping_v2")  = 1.000  # identical
sim("telemetry_json_mapping_v2", "telemetry_json_mapping_v1")  = 0.987  # nearly identical!
sim("telemetry_json_mapping_v2", "user_activity_mapping_v2")   = 0.961  # very similar
sim("telemetry_json_mapping_v2", "eventhub_ingestion_mapping") = 0.934  # still similar
sim("telemetry_json_mapping_v2", "kusto_ingestion_guide")      = 0.891  # moderately similar
```

The embedding model cannot distinguish `telemetry_json_mapping_v2` from `telemetry_json_mapping_v1` (0.987 similarity — nearly identical vectors). This means a document about the v1 predecessor mapping would score almost identically to the v2 document in vector search. BM25 would correctly score the v2 document significantly higher because `telemetry_json_mapping_v2` is a rare term with high IDF, while `telemetry_json_mapping_v1` is a different rare term that simply doesn't match the query.

---

## Summary: Winner for This Scenario

The winning combination for Kusto `Permanent_MappingNotFound` errors is:

```
BM25 (within Azure AI Search Hybrid)
  + Cross-Encoder Reranking
  + CRAG (to remove the false-positive Spark OOM doc)
```

Each component addresses a specific failure mode:

| Without | Failure Mode | Severity |
|---|---|---|
| Without BM25 | Wrong generic mapping guide ranked #1; correct resolution demoted to #3 | **Critical** — wrong JSON paths in created mapping, silent data corruption |
| Without Cross-Encoder | Doc C (different mapping) could tie with Doc A; resolution uses wrong table's column definitions | **High** — mapping created for wrong table; ingestion continues to fail |
| Without CRAG | Spark OOM content in context; LLM produces hedged, confused response mixing Kusto and Spark advice | **Medium** — engineer wastes 10-15 minutes parsing irrelevant Spark tuning advice |
| Without Agentic RAG | No live Kusto query; no ICM historical context; no GraphRAG downstream impact analysis | **High** — misses the Synapse pipeline downstream impact; second ICM fires 13 min later |
| Without GraphRAG | Downstream `synapse_pipeline_customer360` impact not surfaced | **High** — customer-facing analytics stale; Sev1 escalation not flagged |
| Without Parent-Child | Truncated KQL missing `EventPayload: dynamic` note | **Medium** — engineer creates mapping with wrong column type; data ingests but corrupt |

**The winning response** — generated by the full 13-technique pipeline — takes the following form:

```
ALERT WHISPERER RESPONSE:

Root Cause (Confirmed):
  Ingestion mapping 'telemetry_json_mapping_v2' was deleted during the Mar 6 schema
  migration on TelemetryEvents and was not recreated. Live cluster query confirms the
  mapping is currently absent.

This same failure occurred on 2025-09-03 (ICM-2714903) when mapping v1 was deleted
during the v1→v2 migration. The postmortem action item to automate mapping validation
after schema changes was not implemented — this is a recurrence.

URGENT: synapse_pipeline_customer360 monitors this pipeline and will trigger its own
failure alert in approximately 13 minutes if ingestion is not restored. Customer session
analytics are currently reading stale data.

Immediate Fix (Run in ADX Web UI or via Kusto Explorer):

.create-or-alter table TelemetryEvents ingestion json mapping "telemetry_json_mapping_v2"
'[
  {"column": "EventId",            "path": "$.event_id",              "datatype": "guid"},
  {"column": "EventTimestamp",     "path": "$.timestamp",             "datatype": "datetime"},
  {"column": "UserId",             "path": "$.user_id",               "datatype": "string"},
  {"column": "EventType",          "path": "$.event_type",            "datatype": "string"},
  {"column": "EventPayload",       "path": "$.payload",               "datatype": "dynamic"},
  {"column": "SessionDurationMs",  "path": "$.session_duration_ms",   "datatype": "long"},
  {"column": "Region",             "path": "$.region",                "datatype": "string"},
  {"column": "AppVersion",         "path": "$.app_version",           "datatype": "string"}
]'

IMPORTANT: EventPayload must be 'dynamic' (not 'string') — the column type changed in the
Mar 6 migration. Using 'string' will silently corrupt JSON payload values.

Verification (wait 2-3 minutes after running the above):
  TelemetryEvents | where EventTimestamp > ago(5m) | count
  -- Should return rows if ingestion has resumed

Also run:
  .show table TelemetryEvents ingestion mappings
  -- Confirm 'telemetry_json_mapping_v2' appears in the output

Follow-up Action:
  Reopen ICM-2714903 postmortem action item: implement automated mapping validation
  as part of the schema migration pipeline. Suggested: add a .show table ingestion
  mappings health check step to the ADX schema migration runbook.

Source: ADX Ingestion Mapping Reference — TelemetryDB Tables (Confluence ADXRB)
        ICM-2714903 resolution notes | GraphRAG pipeline dependency analysis
```

This response is specific, actionable, and complete — not despite having 13 techniques, but because each technique addressed a specific gap that the others could not fill.

---

## Appendix: Per-Technique Contribution Summary

| Technique | Phase | Primary Contribution | BM25 Interaction |
|---|---|---|---|
| **Topic Tree** | Pre-retrieval | Narrows corpus from 12,000 → 290 chunks | Enables BM25 to operate on relevant subset only |
| **Query Rewriting** | Pre-retrieval | Adds `.create-or-alter`; preserves proper noun `telemetry_json_mapping_v2` for BM25 | V4 (verbatim) maximizes BM25 IDF weight for exact mapping name |
| **HyDE** | Pre-retrieval | Improves semantic recall of resolution-section chunks | Supplements BM25; not primary |
| **RAG Fusion** | Retrieval | Elevates historical ICM incident via causal sub-query | Sub-query decomposition adds resolution verbs that BM25 matches in Doc A |
| **Azure AI Search Hybrid** | Retrieval | **BM25 score 0.921 on `telemetry_json_mapping_v2` correctly ranks Doc A #1** | **Core mechanism** — BM25 IDF weight is the decisive factor |
| **Agentic RAG** | Retrieval | Live Kusto query confirms absence; ICM historical context; Confluence authoritative KQL | No BM25 interaction (different systems) |
| **Parent-Child Chunking** | Retrieval | Expands child with `EventPayload: dynamic` note from parent | BM25 retrieves the child; parent expansion adds critical context |
| **Semantic Chunking** | Indexing | Keeps KQL JSON array intact; prevents syntactically broken chunks | Ensures BM25 matches are syntactically complete documents |
| **CRAG** | Post-retrieval | Rejects Spark OOM false positive; strips irrelevant sections from generic guide | CRAG's keyword check mirrors BM25's logic for relevance scoring |
| **GraphRAG** | Post-retrieval | Surfaces `synapse_pipeline_customer360` downstream impact | No BM25 interaction (graph traversal) |
| **Cross-Encoder** | Post-retrieval | Widens score gap: Doc A 0.961 vs. Doc B 0.641 | Sees exact `telemetry_json_mapping_v2` token match — amplifies BM25's correct ranking |
| **FLARE** | Generation | Suppresses hallucinated Event Hub consumer group name | No BM25 interaction (generation phase) |
| **Self-RAG** | Generation | Validates KQL syntax; grounds downstream impact claim with qualifier | No BM25 interaction (generation phase) |

---

*Next scenario: Scenario 3 — Synapse DWU Throttling (Semantic Vector-Dominant Path), where BM25 fails because the error message is a generic HTTP 429 with no pipeline-specific identifiers, and semantic search dominates.*
