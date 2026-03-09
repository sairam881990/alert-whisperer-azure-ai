"""
Prompt Templates for Alert Whisperer.

Implements all prompt engineering techniques:
- Zero-shot prompting
- Few-shot prompting with curated examples
- Chain-of-Thought (CoT) reasoning
- Tree-of-Thought exploration
- ReAct (Reason + Act) pattern
- Self-consistency verification
- Active prompting with uncertainty detection
- Persona/role adoption
- Clear instruction design with output indicators
- Task breakdown with text delimiters

Each template is a Pydantic-validated PromptTemplate with rendering support.
"""

from __future__ import annotations

from src.models import FewShotExample, PromptTemplate

# ---------------------------------------------------------------------------
# P10: Template version registry — maps template_name -> list[PromptTemplate]
# Supports A/B testing by keeping multiple versions per template name.
# ---------------------------------------------------------------------------
_TEMPLATE_VERSIONS: dict[str, list["PromptTemplate"]] = {}


def register_template_version(template: "PromptTemplate") -> None:
    """Register a versioned template. Templates are sorted by version string.
    The first registered (lowest version string) is the default/stable version."""
    _TEMPLATE_VERSIONS.setdefault(template.name, []).append(template)
    # Sort by version string lexicographically; "1.0" < "2.0" < "2.0-experimental"
    _TEMPLATE_VERSIONS[template.name].sort(key=lambda t: getattr(t, "version", "1.0"))


def get_template_version(name: str, version: str | None = None) -> "PromptTemplate":
    """Return a specific version of a template (or the default/stable if None).

    Args:
        name: Template name (e.g. "root_cause_few_shot")
        version: Version string (e.g. "1.0", "2.0-experimental").
                 Pass None to get the default (lowest-version) template.

    Raises:
        KeyError: If the template name or requested version is not found.
    """
    versions = _TEMPLATE_VERSIONS.get(name)
    if not versions:
        raise KeyError(f"No versions registered for template '{name}'")
    if version is None:
        return versions[0]
    for t in versions:
        if getattr(t, "version", "1.0") == version:
            return t
    raise KeyError(f"Template '{name}' has no version '{version}'")


def list_template_versions(name: str) -> list[str]:
    """List all registered version strings for a given template name."""
    return [getattr(t, "version", "1.0") for t in _TEMPLATE_VERSIONS.get(name, [])]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GLOBAL SYSTEM PROMPTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GLOBAL_KNOWLEDGE_PROMPT = """You are Alert Whisperer, an expert AI assistant specialized in data pipeline operations, \
incident management, and troubleshooting for enterprise data platforms built on Azure.

=== PRIMARY TECHNOLOGY STACK (P1: explicit domain grounding) ===

Apache Spark / Azure Databricks:
- Execution model: Driver + N Executors; jobs split into stages at wide transformations (shuffle boundaries)
- Memory: executor memory = spark.executor.memory + overhead; driver memory separate; off-heap for tungsten
- Common failures: OutOfMemoryError (heap/GC), shuffle spill to disk, partition skew, speculative execution races
- Valid executor memory range: 2 GiB – 64 GiB (Databricks cluster max 64 GiB per executor node class)
- Delta Lake: ACID transactions on ADLS Gen2; common issues: concurrent writes, vacuum removing active files
- Structured Streaming: micro-batch or continuous; checkpointing on ADLS; backpressure via maxFilesPerTrigger
- Key Spark config knobs: spark.sql.shuffle.partitions, spark.memory.fraction, spark.executor.memoryOverhead

Azure Synapse Analytics:
- Pipeline orchestration: activities chained with dependency conditions (Succeeded / Failed / Skipped / Completed)
- Integration Runtimes (IR): Azure IR (managed), Self-hosted IR (SHIR on-prem), Azure-SSIS IR
- Dedicated SQL Pool: MPP engine with distributions (hash/round-robin/replicate); DWU controls concurrency
- Serverless SQL Pool: pay-per-query on ADLS; no resource reservation; supports Delta/Parquet/CSV
- Spark Pools: auto-pause / auto-scale; session isolation between notebooks
- Copy Activity error codes: UserErrorInvalidFolderPath, UserErrorAccessForbidden, TransientErrorNetworkTimeout
- Data Flow: Spark-backed visual ETL; debug clusters separate from production clusters

Azure Data Explorer (Kusto / ADX):
- KQL: pipe-based query language; common operators: where, summarize, join, project, extend, parse
- Ingestion paths: Queued ingestion (EventHub/EventGrid/Blob), Streaming ingestion (low latency <10s)
- Ingestion mappings: JSON/CSV/Avro field mappings; Permanent_ prefix in error codes = no auto-retry
- Materialized views: pre-aggregated; require source table to have ingestion policy enabled
- Cluster scaling: Optimistic autoscale; cache policy hot vs cold data
- ADX Follower databases: read-only attached databases from a leader cluster
- Common ADX errors: Permanent_MappingNotFound, Transient_LowMemoryCondition, Permanent_SchemaNotFound

Azure Data Factory (ADF):
- IR types: Azure, Self-hosted, Azure-SSIS; SHIR registration issues cause ActivityNotStarted errors
- Linked Services: connection definitions; credential type (MSI, Service Principal, Key Vault reference)
- Triggers: Schedule, Tumbling Window, Event-based (Blob/Custom), Storage Events
- Mapping Data Flows: Spark-based; debug cluster reuse reduces cold-start overhead

Supporting Azure Infrastructure:
- ADLS Gen2: hierarchical namespace; ACL vs RBAC; NFS mount for Databricks FUSE-based access
- Key Vault references in linked services; secret rotation causes AuthenticationError if cache not invalidated
- Managed Identity vs Service Principal auth; common issue: identity missing role assignment
- Azure Monitor / Log Analytics: KQL queries against AzureDiagnostics, SparkListenerEvent, SynapseActivity
- Private Endpoints: DNS resolution issues cause ConnectionRefused; check private DNS zones

=== OPERATIONAL CONTEXT ===
- You support a large-scale data platform with 100+ pipelines processing TBs of data daily
- The environment uses Azure Databricks for Spark workloads, Synapse for orchestration, and Kusto for telemetry
- Incidents are tracked in ICM with SLA-based severity levels (Sev1: 5min, Sev2: 15min, Sev3: 60min, Sev4: 4hr)
- Runbooks are maintained in Confluence and follow a standardized format
- Pipeline ownership is mapped to specific teams with on-call rotations

=== BEHAVIORAL GUIDELINES ===
1. Always explain root causes in plain English first, then provide technical details
2. Reference specific runbook steps when available
3. Suggest concrete next actions, not vague recommendations
4. When uncertain, say so explicitly and suggest what information would help
5. Prioritize mitigation (stop the bleeding) before root cause analysis
6. Always consider blast radius — what else might be affected?
7. When recommending Spark configuration changes, validate values against known valid ranges
8. When diagnosing Kusto errors, note whether the prefix is Permanent_ (no retry) or Transient_ (auto-retry)
9. When diagnosing Synapse errors, identify whether the IR type is relevant to the failure

IMPORTANT: If you are uncertain about an answer or the available data is insufficient to provide an accurate response, clearly state "I don't have enough information to answer this accurately" rather than guessing. It's better to be honest about limitations than to provide potentially incorrect information.
"""

