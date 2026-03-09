# Scenario 3 — Cascading Multi-System Failure (Spark → ADLS → Synapse → Kusto)

> **Audience:** Senior Data Engineer building Alert Whisperer — a conversational troubleshooting assistant backed by Azure AI Search, `text-embedding-3-large` (3072 dims), and 13 RAG techniques for Spark/Synapse/Kusto incident response.
>
> **Purpose:** A complete, concrete walkthrough of every technique firing in sequence on a cascading multi-system failure. This scenario is the canonical example of **GraphRAG + Agentic RAG dominance** — where a single root cause (expired Key Vault certificate) spawns 3 separate alerts across 4 systems over 45 minutes, and no single-query retrieval path can connect the dots without graph traversal and multi-source agentic planning.

---

## Scenario Setup

| Field | Value |
|---|---|
| **Initial Trigger** | `spark_etl_daily_ingest` fails with `AzureException: AuthorizationPermissionMismatch` |
| **Detection Time** | 04:12 AM CDT via ICM alert (ICM-4102847) |
| **Root Cause** | Azure Key Vault certificate rotation expired the service principal's ADLS Gen2 credential (`sp-data-platform-prod`) |
| **Systems Affected** | Azure Databricks (Spark) → ADLS Gen2 → Synapse Analytics → Azure Data Explorer (Kusto) |
| **Total Alert Span** | 45 minutes (04:12 → 04:57 AM) |
| **Separate ICM Alerts** | 3 (appearing to be unrelated incidents until correlated) |
| **SLA Breach** | `kusto_ingestion_telemetry` SLA of 05:00 AM missed; 04:57 alert fires |
| **Time Lost Without Correlation** | ~2 hours of independent troubleshooting across 3 alert queues |
| **Triggering Question** | "Are these related? What's the root cause?" |

---

## Incident Timeline

```
04:12 AM  ├── ICM-4102847 FIRES
          │   spark_etl_daily_ingest FAILED
          │   AzureException: AuthorizationPermissionMismatch
          │   ADLS Gen2 path: abfss://raw@adlsprodeastus2.dfs.core.windows.net/events/
          │   Service Principal: sp-data-platform-prod
          │
04:15 AM  ├── Log Analytics Alert FIRES
          │   ADLS Gen2 storage account: adlsprodeastus2
          │   HTTP 403 Forbidden on blob reads
          │   Client: app-id=8c3f1a2d-4b7e (sp-data-platform-prod)
          │   RequestUri: /raw/events/2026/03/08/04/*
          │
04:30 AM  ├── ICM-4103291 FIRES
          │   synapse_pipeline_customer360 TIMEOUT
          │   Activity: ExternalTable_Refresh exceeded 15 min timeout
          │   Reading from: delta_curated_customer360 (not updated since 03:50 AM)
          │
04:57 AM  ├── ICM-4103844 FIRES
          │   kusto_ingestion_telemetry SLA BREACH
          │   Expected completion: 05:00 AM
          │   No ingestion started (trigger event from Synapse never fired)
          │   Impact: TelemetryEvents table stale by 65+ minutes
          │
05:02 AM  └── Support engineer opens Alert Whisperer with 3 active ICM tickets
              Query: "Are these related? What's the root cause?"
```

---

## Dependency Graph (ASCII)

The following graph is the knowledge base representation that GraphRAG traverses. Nodes are pipeline components, secrets, and storage zones. Edges are typed relationships.

```
                        ┌─────────────────────────────┐
                        │      Azure Key Vault          │
                        │  kv-data-platform-prod        │
                        │  Certificate: sp-data-cert    │
                        │  EXPIRED at 03:58 AM          │
                        └──────────────┬────────────────┘
                                       │
                              PROVIDES_AUTH
                                       │
                                       ▼
                        ┌─────────────────────────────┐
                        │   Service Principal           │
                        │  sp-data-platform-prod        │
                        │  app-id: 8c3f1a2d-4b7e        │
                        │  Role: Storage Blob Contributor│
                        └──────────┬──────────────────┬─┘
                                   │                  │
                            GRANTS_ACCESS       GRANTS_ACCESS
                                   │                  │
                    ┌──────────────▼──┐     ┌─────────▼─────────────┐
                    │ ADLS Gen2        │     │ ADLS Gen2             │
                    │ Raw Zone         │     │ Curated Zone          │
                    │ /raw/events/     │     │ /curated/delta/       │
                    └──────┬──────────┘     └──────────┬────────────┘
                           │                           │
                    READ_FROM                    WRITE_TO (blocked)
                           │                           │
              ┌────────────▼────────────────────┐      │
              │  spark_etl_daily_ingest           │──────┘
              │  Databricks cluster               │
              │  Runs: 03:30–04:30 AM daily       │
              │  STATUS: FAILED 04:12 AM          │
              └──────────────┬────────────────────┘
                             │
                      FEEDS_INTO
                             │
              ┌──────────────▼────────────────────┐
              │  synapse_pipeline_customer360       │
              │  Reads: delta_curated_customer360   │
              │  Expects: data fresher than 60 min  │
              │  STATUS: TIMEOUT 04:30 AM           │
              └──────────────┬────────────────────┘
                             │
                    ON_SUCCESS_TRIGGER
                             │
              ┌──────────────▼────────────────────┐
              │  kusto_ingestion_telemetry          │
              │  ADX cluster: adx-prod-eastus2      │
              │  SLA: complete by 05:00 AM          │
              │  STATUS: NEVER STARTED / SLA BREACH │
              └───────────────────────────────────┘
```

**Root cause node: Azure Key Vault** — one expired certificate propagates failure through 4 layers.

---

## The Three Alerts the Engineer Sees

**Alert 1 — ICM-4102847 (04:12 AM):**
```
ALERT: spark_etl_daily_ingest FAILED
Time: 2026-03-08T09:12:04Z
Severity: 2
Component: Azure Databricks
Message: AzureException: AuthorizationPermissionMismatch
  Operation: READ abfss://raw@adlsprodeastus2.dfs.core.windows.net/events/2026/03/08/04/
  ServicePrincipal: sp-data-platform-prod (app-id: 8c3f1a2d-4b7e)
  StatusCode: 403
  ErrorCode: AuthorizationPermissionMismatch
Pipeline owner: data-platform-team@contoso.com
```

**Alert 2 — ICM-4103291 (04:30 AM):**
```
ALERT: synapse_pipeline_customer360 TIMEOUT
Time: 2026-03-08T09:30:18Z
Severity: 2
Component: Azure Synapse Analytics
Message: Activity 'ExternalTable_Refresh' timed out after 900 seconds
  Pipeline: customer360_daily_refresh
  Activity: Refresh_Delta_External_Tables
  Upstream dependency: delta_curated_customer360 (last modified: 2026-03-08T08:52:00Z)
Pipeline owner: analytics-team@contoso.com
```

**Alert 3 — ICM-4103844 (04:57 AM):**
```
ALERT: kusto_ingestion_telemetry SLA BREACH
Time: 2026-03-08T09:57:41Z
Severity: 1
Component: Azure Data Explorer (Kusto)
Message: Ingestion job not started. Expected trigger by 04:45 AM.
  Trigger source: synapse_pipeline_customer360 (success event)
  Last successful run: 2026-03-07T05:03:12Z (yesterday)
  SLA threshold: 05:00 AM daily
  Impact: TelemetryEvents table will be stale at morning dashboard open (07:00 AM)
Pipeline owner: platform-infra-team@contoso.com
```

**The support engineer's opening message to Alert Whisperer:**

> **User:** "Are these related? What's the root cause?"

Note: The engineer has the 3 ICM tickets open in their incident management system. Alert Whisperer receives the query WITH the attached alert payloads as structured context. This is the most ambiguous query across all three scenarios — there is no error message, no stack trace, no system name. Just a two-sentence natural language question.

What follows is the complete trace of all 13 techniques processing this query.

---

## Key Theme: Why GraphRAG + Agentic RAG Dominate This Scenario

Before the technique walkthrough, it is worth understanding why this scenario is fundamentally different from Scenario 1 (single-system OOM) and Scenario 2 (single-system mapping error).