STYLE_GUIDE_PROMPT = """=== COMMUNICATION STYLE ===
- Lead with the impact: what is broken and who is affected
- Use severity-appropriate urgency in tone (critical = direct/urgent, low = informational)
- Structure responses with clear sections: Summary → Root Cause → Impact → Actions → References
- Use bullet points for action items, numbered steps for procedures
- Include direct links to logs, runbooks, and rerun URLs when available
- Keep technical jargon to a minimum unless the audience is deeply technical
- Use consistent terminology: 'pipeline' not 'job/flow/workflow' interchangeably
- Timestamp all references (UTC)
- Bold critical information: severity, affected pipeline, required action

IMPORTANT: If you are uncertain about an answer or the available data is insufficient to provide an accurate response, clearly state "I don't have enough information to answer this accurately" rather than guessing. It's better to be honest about limitations than to provide potentially incorrect information.
"""

SOLUTION_PROMPT = """=== SOLUTION FRAMEWORK ===
When providing solutions, follow this structure:

**Immediate Mitigation** (What to do RIGHT NOW):
- Steps to stop the bleeding / restore service
- Workarounds or manual interventions

**Root Cause Analysis**:
- What went wrong and why
- Which component failed first (chain of causation)
- Whether this is a new issue or recurrence

**Permanent Fix**:
- Code/configuration changes needed
- Testing requirements before deployment
- Rollback plan if the fix causes issues

**Prevention**:
- Monitoring/alerting improvements
- Runbook updates needed
- Architectural changes to prevent recurrence

IMPORTANT: If you are uncertain about an answer or the available data is insufficient to provide an accurate response, clearly state "I don't have enough information to answer this accurately" rather than guessing. It's better to be honest about limitations than to provide potentially incorrect information.
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROOT CAUSE ANALYSIS PROMPTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ROOT_CAUSE_ZERO_SHOT = PromptTemplate(
    name="root_cause_zero_shot",
    description="Zero-shot root cause analysis for pipeline failures",
    technique="zero_shot",
    variables=["error_message", "pipeline_name", "pipeline_type", "log_snippet", "timestamp"],
    template="""Analyze this pipeline failure and provide a root cause summary.

=== FAILURE DETAILS ===
Pipeline: {{pipeline_name}}
Type: {{pipeline_type}}
Timestamp: {{timestamp}}

Error Message:
<<<ERROR>>>
{{error_message}}
<<<END_ERROR>>>

Log Excerpt:
<<<LOGS>>>
{{log_snippet}}
<<<END_LOGS>>>

=== REQUIRED OUTPUT ===
Provide your analysis in this exact format:

**Error Classification**: [One of: OutOfMemoryError, TimeoutException, ConnectionFailure, AuthenticationError, SchemaError, DataQualityError, ResourceExhaustion, ConfigurationError, InfrastructureFailure, Unknown]

**Root Cause Summary** (plain English, 2-3 sentences):

**Technical Details**:

**Severity Assessment**: [critical/high/medium/low] — Justify your assessment.

**Affected Components**: List all systems/services impacted.
""",
)


ROOT_CAUSE_FEW_SHOT = PromptTemplate(
    name="root_cause_few_shot",
    description="Few-shot root cause analysis with curated examples",
    technique="few_shot",
    variables=["error_message", "pipeline_name", "pipeline_type", "log_snippet", "timestamp", "few_shot_examples"],
    template="""You are an expert data pipeline diagnostician. Analyze failures using the examples below as reference.

=== P6: WORKED EXAMPLES — COMMON SPARK/SYNAPSE FAILURE PATTERNS ===

--- Worked Example A: Spark OOM (Out-of-Memory on Executor) ---
INPUT:
Pipeline: spark_daily_aggregation | Type: spark_batch
Error: java.lang.OutOfMemoryError: Java heap space
Logs: ERROR Executor: Exception in task 23.2 in stage 8.0 (TID 1102)
      java.lang.OutOfMemoryError: Java heap space
      at org.apache.spark.sql.catalyst.expressions.UnsafeRow.copy(UnsafeRow.java:531)
      Executor memory: 8g, memoryOverhead: 1638m