**The multi-system correlation problem** is not a retrieval problem — it is a *reasoning over a knowledge graph* problem. The three alerts have:

- **No shared error code.** `AuthorizationPermissionMismatch`, `Activity Timeout`, and `SLA Breach` are completely different error types in completely different systems.
- **No shared stack trace.** Each comes from a different service with a different log format.
- **No shared timeframe signal in isolation.** A Synapse timeout at 04:30 could have dozens of independent causes.
- **One shared root cause** that only becomes visible when you trace the authentication dependency chain from Key Vault through the service principal to ADLS, then through the downstream pipeline dependencies.

A naive vector similarity search for "Are these related?" retrieves general correlation analysis articles, not the specific auth-chain failure pattern. BM25 on "root cause" retrieves every incident postmortem tagged with "root cause analysis" — thousands of documents.

**Only GraphRAG can traverse:** `spark_etl_daily_ingest → READS_FROM → ADLS Gen2 Raw Zone → PROVIDES_AUTH → Azure Key Vault` and simultaneously `spark_etl_daily_ingest → FEEDS_INTO → synapse_pipeline_customer360 → ON_SUCCESS_TRIGGER → kusto_ingestion_telemetry` to see that all three failing nodes trace back to the same upstream failure point.

**Only Agentic RAG can plan:** "I need to search ICM for correlated incidents in the last 2 hours, AND search Confluence for Key Vault auth troubleshooting, AND query Log Analytics for ADLS 403 patterns, AND traverse the dependency graph — and these are four parallel retrievals that require different tools."

---

## Phase 1: Pre-Retrieval

*Before a single document is fetched, three techniques work together to transform the vague correlation query into a rich, multi-perspective set of retrieval signals.*

---

### Technique 1: Topic Tree Classification

**What it is:** A hierarchical taxonomy of error categories maintained as a lightweight in-memory tree. Every incoming query is routed to one or more leaf nodes before retrieval begins. This scopes all subsequent retrieval to a relevant subset of the knowledge base.

**Input:**
```
"Are these related? What's the root cause?"
[Attached context: 3 alert payloads — Spark 403, Synapse timeout, Kusto SLA breach]
```

**Topic tree traversal (multi-branch):**
```
Infrastructure Errors
├── Authentication & Secrets                  ← BRANCH 1 (score: 0.86)
│   ├── Key Vault Certificate Expiry          ← MATCH (score: 0.83, from 403 + AuthorizationPermissionMismatch)
│   ├── Service Principal Auth Failure        ← MATCH (score: 0.89, from AuthorizationPermissionMismatch)
│   └── ADLS Access Control                   ← MATCH (score: 0.81, from 403 on ADLS path)
└── Data Pipeline Errors
    ├── Spark Errors                           ← BRANCH 2 (score: 0.77)
    │   ├── OutOfMemory
    │   ├── AuthorizationPermissionMismatch    ← MATCH (score: 0.91, exact keyword)
    │   └── StageFailure
    ├── Synapse Errors                         ← BRANCH 3 (score: 0.74)
    │   ├── Pipeline Timeout                   ← MATCH (score: 0.88, from "timed out after 900 seconds")
    │   └── External Table Refresh Failure     ← MATCH (score: 0.79, from activity name)
    └── Kusto Errors                           ← BRANCH 4 (score: 0.71)
        ├── SLA Breach                         ← MATCH (score: 0.85, from "SLA BREACH" in alert title)
        └── Ingestion Not Started
```

**Classification output:**
```python
topic_context = "Authentication & Secrets | Spark Errors > Auth | Synapse Errors > Timeout | Kusto Errors > SLA"
topic_filter_tags = ["key_vault", "service_principal", "adls_auth", "spark_403",
                     "synapse_timeout", "kusto_sla", "cascading_failure"]
multi_branch_flag = True   # ← UNUSUAL: 4 branches matched simultaneously
cascade_signal = True      # ← TRIGGERED: 3+ branches matched across 3+ systems
excluded_tags = ["spark_oom", "kusto_mapping", "synapse_dwu", "networking_latency"]
```

**Why the multi-branch match matters:**

In Scenarios 1 and 2, the topic tree routes to a single primary branch with at most one secondary match. Here, four branches fire simultaneously — `Authentication & Secrets`, `Spark Errors`, `Synapse Errors`, and `Kusto Errors`. The `multi_branch_flag = True` is a rare signal: in testing on 1,400 historical Alert Whisperer queries, only 3.2% triggered 4+ simultaneous branches. When that flag fires, it activates the `cascade_signal` path, which:

1. Unlocks the **cascading failure runbook** category in the knowledge base (normally excluded from scope)
2. Forces Agentic RAG's multi-source planning mode (instead of standard single-source)
3. Activates GraphRAG's full dependency traversal (instead of its lightweight 2-hop lookup)

Without this signal, Alert Whisperer would attempt to handle these as three separate single-system queries, routing each to its respective branch independently.

**Effective search space:**

| Corpus | Document Count |
|---|---|
| Full knowledge base | ~12,000 chunks |
| After cascading failure filter (all 4 branches active) | ~2,100 chunks |
| After `cascade_signal` correlation category unlocked | ~2,650 chunks (expanded, not narrowed) |

Note: Cascade mode *expands* rather than *narrows* the search space, because the relevant documents span multiple system domains. The quality improvement comes not from filtering but from activating the cross-system correlation document category.

---

### Technique 2: Query Rewriting

**What it is:** A multi-stage query transformation pipeline that converts raw, often terse queries into multiple rich query variants. This is one of the most dramatic improvements from Query Rewriting across all three scenarios — the input query "Are these related? What's the root cause?" contains zero technical content without the attached alert context.

**Input:**
```
"Are these related? What's the root cause?"
[Structured context from 3 attached alert payloads]
```

**Stage 2a — Synonym Expansion:**

Synonym expansion on the raw query text finds no technical tokens to expand. The query is pure natural language. However, synonym expansion runs on the *attached alert context* separately:

```python
SYNONYM_MAP = {
    "AuthorizationPermissionMismatch": ["403 Forbidden", "RBAC permission denied",
                                         "service principal unauthorized", "access denied ADLS"],
    "ActivityTimeout":                 ["pipeline timeout", "activity exceeded duration",
                                         "execution timeout Synapse"],
    "SLA Breach":                      ["SLA miss", "ingestion delayed", "SLA violation",
                                         "completion deadline exceeded"],
    "sp-data-platform-prod":           [],  # proper noun — no synonyms
    "Key Vault":                       ["AKV", "Azure Key Vault", "kv-data-platform-prod",
                                         "secret store", "certificate store"],
}
```

**Stage 2b — Context Enrichment (THE CRITICAL STAGE HERE):**

The context enricher extracts structured metadata from all three alert payloads and synthesizes a unified context block:

```python
alert_context = {
    "systems_affected":    ["Databricks/Spark", "ADLS Gen2", "Synapse Analytics", "ADX/Kusto"],
    "error_codes":         ["AuthorizationPermissionMismatch", "ActivityTimeout", "SLABreach"],
    "service_principals":  ["sp-data-platform-prod"],
    "storage_accounts":    ["adlsprodeastus2"],
    "time_window":         "04:12 AM – 04:57 AM (45 minutes)",
    "cascade_chain":       ["spark_etl_daily_ingest", "synapse_pipeline_customer360",
                             "kusto_ingestion_telemetry"],
    "auth_signal":         True,   # 403 + AuthorizationPermissionMismatch → auth root cause likely
    "temporal_pattern":    "sequential",  # alerts fire 18 min, 27 min, 27 min apart — cascade pattern
}
```

**Stage 2c — LLM Rewriting (GPT-4o-mini):**

Prompt sent to GPT-4o-mini:
```
System: You are a search query optimizer for a multi-system Azure data platform knowledge base
covering Spark, ADLS Gen2, Synapse Analytics, and Azure Data Explorer (Kusto). Given a vague
support question and structured metadata from 3 concurrent alerts, rewrite into a precise
technical search query that would match root cause analysis runbooks, ICM incidents with similar
cascading patterns, and authentication troubleshooting guides.

User question: "Are these related? What's the root cause?"

Alert 1: AzureException: AuthorizationPermissionMismatch on ADLS Gen2, service principal
          sp-data-platform-prod, at 04:12 AM
Alert 2: Synapse pipeline timeout (ExternalTable_Refresh) at 04:30 AM, upstream dependency stale
Alert 3: Kusto SLA breach at 04:57 AM — ingestion never triggered (waits on Synapse success event)

Context: Sequential failures 45 minutes apart, all downstream of same Spark job.
```

GPT-4o-mini output (V3 rewrite):
```
"AuthorizationPermissionMismatch ADLS Gen2 service principal sp-data-platform-prod
403 Forbidden causing spark_etl_daily_ingest failure upstream, synapse_pipeline_customer360
timeout due to stale delta table, kusto_ingestion_telemetry SLA breach cascade — root cause
correlation analysis Azure Key Vault certificate rotation service principal credential
expiration propagating authentication failure through pipeline dependency chain"
```

**Final output — 4 query variants:**

| Variant | Type | Key Contribution |
|---|---|---|
| `V1` | Synonym-expanded alert context | Catches "RBAC permission denied" and "access denied ADLS" variants in runbooks |
| `V2` | Context-enriched multi-system | Adds `service_principal:`, `cascade_chain:`, `auth_signal:true` metadata filters |
| `V3` | LLM-rewritten (most valuable) | Introduces "Key Vault certificate rotation" — the resolution domain not present in any alert |
| `V4` | Original (preserved for BM25) | "AuthorizationPermissionMismatch ADLS spark_etl_daily_ingest synapse timeout kusto SLA" |

```python
query_variants = [
    # V1: Synonym-expanded
    "AuthorizationPermissionMismatch 403 Forbidden RBAC permission denied service principal unauthorized ADLS sp-data-platform-prod Synapse timeout Kusto SLA miss",
    # V2: Context-enriched
    "cascade_failure:true systems:spark,adls,synapse,kusto auth_signal:true service_principal:sp-data-platform-prod time_window:45min sequential",
    # V3: LLM-rewritten (natural language, root-cause-oriented)
    "AuthorizationPermissionMismatch ADLS service principal credential expiration Azure Key Vault certificate rotation propagating cascade pipeline failure Spark Synapse Kusto dependency chain SLA breach",
    # V4: Original verbatim
    "Are these related? What's the root cause? AuthorizationPermissionMismatch synapse pipeline timeout kusto ingestion SLA breach",
]
```

**Why this is the most dramatic Query Rewriting improvement across all three scenarios:**

In Scenario 1, the raw OOM error already contained enough technical signal for retrieval to function adequately. In Scenario 2, the mapping name was a precise BM25 anchor. Here, the raw query "Are these related? What's the root cause?" has *zero* technical retrieval value on its own. Every useful term in V3 — "Key Vault certificate rotation", "service principal credential expiration", "authentication failure propagating through dependency chain" — was inferred by the LLM from the structured context, not present in the user's message. Without Query Rewriting, this query retrieves generic correlation analysis articles. With V3, it retrieves the Key Vault certificate rotation runbook and the specific ICM incident from 3 months ago where the same cert expiration caused an identical cascade.

---

### Technique 3: HyDE (Hypothetical Document Embeddings)

**What it is:** Instead of embedding the query, HyDE generates a hypothetical document resembling the answer you want to retrieve, then embeds that document as the retrieval vector.

**Template selected:** `cascade_auth_failure` (triggered by `cascade_signal = True` + `auth_signal = True`)

**Hypothetical document generated:**

```
Root Cause Analysis: Multi-System Cascade Failure from Azure Key Vault Certificate Expiration

Incident Summary: At 03:58 AM, the TLS certificate for service principal sp-data-platform-prod
in Azure Key Vault (kv-data-platform-prod) expired. The service principal is used by
spark_etl_daily_ingest to authenticate against ADLS Gen2 (adlsprodeastus2) via OAuth 2.0
client credentials flow. Certificate expiration invalidated the credential, causing ADLS Gen2
to return HTTP 403 (AuthorizationPermissionMismatch) on all storage operations from 04:12 AM.

Cascade Chain:
1. spark_etl_daily_ingest cannot read from ADLS Raw Zone → job fails at stage 2 (read phase)
2. delta_curated_customer360 table not written → remains stale from previous run (03:50 AM)
3. synapse_pipeline_customer360 reads stale external table → ExternalTable_Refresh activity
   waits for fresh data → exceeds 900-second timeout at 04:30 AM
4. kusto_ingestion_telemetry triggers on synapse_pipeline_customer360 SUCCESS event →
   success event never fires → ingestion never starts → SLA of 05:00 AM breached at 04:57 AM

Root Cause: Azure Key Vault certificate for sp-data-platform-prod expired.

Immediate Fix:
  az keyvault certificate renew \
    --vault-name kv-data-platform-prod \
    --name sp-data-cert \
    --days 365

  az ad sp credential reset \
    --id 8c3f1a2d-4b7e \
    --cert @renewed_cert.pem

Restart sequence: Re-run spark_etl_daily_ingest → confirm ADLS write → trigger
synapse_pipeline_customer360 manually → monitor kusto_ingestion_telemetry to close SLA gap.

Prevention: Set Key Vault certificate expiry alerts 30 days in advance via Azure Monitor.
```

**Why this matters for a cascade scenario:**

The hypothetical document introduces the *full resolution narrative* — including the `az keyvault certificate renew` command, the restart sequence, and the 5-step cascade chain — none of which appear in the user's query. The HyDE embedding vector lands in the semantic neighborhood of Key Vault troubleshooting runbooks, service principal rotation guides, and multi-system incident postmortems, all of which are far from the "Are these related?" query embedding space.

---

### Technique 4: RAG Fusion

**What it is:** Generates multiple semantically diverse query variants, executes independent retrievals for each, and merges using Reciprocal Rank Fusion (RRF).

**LLM-generated variants (GPT-4o-mini, temperature=0.8):**

```
Variant F1: "Azure Key Vault certificate rotation service principal ADLS authentication failure root cause"
Variant F2: "cascading pipeline failure Spark ADLS Synapse Kusto dependency chain 403 auth"
Variant F3: "similar multi-system outages authentication propagation historical incidents ICM"
```

**Independent retrieval results per variant:**

| Variant | Top Retrieved Document | Score | Source |
|---|---|---|---|
| F1 | "Key Vault Certificate Rotation Runbook" | 0.91 | Confluence |
| F1 | "ADLS Gen2 Authentication Troubleshooting" | 0.84 | Confluence |
| F2 | "Multi-System Outage: sp-data-infra cert expiry 2025-12-04" (ICM-3847201) | 0.88 | ICM |
| F2 | "Pipeline Dependency Design Patterns" (cascading failure section) | 0.79 | Confluence |
| F3 | "Multi-System Outage: sp-data-infra cert expiry 2025-12-04" (ICM-3847201) | 0.92 | ICM |
| F3 | "Post-Mortem: ADLS auth failure downstream Synapse timeout 2025-09-17" | 0.81 | ICM |

**RRF merge:**

```python
# RRF formula: score(d) = Σ 1 / (rank(d, query_i) + k)  where k=60
rrf_scores = {
    "Key Vault Certificate Rotation Runbook":           1/(1+60) + 1/(4+60) = 0.0311,
    "Multi-System Outage: sp-data-infra cert (ICM)":    1/(1+60) + 1/(1+60) = 0.0328,  ← TOP
    "ADLS Gen2 Authentication Troubleshooting":         1/(2+60) + 1/(3+60) = 0.0321,
    "Pipeline Dependency Design Patterns":              1/(3+60) + 0        = 0.0159,
}
```

**Why this matters:**

The historical ICM incident (December 2025) where the same certificate `sp-data-infra` expired and caused an identical Spark → Synapse cascade ranks #1 by RRF because it appears in BOTH F2 and F3's top results. A single-variant search might retrieve it from one angle but not surface it confidently. RRF's cross-variant consensus mechanism is exactly what elevates the precedent incident — which contains the entire resolution procedure documented from a previous engineer's war story.