OUTPUT:
**Error Classification**: OutOfMemoryError
**Root Cause Summary**: Executor ran out of heap memory during a shuffle operation in stage 8. \
The UnsafeRow.copy call indicates data was being serialized during a join/aggregation. \
Task attempt 23.2 (3rd retry) shows repeated failure on the same partition — consistent with data skew.
**Technical Details**: Stage 8 is a shuffle-write stage (wide transformation). Task 23 is retrying (attempt .2) \
because earlier attempts OOM’d on the same data. UnsafeRow.copy is called during shuffle serialization, \
confirming executor heap exhaustion during shuffle rather than during compute. Executor memory was 8g with \
1638m overhead, leaving ~6.4g for execution. A skewed partition likely exceeds available execution memory.
**Severity Assessment**: HIGH — Daily aggregation pipeline is blocked; downstream BI reports will be stale.
**Affected Components**: spark_daily_aggregation, downstream reporting tables, SLA dashboard.
**Why this matters**: Increasing executor memory alone may not fix this if skew is the root cause. \
Check spark.sql.adaptive.skewJoin.enabled=true (AQE) or use salting on the join key.

--- Worked Example B: Spark Shuffle Spill causing TimeoutException ---
INPUT:
Pipeline: spark_etl_customer_features | Type: spark_batch
Error: org.apache.spark.SparkException: Task exceeded shuffle spill limit
Logs: INFO MemoryStore: Will not store broadcast_42_piece0 in memory (free: 512.0 MB, required: 1.2 GB)
      WARN UnsafeExternalSorter: Spilling data because no enough memory ...
      Shuffle spill (memory): 45.2 GB, Shuffle spill (disk): 38.7 GB
      Stage 14 total time: 2h 18m (expected: 20m)
OUTPUT:
**Error Classification**: ResourceExhaustion
**Root Cause Summary**: Spark stage 14 spilled 38.7 GB to disk during a large shuffle due to insufficient \
execution memory. Spill caused extreme stage slowdown (2h 18m vs 20m expected), eventually hitting the stage timeout.
**Technical Details**: spark.sql.shuffle.partitions is likely set too low (default 200), creating too few large \
partitions that each exceed executor execution memory. The 45.2 GB memory spill and 38.7 GB disk spill confirm \
the executor’s execution memory fraction is undersized for the data volume. Broadcast join hint on broadcast_42 \
failed due to insufficient memory.
**Severity Assessment**: HIGH — Feature pipeline is delayed; any ML training jobs depending on features are blocked.
**Affected Components**: spark_etl_customer_features, ML feature store, downstream training pipelines.
**Fix**: Increase spark.sql.shuffle.partitions to 2000–4000, or increase spark.executor.memory to 16g+, \
or enable AQE (spark.sql.adaptive.enabled=true) for automatic partition coalescing.

--- Worked Example C: Spark Partition Skew ---
INPUT:
Pipeline: spark_join_events_users | Type: spark_batch
Error: org.apache.spark.TaskKilledException: killed reason: another attempt succeeded
Logs: Stage 6: (200 tasks) min=0.2s, median=1.4s, max=4h 12m, straggler tasks: 3
      Task 198: processing 87,432,901 rows (partition: user_id=NULL)
      Task 199: processing 91,204,441 rows (partition: user_id=NULL)
      Median partition size: 12,341 rows
OUTPUT:
**Error Classification**: ResourceExhaustion
**Root Cause Summary**: Stage 6 has severe partition skew — two tasks process ~88M and ~91M rows each while \
the median partition is only 12K rows (7,000x skew ratio). The NULL user_id values are concentrating into \
a single partition, causing extreme stragglers.
**Technical Details**: The NULL skew pattern occurs when joining on a nullable key (user_id). All NULL values \
route to the same reduce partition. AQE skew join optimization may not handle NULL skew. \
Enable spark.sql.adaptive.skewJoin.enabled=true and spark.sql.adaptive.skewJoin.skewedPartitionThresholdInBytes. \
Alternatively, pre-filter NULLs or use COALESCE(user_id, uuid()) to distribute NULLs.
**Severity Assessment**: HIGH — Straggler tasks are blocking stage completion for 4+ hours.
**Affected Components**: spark_join_events_users, user analytics dashboard, daily event aggregation.

=== ADDITIONAL USER-PROVIDED EXAMPLES ===
{{few_shot_examples}}

=== NEW FAILURE TO ANALYZE ===
Pipeline: {{pipeline_name}}
Type: {{pipeline_type}}
Timestamp: {{timestamp}}

Error:
<<<ERROR>>>
{{error_message}}
<<<END_ERROR>>>

Logs:
<<<LOGS>>>
{{log_snippet}}
<<<END_LOGS>>>

Provide your analysis in the same format as the examples above.
""",
)


ROOT_CAUSE_CHAIN_OF_THOUGHT = PromptTemplate(
    name="root_cause_cot",
    description="Chain-of-Thought root cause analysis with step-by-step reasoning",
    technique="chain_of_thought",
    variables=["error_message", "pipeline_name", "pipeline_type", "log_snippet", "context"],
    template="""Analyze this pipeline failure using step-by-step reasoning. Think through each step carefully before concluding.

=== FAILURE CONTEXT ===
Pipeline: {{pipeline_name}}
Type: {{pipeline_type}}

Error: {{error_message}}

Logs:
<<<LOGS>>>
{{log_snippet}}
<<<END_LOGS>>>

Historical Context:
<<<CONTEXT>>>
{{context}}
<<<END_CONTEXT>>>

=== STEP-BY-STEP ANALYSIS ===

**Step 1 — Identify the Error Type**:
What category of error is this? Look at the exception class, error code, and message patterns.

**Step 2 — Trace the Failure Chain**:
What was the sequence of events? What triggered the error? Was there a cascade?

**Step 3 — Identify the Root Cause**:
What is the underlying reason? Distinguish between the symptom (what failed) and the cause (why it failed).

**Step 4 — Assess Historical Patterns**:
Does this match any known patterns from the historical context? Is this a regression or a new issue?

**Step 5 — Determine Impact and Blast Radius**:
What downstream systems/pipelines are affected? What data may be stale or missing?

**Step 6 — Recommend Actions**:
What should be done immediately? What's the permanent fix?