---

## Phase 2: Retrieval

*Four techniques govern how documents are actually fetched from the knowledge base.*

---

### Technique 5: Azure AI Search Hybrid (BM25 + Vector)

**What it is:** Azure AI Search's combined retrieval mode that runs BM25 lexical scoring and `text-embedding-3-large` vector similarity in parallel, then merges using Reciprocal Rank Fusion.

**Query executed:**
```
Primary: V3 (LLM-rewritten) + V4 (verbatim with error codes)
Filter: topic_filter_tags include any of ["key_vault", "service_principal", "adls_auth",
        "spark_403", "synapse_timeout", "kusto_sla", "cascading_failure"]
```

**Top 6 retrieved chunks:**

| Rank | Document | Source | BM25 | Vector | Hybrid |
|---|---|---|---|---|---|
| 1 | "Key Vault Certificate Rotation Runbook" | Confluence | 0.79 | 0.88 | **0.85** |
| 2 | ICM-3847201: "sp-data-infra cert expiry cascade" (Dec 2025) | ICM | **0.91** | 0.83 | **0.88** |
| 3 | "ADLS Gen2 Authentication Troubleshooting" | Confluence | 0.71 | 0.84 | 0.79 |
| 4 | ICM-4001834: "Synapse pipeline timeout — network issue" (Jan 2026) | ICM | 0.68 | 0.76 | 0.73 |
| 5 | "Multi-System Incident Response Playbook" | Confluence | 0.54 | 0.87 | 0.74 |
| 6 | "Azure Service Principal Credential Management" | Confluence | 0.61 | 0.82 | 0.73 |

**Critically retrieved:** ICM-3847201 from December 2025 is the historical precedent incident where an identical cert expiration caused a Spark → Synapse cascade. It ranks #2 overall because BM25 gives high weight to `sp-data-infra` (similar service principal name pattern) and `certificate expiry` co-occurring with `cascade`. This is the document that contains the complete step-by-step resolution that was used 3 months ago.

**False positive flagged:** ICM-4001834 (Synapse pipeline timeout — network issue) is retrieved because it shares the `synapse_timeout` tag and `pipeline timeout` keyword. This document will be explicitly filtered by CRAG (Technique 9).

---

### Technique 6: Agentic RAG

**What it is:** Instead of a single retrieval call, an LLM-based agent plans a sequence of targeted retrieval actions, each using the best tool for a specific sub-question. This is the technique that handles the *multi-source* nature of cascade failures.

**This is a key technique for this scenario.**

**Trigger:** `cascade_signal = True` AND `multi_branch_flag = True` activates Agentic RAG's full planning mode.

**Agent's planning phase (GPT-4o, system prompt: "You are a retrieval planner for a multi-system incident"):**

```
PLAN:

Step 1 [PARALLEL]: Search ICM for all incidents involving systems
  [spark_etl_daily_ingest, synapse_pipeline_customer360, kusto_ingestion_telemetry]
  in the time window 2026-03-08T04:00Z to 2026-03-08T10:00Z
  → Tool: icm_search(time_window="2h", pipelines=["spark_etl_daily_ingest",
                                                    "synapse_pipeline_customer360",
                                                    "kusto_ingestion_telemetry"])
  → Goal: Find all related open incidents and confirm correlation

Step 2 [PARALLEL]: Search Confluence for "Key Vault authentication" and "ADLS 403"
  → Tool: confluence_search(query="Key Vault certificate ADLS 403 service principal rotation")
  → Goal: Find resolution runbook for the auth failure class

Step 3 [PARALLEL]: Query Log Analytics for ADLS access patterns in the last 2 hours
  → Tool: loganalytics_query(kql="StorageBlobLogs | where TimeGenerated > ago(2h) |
          where AuthenticationType == 'OAuth' and StatusCode == 403 |
          summarize count() by CallerObjectId, bin(TimeGenerated, 5m)")
  → Goal: Confirm 403 pattern correlates with cert expiry time, not a permission change

Step 4 [SEQUENTIAL, after Step 1]: Traverse dependency graph starting from
  failing nodes identified in Step 1
  → Tool: graph_query(start_nodes=["spark_etl_daily_ingest"], traversal="upstream",
                       relationship_types=["READS_FROM", "PROVIDES_AUTH"])
  → Goal: Find the upstream root cause node that all 3 failing pipelines share
```

**Step 1 result (ICM search):**
```json
{
  "incidents": [
    {"id": "ICM-4102847", "system": "spark", "time": "04:12", "error": "AuthorizationPermissionMismatch"},
    {"id": "ICM-4103291", "system": "synapse", "time": "04:30", "error": "PipelineTimeout"},
    {"id": "ICM-4103844", "system": "kusto", "time": "04:57", "error": "SLABreach"}
  ],
  "correlation_signal": "all_3_downstream_of_spark_etl_daily_ingest",
  "time_pattern": "sequential_cascade"
}
```

**Step 2 result (Confluence search):**
```
Found: "Key Vault Certificate Rotation Runbook" (page_id: 8847291)
Found: "ADLS Gen2 Authentication: Service Principal Troubleshooting" (page_id: 7293847)
Found: "sp-data-platform-prod Credential Management" (page_id: 9012847)
```

**Step 3 result (Log Analytics KQL):**
```
TimeGenerated          CallerObjectId   Count
2026-03-08T03:55Z      8c3f1a2d-4b7e    0      ← working fine at 03:55 AM
2026-03-08T04:00Z      8c3f1a2d-4b7e    0      ← no requests (Spark not yet reading)
2026-03-08T04:10Z      8c3f1a2d-4b7e    47     ← 47 × 403 Forbidden — Spark hit auth wall
2026-03-08T04:15Z      8c3f1a2d-4b7e    312    ← retry storm
2026-03-08T04:20Z      8c3f1a2d-4b7e    0      ← Spark gave up, job failed

CallerObjectId 8c3f1a2d-4b7e = sp-data-platform-prod
Certificate expiry time (from Key Vault audit log): 03:58 AM ← CONFIRMED
```

**Step 4 result (Graph traversal):**
```
Graph path found (upstream from spark_etl_daily_ingest):
  spark_etl_daily_ingest
    ─[READS_FROM]→ ADLS Gen2 Raw Zone (adlsprodeastus2/raw)
        ─[PROVIDES_AUTH]→ Service Principal (sp-data-platform-prod)
            ─[CREDENTIALS_FROM]→ Azure Key Vault (kv-data-platform-prod)
                                  Certificate: sp-data-cert
                                  STATUS: EXPIRED at 03:58 AM  ← ROOT CAUSE

Graph path found (downstream from spark_etl_daily_ingest):
  spark_etl_daily_ingest
    ─[FEEDS_INTO]→ synapse_pipeline_customer360
        ─[ON_SUCCESS_TRIGGER]→ kusto_ingestion_telemetry
```

**Agentic RAG synthesis:**

Having run 4 retrieval steps (3 in parallel, 1 sequential), the agent synthesizes:

```
ROOT CAUSE CONFIRMED: Azure Key Vault certificate sp-data-cert for service principal
sp-data-platform-prod expired at 03:58 AM. This single failure point propagates to:
  - spark_etl_daily_ingest (direct auth failure, 04:12 AM)
  - synapse_pipeline_customer360 (stale upstream data, timeout 04:30 AM)
  - kusto_ingestion_telemetry (trigger event never fires, SLA breach 04:57 AM)

Resolution runbook located: Confluence page 8847291 "Key Vault Certificate Rotation Runbook"
Historical precedent: ICM-3847201 (December 2025) — same pattern, same fix
```

**Why Agentic RAG is essential here:**

A standard RAG call asks one question and retrieves from one source. This problem requires four different retrieval modalities simultaneously:
- **ICM** for incident correlation (temporal, structured)
- **Confluence** for resolution runbooks (document retrieval)
- **Log Analytics** for real-time auth evidence (live KQL query)
- **Graph** for dependency traversal (structural reasoning)