Now provide your complete analysis following these steps:
""",
)


ROOT_CAUSE_TREE_OF_THOUGHT = PromptTemplate(
    name="root_cause_tree_of_thought",
    description="Tree-of-Thought exploration of multiple root cause hypotheses",
    technique="tree_of_thought",
    variables=["error_message", "pipeline_name", "log_snippet", "context"],
    template="""Explore multiple possible root causes for this failure. Evaluate each hypothesis before converging on the most likely cause.

=== FAILURE ===
Pipeline: {{pipeline_name}}
Error: {{error_message}}

Logs:
<<<LOGS>>>
{{log_snippet}}
<<<END_LOGS>>>

Context:
<<<CONTEXT>>>
{{context}}
<<<END_CONTEXT>>>

=== THOUGHT TREE EXPLORATION ===

Generate 3 distinct hypotheses for what caused this failure. For each:

**Hypothesis 1**: [State the hypothesis]
- Evidence FOR: [What in the logs/error supports this?]
- Evidence AGAINST: [What contradicts this?]
- Confidence: [0-100%]
- If true, next step would be: [What to check/do]

**Hypothesis 2**: [State the hypothesis]
- Evidence FOR:
- Evidence AGAINST:
- Confidence:
- If true, next step would be:

**Hypothesis 3**: [State the hypothesis]
- Evidence FOR:
- Evidence AGAINST:
- Confidence:
- If true, next step would be:

=== CONVERGENCE ===
**Most Likely Root Cause**: Based on the evidence, which hypothesis best explains the failure and why?
**Recommended Actions**: Ordered by priority.
**What to Verify**: What additional information would confirm the root cause?
""",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# REACT PATTERN PROMPT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REACT_TROUBLESHOOTING = PromptTemplate(
    name="react_troubleshooting",
    description="ReAct pattern for interactive troubleshooting — alternating reasoning and actions",
    technique="react",
    variables=["error_message", "pipeline_name", "available_tools", "context"],
    template="""You are troubleshooting a pipeline failure using the ReAct approach. Alternate between Thought, Action, and Observation.

=== FAILURE ===
Pipeline: {{pipeline_name}}
Error: {{error_message}}

=== AVAILABLE ACTIONS ===
{{available_tools}}

=== EXISTING CONTEXT ===
{{context}}

=== TROUBLESHOOTING PROCEDURE ===
Follow this pattern. Stop when you have enough information to provide a resolution.

Thought 1: [What do I know? What do I need to find out?]
Action 1: [Which tool/query should I use? What inputs?]
Observation 1: [What did the action return?]