No single `vector_search()` call can bridge all four. The agent's plan is what makes multi-source correlation possible.

---

### Technique 7: Parent-Child Chunking

**What it is:** Documents are indexed at two granularities — small "child" chunks (150–300 tokens, precise semantic units) and larger "parent" chunks (600–1,200 tokens, full sections). Retrieval operates on child chunks for precision; the retrieved child's parent is returned for context.

**Child chunk retrieved:**
```
Chunk ID: conf-8847291-child-04
Source: "Key Vault Certificate Rotation Runbook" (Confluence)
Content: "To renew a certificate in Azure Key Vault:
  az keyvault certificate renew \
    --vault-name <vault-name> \
    --name <cert-name>
  This command creates a new version of the certificate. The new version becomes
  active immediately. To force the service principal to pick up the new certificate:
  az ad sp credential reset --id <app-id> --cert @<cert-file>"
Tokens: 87
```

**Parent chunk expanded:**
```
Chunk ID: conf-8847291-parent-02
Source: "Key Vault Certificate Rotation Runbook" (Confluence), Section 3: Emergency Rotation
Content: [Full 840-token section including]:
  - Pre-rotation checklist (confirm which service principals use the cert)
  - The renewal command (as above)
  - The credential reset command (as above)
  - Post-rotation validation steps (verify ADLS access restored)
  - Downstream pipeline restart sequence
  - Notification requirements (ICM severity downgrade, on-call notification)
```

**What the parent adds:** The child chunk contains the `az keyvault certificate renew` command, but the parent section contains the critical **restart sequence** — which pipelines to re-trigger in which order after the cert is rotated. Without the parent, the engineer would renew the cert and then wonder why Synapse is still timing out (they need to manually re-trigger `spark_etl_daily_ingest` first, then wait for it to complete, then trigger `synapse_pipeline_customer360`).

---

### Technique 8: Semantic Chunking

**What it is:** Instead of fixed-size token windows, semantic chunking detects natural topical boundaries in documents (using cosine similarity drops between adjacent sentence embeddings) and preserves each coherent section intact.

**Document:** "Multi-System Failure Correlation and Root Cause Analysis" (Confluence, page 7891034)

**Fixed-size chunking result (400 tokens, 50-token overlap):**
```
Chunk A (400 tok): "...authentication failures in Azure services typically manifest as
  HTTP 403 responses. When troubleshooting, first verify service principal permissions
  in Azure AD. Check if the service principal has the Storage Blob Data Contributor
  role on the affected storage account. If permissions appear correct, the issue may
  be with the credential itself — check if the certificate or"   ← CUTS MID-THOUGHT
Chunk B (400 tok): "client secret has expired in Azure Key Vault. Certificate expiry
  is a common cause of sudden auth failures. The Key Vault audit log will show..."
```

**Semantic chunking result (5 natural sections detected, 120–890 tokens each):**
```
Section 1 (120 tok):  "Types of Multi-System Cascade Failures" (introduction, high-level)
Section 2 (340 tok):  "Authentication Chain Failures" ← RETRIEVED (coherent self-contained section)
Section 3 (890 tok):  "ADLS Gen2 Credential Troubleshooting Flowchart" (with decision tree)
Section 4 (510 tok):  "Downstream Pipeline Recovery Sequence" ← RETRIEVED
Section 5 (280 tok):  "Prevention: Certificate Expiry Alerting"
```

**Why this matters for this scenario:**

The "Authentication Chain Failures" section is a complete, self-contained analysis of how cert expiry propagates through Azure service principals. Fixed-size chunking bisects this section mid-sentence at the 400-token boundary, splitting the problem description from the diagnosis steps. When Alert Whisperer retrieves Chunk A, it has the problem framing but not the diagnosis. When it retrieves Chunk B, it has the diagnosis but lacks the framing context. Semantic chunking keeps the section intact — the engineer gets a complete, coherent description of the failure pattern.

---

## Phase 3: Post-Retrieval

*Four techniques refine, validate, and structure the retrieved documents before they reach the LLM for final answer generation.*

---

### Technique 9: CRAG (Corrective RAG)

**What it is:** A relevance evaluation filter that scores each retrieved chunk against the query with an independent scoring model and discards chunks below a relevance threshold. Critically, it also triggers a *corrective retrieval* step — if a chunk is discarded, a targeted follow-up query is issued to fill the gap.

**Chunks evaluated:**

| Chunk | Relevance Score | Decision | Reason |
|---|---|---|---|
| "Key Vault Certificate Rotation Runbook" | **0.94** | KEEP | Directly addresses root cause |
| ICM-3847201 (Dec 2025 cert expiry cascade) | **0.91** | KEEP | Exact historical precedent |
| "ADLS Gen2 Authentication Troubleshooting" | **0.86** | KEEP | Addresses 403 error class |
| ICM-4001834 (Synapse timeout — **network issue**) | **0.31** | **DISCARD** | Wrong root cause — different incident |
| "Multi-System Incident Response Playbook" | **0.77** | KEEP | Relevant cascade response structure |
| "Azure Service Principal Credential Management" | **0.83** | KEEP | Credentials rotation procedure |

**The critical discard:** ICM-4001834 is a Synapse pipeline timeout from January 2026 that was caused by a **network connectivity issue** between Synapse and the ADLS endpoint — not an authentication failure. Its presence in the retrieved set would cause the LLM to include "check network connectivity between Synapse and ADLS" as a troubleshooting step, which is:
1. Wrong (the root cause is Key Vault cert, not networking)
2. Actively misleading (an engineer following this step would spend time on network diagnostics and find nothing wrong)

CRAG's scorer assigns it 0.31 because:
- The incident title says "network issue" — low relevance to the auth-chain query
- The resolution in the ICM body involves Azure network security groups, not Key Vault
- The embedding-based relevance score for the chunk body vs. V3 query is 0.38 (below threshold of 0.45)

**Corrective retrieval triggered:** Because a Synapse timeout chunk was discarded, CRAG issues a follow-up query specifically for Synapse timeout + authentication root cause (to ensure no auth-related Synapse timeout docs were missed):

```python
corrective_query = "Synapse pipeline timeout upstream authentication failure ADLS service principal"
# Returns: No new high-relevance documents found beyond what's already in set
# CRAG confirms: existing Key Vault + ADLS auth docs cover the Synapse timeout sufficiently
```

---

### Technique 10: GraphRAG

**What it is:** Retrieval over a knowledge graph where nodes are pipeline components, services, credentials, and data assets, and edges are typed relationships (READS_FROM, FEEDS_INTO, PROVIDES_AUTH, TRIGGERS, etc.). GraphRAG traverses this graph starting from the failing nodes to find common upstream ancestors (root causes) and common downstream consequences (blast radius).

**This is the star technique for this scenario.**

**Graph traversal — upstream (root cause search):**

Starting from all three failing nodes simultaneously:

```
Node: spark_etl_daily_ingest  (STATUS: FAILED, ERROR: AuthorizationPermissionMismatch)
  ─[READS_FROM]─────────────► ADLS Gen2 Raw Zone
                                  ─[PROTECTED_BY]──► Azure Role Assignment
                                                         ─[ASSIGNED_TO]──► sp-data-platform-prod
                                  ─[REQUIRES_AUTH]─► sp-data-platform-prod
                                                         ─[CREDENTIALS_FROM]─► Azure Key Vault
                                                                                  cert: sp-data-cert
                                                                                  EXPIRED: 03:58 AM
                                                                                  ████ ROOT CAUSE ████

Node: synapse_pipeline_customer360  (STATUS: TIMEOUT, ERROR: ActivityTimeout)
  ─[READS_FROM]─────────────► delta_curated_customer360 (Delta table)
                                  ─[WRITTEN_BY]────► spark_etl_daily_ingest
                                                         ─[FAILED_AT]────► 04:12 AM
                                  ─[LAST_UPDATED]──► 03:50 AM (stale, not today's run)

Node: kusto_ingestion_telemetry  (STATUS: NOT_STARTED, ERROR: SLABreach)
  ─[TRIGGERED_BY]───────────► synapse_pipeline_customer360 (success event)
                                  ─[STATUS: TIMEOUT]─► success event never fired
```

**Graph traversal — downstream (blast radius):**

```
Azure Key Vault (cert expired)
  └──► sp-data-platform-prod credential invalid
         ├──► spark_etl_daily_ingest FAILS
         │      └──► delta_curated_customer360 NOT UPDATED
         │              └──► synapse_pipeline_customer360 TIMES OUT
         │                      └──► kusto_ingestion_telemetry NEVER STARTS
         │                              └──► TelemetryEvents table STALE at dashboard open
         └──► [Check: other pipelines using same SP?]
                ├──► spark_etl_clickstream — uses sp-data-platform-prod (RUNS AT 05:30 AM — WILL ALSO FAIL)
                └──► synapse_pipeline_product_analytics — uses sp-data-platform-prod (RUNS AT 06:00 AM — WILL ALSO FAIL)
```

**GraphRAG output:**

```
GRAPH ANALYSIS RESULT:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ROOT CAUSE: Azure Key Vault kv-data-platform-prod
  Certificate sp-data-cert EXPIRED at 03:58 AM
  Service Principal affected: sp-data-platform-prod (app-id: 8c3f1a2d-4b7e)

ALL 3 ALERTS SHARE THIS ROOT CAUSE:
  ICM-4102847 (Spark 403) ─── auth failure direct
  ICM-4103291 (Synapse timeout) ─── upstream data not produced
  ICM-4103844 (Kusto SLA) ─── trigger event never fired

BLAST RADIUS (ADDITIONAL PIPELINES AT RISK):
  spark_etl_clickstream ─── WILL FAIL at 05:30 AM (uses same SP)
  synapse_pipeline_product_analytics ─── WILL FAIL at 06:00 AM (uses same SP)

HOPS TO ROOT CAUSE:
  spark_etl_daily_ingest: 3 hops (job → ADLS → SP → Key Vault)
  synapse_pipeline_customer360: 4 hops (job → delta_table → spark_job → ADLS → SP → Key Vault)
  kusto_ingestion_telemetry: 5 hops (job → trigger → synapse → delta → spark → ADLS → SP → Key Vault)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**Why this is the decisive technique:**

Without GraphRAG, Alert Whisperer would retrieve documents about each of the three alerts independently. An engineer might get:
- "Spark AuthorizationPermissionMismatch" → suggests checking RBAC, maybe a permission was revoked
- "Synapse pipeline timeout" → suggests checking Synapse DWU, network, upstream dependencies
- "Kusto SLA breach" → suggests checking trigger configuration, Event Hub connections

These three separate paths do not converge on "Key Vault cert expired" because that root cause is **not mentioned in any of the error messages**. It only becomes visible by traversing the authentication dependency graph from the failing node upstream to the credential store.

GraphRAG also identifies two pipelines (`spark_etl_clickstream`, `synapse_pipeline_product_analytics`) that will fail in the next 90 minutes if the cert is not renewed — information that is completely invisible to any document retrieval technique but immediately apparent from graph traversal.

---

### Technique 11: Cross-Encoder Reranking

**What it is:** After retrieval, a more computationally expensive bi-encoder re-scores each (query, document) pair jointly, allowing it to consider interaction between query and document tokens rather than scoring them independently.

**Input to Cross-Encoder:** All kept chunks after CRAG filtering, scored against V3 query.

| Document | Bi-encoder Score (pre-rerank) | Cross-encoder Score | Δ | Final Rank |
|---|---|---|---|---|
| "Key Vault Certificate Rotation Runbook" | 0.85 | **0.95** | +0.10 | **#1** |
| ICM-3847201 (Dec 2025 cert expiry cascade) | 0.88 | **0.93** | +0.05 | **#2** |
| "ADLS Gen2 Authentication Troubleshooting" | 0.79 | 0.82 | +0.03 | #3 |
| "Azure Service Principal Credential Management" | 0.73 | 0.79 | +0.06 | #4 |
| "Multi-System Incident Response Playbook" | 0.74 | 0.71 | −0.03 | #5 |
| ICM-4001834 (Synapse timeout — network) | *DISCARDED by CRAG* | — | — | — |

**Promotion analysis:**

The "Key Vault Certificate Rotation Runbook" gains the largest cross-encoder boost (+0.10) because the cross-encoder sees that the runbook's body contains exact phrases from V3 — "certificate rotation", "service principal credential", "authentication failure" — in a resolution context. The bi-encoder, scoring query and document independently, could not see these direct token interactions.

The ICM-3847201 historical incident scores 0.93 despite being a narrative incident report (not a structured runbook) because the cross-encoder recognizes that the incident describes an identical scenario — identical service principal pattern, identical cascade chain, identical fix procedure.

The "Multi-System Incident Response Playbook" actually *drops* slightly (−0.03) because cross-encoding reveals it's a general playbook about incident response processes, not specifically about auth-chain cascades. It's still kept (0.71 > threshold of 0.60) but ranked after more specific documents.

---

## Phase 4: Generation

*The final two techniques operate during and after LLM answer generation.*

---

### Technique 12: FLARE (Forward-Looking Active REtrieval)

**What it is:** FLARE monitors the LLM's in-progress generation token-by-token. When the LLM produces tokens that signal low confidence or hedging language ("may be related to...", "could indicate...", "I'm not certain whether..."), FLARE pauses generation, retrieves additional context targeted at the uncertain claim, and injects it before generation continues.

**Generation excerpt (before FLARE triggers):**
```
"...The Azure Key Vault certificate expired at 03:58 AM, causing the service principal
sp-data-platform-prod to lose authentication access to ADLS Gen2. This explains the
AuthorizationPermissionMismatch error on spark_etl_daily_ingest. The downstream cascade
to Synapse and then to Kusto follows directly from the pipeline dependency chain.

The Kusto SLA breach may be related to the specific SLA thresholds configured for the
kusto_ingestion_telemetry pipeline — it is not clear from the available context whether
the 05:00 AM SLA deadline includes a grace period..."
```

**FLARE trigger detected:**
```python
uncertainty_phrases = ["may be related to", "it is not clear", "whether"]
hedging_detected = True
retrieval_query = "kusto_ingestion_telemetry SLA threshold configuration grace period"
```

**Retrieval result (targeted):**
```
From Confluence "Kusto Ingestion SLA Definitions" (page 6234891):
  kusto_ingestion_telemetry:
    SLA target:         05:00 AM daily
    Grace period:       None (hard SLA — triggers immediate Sev-1 ICM)
    Notification:       on-call page to platform-infra-team via PagerDuty
    Remediation window: 05:00–07:00 AM (before morning dashboard at 07:00 AM)
    Escalation:         If not resolved by 07:00 AM, Sev-1 escalates to engineering lead