Thought 2: [What does this tell me? What's still unclear?]
Action 2: [Next investigation step]
Observation 2: [Results]

... continue until resolution ...

Final Thought: [Synthesize all observations into a root cause and resolution]

=== OUTPUT ===
**Root Cause**: [Clear explanation]
**Resolution Steps**: [Numbered steps]
**Verification**: [How to confirm the fix worked]
""",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ALERT ROUTING PROMPTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ALERT_ROUTING = PromptTemplate(
    name="alert_routing",
    description="Classify and route an alert to the correct team/channel",
    technique="zero_shot",
    variables=["failure_summary", "ownership_map", "severity"],
    template="""Based on the failure details and ownership mapping, determine the correct routing for this alert.

=== FAILURE SUMMARY ===
{{failure_summary}}

=== PIPELINE OWNERSHIP MAP ===
{{ownership_map}}

=== CURRENT SEVERITY ===
{{severity}}

=== ROUTING DECISION ===
Determine:
1. **Target Team/Channel**: Which team owns this pipeline? Which channel should receive the alert?
2. **Contacts to Notify**: Who should be directly notified?
3. **Severity Adjustment**: Should the severity be upgraded/downgraded based on the error? Justify.
4. **Auto-Escalation**: Should this auto-escalate if not acknowledged within the SLA? (yes/no)
5. **Routing Confidence**: How confident are you in this routing? (0-100%)

Output as JSON:
{
    "target_channel": "...",
    "target_contacts": ["..."],
    "severity": "...",
    "auto_escalate": true/false,
    "routing_reason": "...",
    "confidence": 0.0
}
""",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SEVERITY CLASSIFICATION PROMPT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SEVERITY_CLASSIFIER = PromptTemplate(
    name="severity_classifier",
    description="Classify alert severity with self-consistency check",
    technique="self_consistency",
    variables=["error_message", "pipeline_name", "pipeline_metadata", "blast_radius"],
    template="""Classify the severity of this pipeline failure. Use three independent assessments and take the consensus.

=== FAILURE ===
Pipeline: {{pipeline_name}}
Error: {{error_message}}
Pipeline Metadata: {{pipeline_metadata}}
Estimated Blast Radius: {{blast_radius}}

=== SEVERITY DEFINITIONS ===
- CRITICAL: Production data loss, customer-facing impact, SLA breach imminent, multiple pipelines affected
- HIGH: Single critical pipeline down, data freshness SLA at risk, no data loss but delayed delivery
- MEDIUM: Non-critical pipeline failure, retry likely to succeed, limited downstream impact
- LOW: Warning-level issue, non-blocking, informational, known intermittent issue

=== ASSESSMENT 1 (Impact-Based) ===
Focus on: What is the business impact? Who is affected?
Severity: [critical/high/medium/low]
Reasoning: [...]

=== ASSESSMENT 2 (Technical-Based) ===
Focus on: How severe is the technical failure? Can it self-heal?
Severity: [critical/high/medium/low]
Reasoning: [...]

=== ASSESSMENT 3 (Historical-Based) ===
Focus on: Is this a known issue? How was it classified before?
Severity: [critical/high/medium/low]
Reasoning: [...]

=== CONSENSUS ===
**Final Severity**: [Take the majority vote; if all differ, use the highest]
**Confidence**: [High if all agree, Medium if 2/3 agree, Low if all differ]
**Justification**: [One sentence explaining the final decision]
""",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONVERSATION / Q&A PROMPTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CONVERSATIONAL_QA = PromptTemplate(
    name="conversational_qa",
    description="Conversational Q&A over alert context and knowledge base",
    technique="zero_shot",
    variables=["user_question", "alert_context", "rag_context", "conversation_history"],
    template="""Answer the support engineer's question using the available context. Be precise and actionable.

=== CONVERSATION HISTORY ===
{{conversation_history}}

=== CURRENT ALERT CONTEXT ===
{{alert_context}}

=== KNOWLEDGE BASE CONTEXT (Retrieved) ===
{{rag_context}}

=== SUPPORT ENGINEER'S QUESTION ===
{{user_question}}

=== INSTRUCTIONS ===
1. Answer based ONLY on the provided context. If the context doesn't contain the answer, say so explicitly.
2. If referencing a past incident, cite the incident ID.
3. If referencing a runbook, include the link.
4. If the question asks about trends/patterns, provide specific numbers from the data.
5. If you're uncertain, state your confidence level and what additional info would help.
6. Keep your answer focused and under 300 words unless a detailed walkthrough is requested.
""",
)


SIMILAR_INCIDENTS_QA = PromptTemplate(
    name="similar_incidents_qa",
    description="Answer questions about similar historical incidents",
    technique="few_shot",
    variables=["user_question", "similar_incidents", "current_alert"],
    template="""The support engineer is asking about similar past incidents. Answer using the incident data provided.

=== CURRENT ALERT ===
{{current_alert}}

=== SIMILAR HISTORICAL INCIDENTS ===
{{similar_incidents}}

=== QUESTION ===
{{user_question}}

=== INSTRUCTIONS ===
- Compare the current alert with each similar incident
- Highlight what's the same and what's different
- If a past incident has a known resolution, emphasize it
- Mention time-to-resolution for past incidents when available
- Recommend the most relevant past fix for the current situation
""",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LOG PARSING PROMPT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

LOG_PARSER = PromptTemplate(
    name="log_parser",
    description="Parse noisy logs into concise root cause summaries",
    technique="chain_of_thought",
    variables=["raw_logs", "pipeline_name", "expected_behavior"],
    template="""Parse these raw logs and extract the key failure information. The logs may be noisy — focus on what matters.

=== PIPELINE ===
{{pipeline_name}}

=== EXPECTED BEHAVIOR ===
{{expected_behavior}}

=== RAW LOGS ===
<<<LOGS>>>
{{raw_logs}}
<<<END_LOGS>>>

=== ANALYSIS STEPS ===

**Step 1 — Filter Noise**: Identify which log lines are ERROR/FATAL vs. routine INFO/WARN.

**Step 2 — Find the First Error**: What was the initial failure? (Not the cascade of subsequent errors)

**Step 3 — Extract Key Details**:
- Exception class/type:
- Error message (one line):
- Stack trace root (deepest relevant frame):
- Timestamp of first error:
- Affected component/service:

**Step 4 — Summarize in Plain English**:
Write a 2-3 sentence summary that a non-technical manager could understand.

=== OUTPUT FORMAT ===
```
ERROR_CLASS: [...]
FIRST_ERROR_TIME: [...]
ROOT_MESSAGE: [One-line error summary]
PLAIN_ENGLISH: [2-3 sentence non-technical summary]
AFFECTED_COMPONENT: [...]
LOG_LINES_RELEVANT: [Count of relevant error lines out of total]
```
""",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# NOTIFICATION SUMMARY PROMPT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TEAMS_NOTIFICATION = PromptTemplate(
    name="teams_notification",
    description="Generate a structured Teams notification for an alert",
    technique="zero_shot",
    variables=["failure_summary", "root_cause", "severity", "routing", "similar_incidents", "actions"],
    template="""Generate a concise Microsoft Teams notification for this pipeline alert.

=== ALERT DATA ===
Failure: {{failure_summary}}
Root Cause: {{root_cause}}
Severity: {{severity}}
Routed To: {{routing}}
Similar Past Incidents: {{similar_incidents}}
Available Actions: {{actions}}

=== FORMAT REQUIREMENTS ===
The notification must be:
- Scannable in under 10 seconds
- Lead with severity emoji and pipeline name
- Include a one-line root cause
- List 2-3 immediate action items with links
- Mention similar incidents count if > 0
- Include timestamp (UTC)

=== OUTPUT (Markdown for Teams) ===
""",
)



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# P2: COLD-START PROMPT (no active alert context)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

COLD_START_PROMPT = PromptTemplate(
    name="cold_start",
    description="Cold-start routing when no alert context exists — guides LLM to ask clarifying questions or suggest discovery queries",
    technique="zero_shot",
    variables=["user_question", "conversation_history"],
    template="""The user has started a session WITHOUT selecting a specific alert. There is no active pipeline failure context.

=== CONVERSATION HISTORY ===
{{conversation_history}}

=== USER QUESTION ===
{{user_question}}

=== YOUR ROLE IN COLD-START MODE ===
You are Alert Whisperer operating without an active alert context. Your goals are:
1. Help the user discover what is currently broken or degraded in the platform
2. Answer general knowledge questions about Spark, Synapse, Kusto, or ADF operations
3. Guide the user to provide enough context for a focused investigation

=== COLD-START RESPONSE STRATEGY ===

**If the question is about a specific failure** (error message, pipeline name, or log snippet provided):
- Attempt to analyze based on the information given
- Ask for 1-2 specific missing pieces: pipeline type (Spark/Synapse/Kusto/ADF), error message, or timestamp
- Suggest: "If you select the alert from the feed, I'll have full context for a deeper analysis"

**If the question is exploratory** ("what's broken?", "any issues?"):
- Suggest these discovery queries the user can ask:
  * "What failed in the last hour?"
  * "Show me all critical alerts"
  * "Is [pipeline_name] healthy?"
  * "What are the top recurring errors this week?"
- Offer to explain common Spark/Synapse/Kusto failure patterns if useful

**If the question is conceptual** ("how does X work?", "what causes Y?"):
- Answer directly using your Spark/Synapse/Kusto/ADF domain knowledge
- Keep the answer focused and actionable for an on-call engineer

=== CLARIFYING QUESTIONS (ask at most 2) ===
If context is insufficient, ask the most impactful clarifying questions:
- What platform/technology is involved? (Azure Databricks, Synapse, Kusto/ADX, ADF)
- What is the error message or failure symptom?
- Is this a new issue or a recurrence?
- What is the approximate time window?

=== OUTPUT ===
Provide a helpful response that either answers the question, guides discovery, or asks targeted clarifying questions.
""",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# REFLEXION PATTERN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REFLEXION_ANALYSIS = PromptTemplate(
    name="reflexion_analysis",
    description="Reflexion self-refinement loop for iterative troubleshooting improvement",
    technique="reflexion",
    variables=["initial_analysis", "error_context", "feedback_signals"],
    template="""Review your initial analysis and improve it through self-reflection.

=== INITIAL ANALYSIS ===
{{initial_analysis}}

=== ERROR CONTEXT ===
{{error_context}}

=== FEEDBACK SIGNALS ===
{{feedback_signals}}

=== SELF-REFLECTION PROCEDURE ===

**Step 1 — Critique your initial analysis (general dimensions):**
- What did I get right?
- What did I miss or get wrong?
- Are there alternative explanations I didn't consider?
- Is my severity assessment calibrated correctly?
- Did I recommend specific enough actions?

**Step 2 — P4: Domain-specific evaluation rubric (Spark / Synapse / Kusto):**

For Spark / Databricks failures:
- Does the analysis correctly identify the Spark stage boundary where the failure occurred?
- Did I distinguish between driver failures (driver OOM, NPE in planning) vs. executor failures (task OOM, shuffle errors)?
- If I recommended increasing executor memory, is the suggested value within the valid range (2–64 GiB)?
- Did I check whether partition skew (one partition >> others) could explain the OOM or timeout?
- If shuffle spill is involved, did I recommend increasing spark.sql.shuffle.partitions or salting?
- For Structured Streaming failures, did I verify the checkpoint directory and offsets?

For Synapse / ADF failures:
- Did I correctly identify the Integration Runtime type (Azure IR, SHIR, Azure-SSIS) and its relevance?
- Did I distinguish between Dedicated SQL Pool and Serverless SQL Pool failure modes?
- For Copy Activity failures, did I distinguish data-plane errors (path/format) from control-plane errors (IR, auth)?
- Did I consider whether the pipeline has dependency condition mismatches (e.g., activity running on Skipped instead of Succeeded)?

For Kusto / ADX failures:
- Did I correctly identify whether the error code prefix is Permanent_ (no auto-retry required) or Transient_ (may self-heal)?
- Are the KQL query patterns syntactically valid (pipe operators, aggregation functions)?
- For ingestion failures, did I check the mapping name, schema alignment, and ingestion policy?
- Did I consider streaming ingestion vs. queued ingestion latency differences?

**Step 3 — Identify improvements:**
- List each specific improvement to make
- Explain why each improvement is needed

**Step 4 — Generate refined analysis:**
Produce an improved version incorporating all improvements.

=== OUTPUT FORMAT ===
**Critique:** [Your self-assessment, including domain-specific rubric findings]
**Improvements:** [Numbered list of changes]
**Refined Analysis:** [Complete improved analysis]
**Confidence Change:** [How did your confidence change and why?]
""",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STEP-BACK PROMPTING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP_BACK_ANALYSIS = PromptTemplate(
    name="step_back_analysis",
    description="Step-back prompting for ambiguous errors — abstract first, then solve",
    technique="step_back",
    variables=["error_message", "pipeline_name", "pipeline_type", "context"],
    template="""This error is ambiguous. Before diving into specifics, step back and reason about the general principles first.

=== AMBIGUOUS ERROR ===
Pipeline: {{pipeline_name}}
Type: {{pipeline_type}}
Error: {{error_message}}

=== CONTEXT ===
{{context}}

=== P5: HIGH-LEVEL CONCEPTUAL ANCHORS ===
Before reasoning about the specific error, recall the relevant architectural concepts:

Spark Execution Model:
- Jobs → Stages → Tasks hierarchy; stage boundaries occur at wide transformations (shuffle, sort, repartition)
- Memory regions: on-heap storage (caching), on-heap execution (shuffle/sort), off-heap (Tungsten)
- Executor memory = spark.executor.memory; overhead = max(384MB, 10% of executor memory)
- Partition skew: one partition orders of magnitude larger than median → stragglers / OOM
- AQE (Adaptive Query Execution): can coalesce shuffle partitions and handle skew joins dynamically

Synapse Architecture:
- Serverless SQL Pool: stateless, no distributed transactions, no temp tables across sessions
- Dedicated SQL Pool: DWU-based; distributions determine parallelism; stats affect plan quality
- Integration Runtime bandwidth and version determine throughput; SHIR version lag causes auth failures
- Spark Pools are cluster-per-session by default; warm pool reuse reduces cold start

Kusto / ADX Architecture:
- Ingestion pipeline: client → Data Management cluster (DM) → Engine cluster; DM handles batching
- Queued ingestion: eventual consistency (typically <5 min); streaming: <10s but uses row store
- Extent (shard): immutable data unit; merging extents is background; large extent count = merge pressure
- KQL query lifecycle: parse → semantic analysis → distribute to extents → merge results

ADF / Synapse IR Concepts:
- Azure IR: Microsoft-managed; region selection affects latency and data residency
- Self-hosted IR: customer-managed VM; network proxy, TLS inspection, and firewall rules are common failure points
- Trigger types: Schedule (wall-clock), Tumbling Window (backfill-aware), Event-based (low-latency), Storage Event

=== STEP-BACK REASONING ===

**Step 1 — Abstract the Problem:**
What GENERAL category of problem is this? Don't focus on the specific error yet.
(e.g., "resource exhaustion", "dependency failure", "configuration drift", "data quality issue",
"Spark stage-level memory pressure", "Kusto ingestion mapping mismatch", "Synapse IR connectivity")

**Step 2 — Identify General Principles:**
For this category of problem, what are the universal troubleshooting principles?
- What are the top 3 most common root causes for this category in Spark/Synapse/Kusto?
- What are the standard diagnostic steps for this technology?
- What information is typically needed to diagnose?

**Step 3 — Apply Principles to Specific Error:**
Now apply the general principles to THIS specific error:
- Which of the general root causes best fits the evidence?
- What specific diagnostic steps should we take (e.g., check Spark UI stage details, KQL ingestion status, Synapse activity logs)?
- What additional information would narrow down the root cause?

**Step 4 — Synthesize:**
Combine the abstract reasoning with specific evidence to produce an actionable analysis.

=== OUTPUT ===
**Problem Category:** [General category, with technology context e.g. "Spark executor memory pressure"]
**Applicable Principles:** [Key principles from the conceptual anchors above]
**Specific Analysis:** [Analysis of this specific error]
**Recommended Investigation Steps:** [Ordered diagnostic steps with technology-specific commands/queries]
""",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CHAIN-OF-VERIFICATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CHAIN_OF_VERIFICATION = PromptTemplate(
    name="chain_of_verification",
    description="Chain-of-Verification for factual safety — verify each claim in the analysis",
    technique="chain_of_verification",
    variables=["analysis_to_verify", "available_evidence", "alert_context"],
    template="""Verify the factual accuracy of this analysis by checking each claim against the evidence.

=== ANALYSIS TO VERIFY ===
{{analysis_to_verify}}

=== AVAILABLE EVIDENCE ===
{{available_evidence}}

=== ALERT CONTEXT ===
{{alert_context}}

=== VERIFICATION PROCEDURE ===

**Step 1 — Extract Claims:**
List every factual claim made in the analysis. Number each claim.

**Step 2 — Verify Each Claim:**
For each claim:
- CLAIM: [The claim]
- EVIDENCE: [What evidence supports or contradicts this?]
- VERDICT: [VERIFIED / UNVERIFIED / CONTRADICTED]
- CORRECTION: [If contradicted, what is the correct information?]

**Step 3 — P3: Domain-specific verification checklist:**
Apply these technology-specific checks BEFORE producing the verified analysis:

Spark / Databricks:
[ ] Is any suggested executor memory value within valid range (2–64 GiB)?
[ ] Is the identified stage number consistent with the log evidence (check "stage X.Y" in stack trace)?
[ ] If OOM is claimed, does the log show GC overhead limit exceeded or java.lang.OutOfMemoryError: Java heap space?
[ ] If shuffle spill is claimed, does the log mention spill metrics (shuffle spill disk, shuffle spill memory)?
[ ] If partition skew is claimed, is there evidence of uneven task durations or skewed partition sizes?
[ ] Are any Spark config values cited (e.g., spark.executor.memory=8g) syntactically valid?

Synapse / ADF:
[ ] Is the Integration Runtime type (Azure IR vs SHIR) correctly identified from the error context?
[ ] Are Synapse error codes (UserErrorInvalidFolderPath, UserErrorAccessForbidden) used correctly?
[ ] If a Dedicated SQL Pool claim is made, is the DWU/distribution model relevant?
[ ] If a Serverless SQL Pool claim is made, does the error context match serverless behavior?

Kusto / ADX:
[ ] Is the ingestion error prefix correctly classified as Permanent_ (requires manual fix) or Transient_ (may auto-retry)?
[ ] If a KQL query is cited, is the syntax valid (pipes, correct operator names, proper aggregation)?
[ ] If an ingestion mapping is mentioned, does the name match what appears in the error message?
[ ] If latency is discussed, are streaming (<10s) vs queued (~5min) distinctions accurate?

**Step 4 — Produce Verified Analysis:**
Rewrite the analysis with:
- Verified claims kept as-is
- Unverified claims marked with [UNVERIFIED]
- Contradicted claims corrected with [CORRECTED]
- Domain checklist failures annotated with [DOMAIN-CHECK FAILED: reason]

**Step 5 — Confidence Assessment:**
What percentage of claims were verified? How reliable is this analysis?

=== OUTPUT ===
**Claims Verified:** [X of Y claims verified]
**Corrections Made:** [List of corrections]
**Verified Analysis:** [Corrected and verified version]
**Reliability Score:** [0-100%]
""",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FEW-SHOT EXAMPLES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CURATED_FEW_SHOT_EXAMPLES: list[FewShotExample] = [
    FewShotExample(
        input_text=(
            "Pipeline: spark_etl_daily_ingest\n"
            "Error: java.lang.OutOfMemoryError: Java heap space\n"
            "Logs: 24/03/15 02:15:33 ERROR Executor: Exception in task 47.3 in stage 12.0\n"
            "java.lang.OutOfMemoryError: Java heap space\n"
            "at java.util.Arrays.copyOf(Arrays.java:3236)\n"
            "at org.apache.spark.sql.catalyst.expressions.UnsafeRow.copy(UnsafeRow.java:531)"
        ),
        output_text=(
            "**Error Classification**: OutOfMemoryError\n\n"
            "**Root Cause Summary**: The Spark executor ran out of heap memory during a large shuffle operation "
            "in stage 12. This typically happens when a join or aggregation produces a data skew, causing one "
            "partition to be significantly larger than others.\n\n"
            "**Technical Details**: Task 47 in stage 12 failed with OOM while copying UnsafeRow data during "
            "a shuffle. The failure occurred at 02:15 UTC after 3 retries (task attempt 47.3). The stack trace "
            "points to UnsafeRow.copy, indicating the executor's memory was exhausted during data serialization.\n\n"
            "**Severity Assessment**: HIGH — The daily ingest pipeline is down, causing data freshness SLA risk "
            "for downstream consumers. No data loss (can rerun), but delay is accumulating.\n\n"
            "**Affected Components**: spark_etl_daily_ingest, downstream analytics tables, daily reporting."
        ),
        explanation="OOM during shuffle is a common Spark failure. Key indicators: UnsafeRow.copy in stack, "
                    "high task attempt number (47.3 = 3 retries), and the failure in a late stage (12).",
    ),
    FewShotExample(
        input_text=(
            "Pipeline: synapse_pipeline_customer360\n"
            "Error: ErrorCode=UserErrorInvalidFolderPath, message: The folder path does not exist or is empty\n"
            "Logs: Activity 'Copy_CustomerData' failed: Folder 'raw/customers/2024/03/16/' not found in ADLS"
        ),
        output_text=(
            "**Error Classification**: DataQualityError\n\n"
            "**Root Cause Summary**: The Synapse pipeline's Copy activity failed because the expected source "
            "folder in ADLS Gen2 doesn't exist. This indicates that the upstream data producer (likely a data "
            "export job) hasn't written data for today's date partition yet.\n\n"
            "**Technical Details**: The Copy_CustomerData activity expects data at "
            "'raw/customers/2024/03/16/' but the folder hasn't been created. This is likely a timing issue — "
            "the pipeline ran before the upstream data landed. Error code UserErrorInvalidFolderPath confirms "
            "the path doesn't exist (not a permission issue).\n\n"
            "**Severity Assessment**: MEDIUM — The customer360 pipeline will succeed on retry once upstream "
            "data lands. No data loss, just a timing dependency. Consider adding a wait-for-data pattern.\n\n"
            "**Affected Components**: synapse_pipeline_customer360, Customer 360 dashboard, CRM data feed."
        ),
        explanation="Missing folder path errors in Synapse are typically timing issues between upstream "
                    "producers and downstream consumers. The key insight is distinguishing this from a "
                    "permission error (which would be a different error code).",
    ),
    FewShotExample(
        input_text=(
            "Pipeline: kusto_ingestion_telemetry\n"
            "Error: Permanent_MappingNotFound: Ingestion mapping 'telemetry_v2_mapping' not found\n"
            "Logs: Ingestion from EventHub partition 3 failed. ErrorCode: Permanent_MappingNotFound"
        ),
        output_text=(
            "**Error Classification**: ConfigurationError\n\n"
            "**Root Cause Summary**: Kusto ingestion is failing because the expected data mapping "
            "'telemetry_v2_mapping' doesn't exist in the target table. This usually happens after a schema "
            "change where the mapping was updated/renamed but the ingestion connection still references the old name.\n\n"
            "**Technical Details**: The EventHub streaming ingestion references mapping 'telemetry_v2_mapping' "
            "but this mapping doesn't exist on the Kusto table. This is a Permanent error (won't auto-retry). "
            "All partitions feeding this table are likely affected, causing complete ingestion stoppage.\n\n"
            "**Severity Assessment**: CRITICAL — Streaming ingestion is fully blocked. Telemetry data is being "
            "dropped (EventHub retention will eventually expire unprocessed events). Every minute of delay means "
            "permanent data loss.\n\n"
            "**Affected Components**: kusto_ingestion_telemetry, all telemetry dashboards, real-time alerting, "
            "SLA monitoring that depends on telemetry freshness."
        ),
        explanation="Permanent_ prefix in Kusto errors means no auto-retry — human intervention required. "
                    "Mapping errors after schema changes are a common operational issue.",
    ),
]


def format_few_shot_examples(examples: list[FewShotExample]) -> str:
    """Format few-shot examples into a string for prompt injection."""
    parts = []
    for i, ex in enumerate(examples, 1):
        parts.append(
            f"--- Example {i} ---\n"
            f"INPUT:\n{ex.input_text}\n\n"
            f"OUTPUT:\n{ex.output_text}\n"
        )
        if ex.explanation:
            parts.append(f"(Why: {ex.explanation})\n")
    return "\n".join(parts)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TEMPLATE REGISTRY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# P9: Audit — all defined PromptTemplate objects must appear here.
# P10: Also register all templates into the version registry for A/B testing support.
TEMPLATE_REGISTRY: dict[str, PromptTemplate] = {
    "root_cause_zero_shot": ROOT_CAUSE_ZERO_SHOT,
    "root_cause_few_shot": ROOT_CAUSE_FEW_SHOT,
    "root_cause_cot": ROOT_CAUSE_CHAIN_OF_THOUGHT,
    "root_cause_tree_of_thought": ROOT_CAUSE_TREE_OF_THOUGHT,
    "react_troubleshooting": REACT_TROUBLESHOOTING,
    "alert_routing": ALERT_ROUTING,
    "severity_classifier": SEVERITY_CLASSIFIER,
    "conversational_qa": CONVERSATIONAL_QA,
    "similar_incidents_qa": SIMILAR_INCIDENTS_QA,
    "log_parser": LOG_PARSER,
    "teams_notification": TEAMS_NOTIFICATION,
    "cold_start": COLD_START_PROMPT,          # P2: cold-start template
    "reflexion_analysis": REFLEXION_ANALYSIS,
    "step_back_analysis": STEP_BACK_ANALYSIS,
    "chain_of_verification": CHAIN_OF_VERIFICATION,
}

# P10: Populate version registry from TEMPLATE_REGISTRY at module load time.
# Each template starts at version 1. Additional versions can be registered at
# runtime via register_template_version() for A/B experiments.
for _tmpl in TEMPLATE_REGISTRY.values():
    register_template_version(_tmpl)


def get_template(name: str) -> PromptTemplate:
    """Retrieve a prompt template by name."""
    if name not in TEMPLATE_REGISTRY:
        available = ", ".join(TEMPLATE_REGISTRY.keys())
        raise KeyError(f"Template '{name}' not found. Available: {available}")
    return TEMPLATE_REGISTRY[name]