```

**Generation continues (after FLARE injection):**
```
"...The Kusto SLA breach is a hard SLA with no grace period — the 05:00 AM deadline triggers
an immediate Sev-1 ICM and a PagerDuty page to the platform-infra-team on-call. The remediation
window is 05:00–07:00 AM before the morning dashboard opens. If the Key Vault certificate is
not renewed and the pipeline restarted before 07:00 AM, the escalation will page the
engineering lead..."
```

**Why this matters:** Without FLARE, the engineer would receive a response that correctly identifies the root cause but leaves them uncertain about the severity and time pressure of the Kusto SLA breach. The FLARE-triggered retrieval adds the specific notification procedure and escalation timeline — critical operational information for triage prioritization.

---

### Technique 13: Self-RAG (Self-Reflective RAG)

**What it is:** After generation, the LLM's own output is audited by a second model pass that checks each factual claim against the retrieved source documents, classifying each as: `[Supported]`, `[Partially Supported]`, or `[Not Supported / Inferred]`.

**Claims extracted from the generated response:**

| Claim | Verdict | Source |
|---|---|---|
| "Azure Key Vault certificate sp-data-cert expired at 03:58 AM" | **[Supported]** | Log Analytics KQL result (Step 3, Agentic RAG) |
| "Service principal sp-data-platform-prod (app-id: 8c3f1a2d-4b7e)" | **[Supported]** | ICM-4102847 alert payload |
| "All 3 alerts share this root cause" | **[Supported]** | GraphRAG traversal output |
| "`az keyvault certificate renew --vault-name kv-data-platform-prod --name sp-data-cert`" | **[Supported]** | Confluence "Key Vault Certificate Rotation Runbook" |
| "spark_etl_clickstream and synapse_pipeline_product_analytics will also fail" | **[Supported]** | GraphRAG blast radius traversal |
| "Hard SLA — no grace period, immediate Sev-1 on 05:00 AM breach" | **[Supported]** | Confluence "Kusto Ingestion SLA Definitions" (FLARE retrieval) |
| "ICM-3847201 from December 2025 describes an identical cascade" | **[Supported]** | ICM-3847201 body text |
| "Notification procedure: PagerDuty page to platform-infra-team" | **[Supported]** | Confluence SLA page (FLARE retrieval) |
| "Certificate rotation should take approximately 5 minutes" | **[Partially Supported]** | Runbook says "typically quick" but no time specified |
| "Run spark_etl_daily_ingest before triggering synapse_pipeline_customer360" | **[Supported]** | Parent chunk restart sequence |

**Partially Supported handling:**

The claim "certificate rotation takes approximately 5 minutes" is hedged in the final response:

```
"Certificate renewal is typically quick (the runbook does not specify an exact duration —
run the az keyvault certificate renew command and confirm the new version is active before
resetting the service principal credential)."
```

Self-RAG's `[Partially Supported]` flag converts a stated fact into a hedged note, preventing the engineer from building a time estimate into their incident timeline based on an unverified assertion.

---

## Final Response Generated by Alert Whisperer

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ALERT WHISPERER — CASCADE FAILURE ANALYSIS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

YES — all 3 alerts are related. Single root cause identified.

ROOT CAUSE
──────────
Azure Key Vault (kv-data-platform-prod) certificate sp-data-cert expired at 03:58 AM.
This expired the credential for service principal sp-data-platform-prod (app-id:
8c3f1a2d-4b7e), which is used by spark_etl_daily_ingest to authenticate against
ADLS Gen2. All 3 alerts are downstream consequences of this single expiry.

CASCADE CHAIN
─────────────
03:58 AM  Key Vault cert expires → sp-data-platform-prod credential invalid
04:12 AM  spark_etl_daily_ingest: 403 on ADLS read (AuthorizationPermissionMismatch)
04:30 AM  synapse_pipeline_customer360: timeout reading stale upstream data
04:57 AM  kusto_ingestion_telemetry: SLA breach (trigger never fired from Synapse)

IMMEDIATE FIX (run now)
───────────────────────
Step 1: Renew the expired certificate in Key Vault:
  az keyvault certificate renew \
    --vault-name kv-data-platform-prod \
    --name sp-data-cert \
    --days 365

Step 2: Reset service principal credential to use renewed cert:
  az ad sp credential reset \
    --id 8c3f1a2d-4b7e \
    --cert @renewed_cert.pem

Step 3: Verify ADLS access restored:
  az storage blob list \
    --account-name adlsprodeastus2 \
    --container-name raw \
    --auth-mode login

Step 4: Re-trigger the pipeline in sequence:
  # Trigger Spark job (will now complete)
  az databricks jobs run-now --job-id <spark_etl_daily_ingest_job_id>

  # Wait for Spark to complete, then trigger Synapse:
  az synapse pipeline create-run \
    --workspace-name synapse-prod-eastus2 \
    --name customer360_daily_refresh

  # Kusto ingestion will trigger automatically on Synapse success event

ADDITIONAL PIPELINES AT RISK (act now)
───────────────────────────────────────
The following pipelines use the same service principal and WILL FAIL at their
next scheduled run if the cert is not renewed immediately:
  - spark_etl_clickstream: scheduled 05:30 AM → renew cert NOW to prevent
  - synapse_pipeline_product_analytics: scheduled 06:00 AM → renew cert NOW to prevent

SLA STATUS
──────────
kusto_ingestion_telemetry SLA is a HARD SLA (no grace period). The 05:00 AM breach
already filed Sev-1 ICM and paged platform-infra-team on-call via PagerDuty.
Remediation window: now until 07:00 AM (before morning dashboard).

HISTORICAL PRECEDENT
────────────────────
ICM-3847201 (December 2025): Identical pattern — sp-data-infra cert expiry caused
Spark → Synapse cascade. Resolved using the same certificate renewal procedure above.
Resolution time in that incident: 34 minutes from root cause identification to
pipeline recovery.

PREVENTION
──────────
Set a 30-day advance expiry alert on sp-data-cert in Azure Monitor to prevent
recurrence. Runbook: Confluence page 8847291 "Key Vault Certificate Rotation Runbook".
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Technique Impact Comparison

The following table ranks all 13 techniques by their impact on correctly resolving this cascading multi-system failure scenario.

| Rank | Technique | Rating | Impact in This Scenario |
|---|---|---|---|
| 1 | **GraphRAG** | ★★★★★ | Without it, 3 alerts remain 3 separate incidents. GraphRAG traces all 3 to a single Key Vault root cause node and identifies 2 additional at-risk pipelines invisible to document retrieval. |
| 1 | **Agentic RAG** | ★★★★★ | Multi-source retrieval plan (ICM correlation + Confluence runbooks + Log Analytics KQL + graph traversal) was essential. No single-source retrieval could cover all four evidence domains. |
| 3 | **Query Rewriting** | ★★★★★ | Most dramatic transformation across all 3 scenarios. "Are these related?" had zero technical content. V3 rewrite introduced "Key Vault certificate rotation" which was not present in any alert payload. |
| 4 | **CRAG** | ★★★★☆ | Correctly rejected ICM-4001834 (Synapse timeout, wrong root cause — networking). Without CRAG, the response would include "check Synapse-to-ADLS network connectivity" as a troubleshooting step. |
| 5 | **RAG Fusion** | ★★★★☆ | Three parallel decompositions retrieved auth docs, dependency docs, and historical precedent independently — the December 2025 historical incident surfaced through F3 ("similar multi-system outages"). |
| 6 | **Cross-Encoder** | ★★★★☆ | Correctly promoted Key Vault rotation runbook above generic ADLS troubleshooting. Cross-encoding detected direct token interactions ("certificate rotation" + "service principal" in resolution context) that bi-encoder missed. |
| 7 | **HyDE** | ★★★☆☆ | Shifted retrieval vector from "question space" to "answer space" for the Key Vault domain. Less critical here than in Scenario 1 because Agentic RAG's targeted retrievals covered the same ground more precisely. |
| 8 | **FLARE** | ★★★☆☆ | Added critical SLA metadata (hard deadline, no grace period, PagerDuty escalation) when LLM hedged on Kusto SLA specifics. Without FLARE, the engineer would not know the urgency of the 07:00 AM dashboard deadline. |
| 9 | **Semantic Chunking** | ★★★☆☆ | Kept the "Authentication Chain Failures" section intact rather than bisecting it. High value when the relevant section spans 340 tokens — fixed-size chunking would have split it. |
| 10 | **Parent-Child Chunking** | ★★★☆☆ | Child chunk retrieved the `az keyvault certificate renew` command; parent expansion added the full restart sequence (Spark → Synapse restart order). Without the parent, the fix would be incomplete. |
| 11 | **Self-RAG** | ★★★☆☆ | Validated all key claims. Caught one `[Partially Supported]` claim (cert rotation duration) and hedged it appropriately in the response. Prevented a confident incorrect time estimate. |
| 12 | **Topic Tree** | ★★★☆☆ | Multi-branch match (4 simultaneous branches) triggered the `cascade_signal` flag, which activated Agentic RAG's full planning mode and GraphRAG's complete traversal. The upstream value multiplier for other techniques. |
| 13 | **Azure AI Search Hybrid** | ★★☆☆☆ | Standard BM25+vector hybrid was the baseline retrieval for the document-based evidence. Less impactful here than in Scenarios 1–2 because the most decisive evidence (graph traversal, Log Analytics) came from non-document sources. |

---

## What Would Happen Without GraphRAG

This section quantifies the engineer experience if Alert Whisperer processed this scenario using the other 12 techniques but excluded GraphRAG.

**Without GraphRAG, the response would be:**

```
Analysis of the 3 alerts:

Alert 1 (Spark 403): The AuthorizationPermissionMismatch error indicates the service
principal sp-data-platform-prod lacks permission to read from ADLS Gen2. Check the
RBAC role assignment for this service principal — verify it has Storage Blob Data
Reader or Storage Blob Data Contributor on the affected container. This may also be
caused by a recently expired credential. Check the service principal's certificate
or client secret expiry in Azure AD.

Alert 2 (Synapse timeout): The ExternalTable_Refresh activity timed out. This could be:
  (a) The upstream Delta table is stale — Spark may not have run successfully
  (b) Network connectivity issue between Synapse and ADLS (see ICM-4001834 for a
      similar case in January)  ← WRONG DIRECTION (CRAG should have removed this)
  (c) DWU exhaustion causing slow query execution

Alert 3 (Kusto SLA breach): The ingestion job never started because its trigger
(Synapse success event) did not fire. This is a downstream consequence of Alert 2.

These alerts may be related through Alert 1 → Alert 2 → Alert 3, but I cannot
confirm the root cause without more investigation.
```

**Problems with the GraphRAG-free response:**

1. **Root cause not identified.** "Check if the service principal's certificate or client secret expiry" is a suggestion, not a finding. The engineer must still investigate manually.

2. **No blast radius.** `spark_etl_clickstream` and `synapse_pipeline_product_analytics` will fail at 05:30 AM and 06:00 AM. Without graph traversal, this is invisible until it happens.

3. **Wrong suggestion included.** Despite CRAG removing ICM-4001834 from the document set, the LLM still referenced it from its training-data knowledge — "see ICM-4001834 for a similar case" — injecting the wrong-root-cause network hypothesis.

4. **No historical precedent surfaced.** The December 2025 ICM-3847201 incident (same cascade, same fix) is retrieved by RAG Fusion and would normally be the most valuable piece of context. Without GraphRAG confirming the same root cause, the LLM cannot confidently map ICM-3847201 to this incident — it might mention it as "possibly related" rather than "exact precedent."

5. **Estimated engineer time lost:** ~2 hours investigating three alert queues independently before a senior engineer manually notices the auth correlation, identifies the Key Vault cert, and runs the renewal command. The December 2025 ICM-3847201 shows this same manual process took 34 minutes just in the fix phase — the initial ~2 hours of confused triage is the avoidable waste.

**With GraphRAG:** Root cause identified in the first response. Fix procedure linked. Blast radius identified. Additional pipelines saved from failing. Total engineer time: ~12 minutes from query to cert renewed and pipelines requeued.

---

## Azure CLI Fix Commands (Complete Reference)

The following is the complete, copy-pasteable command sequence for an engineer responding to this incident.

### Step 1 — Verify Key Vault Certificate Status

```bash
# Check certificate expiry
az keyvault certificate show \
  --vault-name kv-data-platform-prod \
  --name sp-data-cert \
  --query "{expires: attributes.expires, enabled: attributes.enabled, status: policy.issuer.name}" \
  --output table

# Check Key Vault audit log for exact expiry time
az monitor activity-log list \
  --resource-id /subscriptions/<sub-id>/resourceGroups/<rg>/providers/Microsoft.KeyVault/vaults/kv-data-platform-prod \
  --start-time "2026-03-08T03:00:00Z" \
  --end-time "2026-03-08T05:00:00Z" \
  --query "[?operationName.value == 'Microsoft.KeyVault/vaults/certificates/update']" \
  --output table
```

### Step 2 — Renew the Expired Certificate

```bash
# Renew certificate (creates new version, active immediately)
az keyvault certificate renew \
  --vault-name kv-data-platform-prod \
  --name sp-data-cert \
  --days 365

# Download the renewed certificate
az keyvault certificate download \
  --vault-name kv-data-platform-prod \
  --name sp-data-cert \
  --file renewed_cert.pem \
  --encoding PEM
```

### Step 3 — Reset Service Principal Credential

```bash
# Reset credential to use renewed certificate
az ad sp credential reset \
  --id 8c3f1a2d-4b7e \
  --cert @renewed_cert.pem \
  --output json

# Verify the new credential is active
az ad sp show \
  --id 8c3f1a2d-4b7e \
  --query "passwordCredentials[].{endDate: endDate, customKeyIdentifier: customKeyIdentifier}" \
  --output table
```

### Step 4 — Verify ADLS Access Restored

```bash
# Test read access on the Raw Zone path that was failing
az storage blob list \
  --account-name adlsprodeastus2 \
  --container-name raw \
  --prefix "events/2026/03/08/04/" \
  --auth-mode login \
  --output table

# Expected: returns blob listing (no AuthorizationPermissionMismatch)
```

### Step 5 — Re-trigger Pipeline Sequence

```bash
# Step 5a: Re-trigger Spark ETL (now that ADLS access is restored)
az databricks jobs run-now \
  --job-id <spark_etl_daily_ingest_job_id> \
  --profile prod-databricks

# Step 5b: Monitor until completion (expected ~20 min)
az databricks runs get \
  --run-id <run_id_from_step_5a> \
  --profile prod-databricks \
  --query "state.life_cycle_state"

# Step 5c: Once Spark completes, re-trigger Synapse pipeline manually
az synapse pipeline create-run \
  --workspace-name synapse-prod-eastus2 \
  --name customer360_daily_refresh \
  --output json

# Step 5d: Kusto ingestion will trigger automatically via Synapse success event
# Monitor Kusto ingestion status:
az kusto cluster show \
  --cluster-name adx-prod-eastus2 \
  --resource-group rg-data-platform-prod \
  --query "provisioningState"
```

### Step 6 — Prevent Recurrence

```bash
# Set 30-day advance expiry alert on the certificate
az monitor alert create \
  --name "KeyVault-sp-data-cert-expiry-alert" \
  --resource-group rg-data-platform-prod \
  --condition "metric name 'DaysUntilExpiry' below 30" \
  --description "Alert 30 days before sp-data-cert expires in kv-data-platform-prod" \
  --action-group data-platform-oncall-ag

# Verify other pipelines using same service principal won't fail
# (blast radius pipelines identified by GraphRAG)
# Manually verify spark_etl_clickstream and synapse_pipeline_product_analytics
# are picking up the renewed credential before their 05:30/06:00 AM scheduled runs
```

---

## Summary: Why This Scenario Justifies GraphRAG + Agentic RAG Architecture

Scenario 3 is the definitive argument for the dual GraphRAG + Agentic RAG architecture in Alert Whisperer.

**GraphRAG** is necessary because the root cause (Key Vault cert expiry) never appears in any of the three alert error messages. It is only discoverable by traversing the authentication dependency chain: `Spark job → ADLS path → Service Principal → Key Vault certificate`. That traversal requires a knowledge graph where authentication dependencies are modeled as typed edges — not a vector index of documents. The graph also surfaces two additional at-risk pipelines before they fail, converting a reactive incident response into a proactive outage prevention.

**Agentic RAG** is necessary because the evidence is distributed across four separate data sources that require different access patterns: ICM for incident correlation (structured temporal query), Confluence for resolution runbooks (semantic document retrieval), Log Analytics for real-time auth evidence (live KQL execution), and the dependency graph for structural traversal. No single `vector_search()` call can retrieve from all four simultaneously. The agent's plan — four parallel retrieval actions with a sequential dependency — is what makes multi-source correlation possible at the speed required for incident triage.

Together, they transform a 2-hour manual investigation into a 12-minute automated triage. The value proposition is not academic: the December 2025 historical incident (ICM-3847201) shows exactly how long manual investigation takes for this pattern. Alert Whisperer collapses that timeline by two orders of magnitude.
