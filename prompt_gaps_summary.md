# Prompt Engineering Gaps P1–P12 — Implementation Summary

**Date:** 2026-03-09  
**Implemented by:** Subagent (automated)

All 12 production gaps have been implemented. All 4 files pass `ast.parse()` syntax validation.

---

## Files Modified

| File | Lines Before | Changes |
|------|-------------|---------|
| `src/prompts/templates.py` | 804 | P1, P2, P3, P4, P5, P6, P9, P10 |
| `src/prompts/prompt_engine.py` | 773 | P2 (routing), P7, P8, P11, P12 |
| `src/engine/chat_engine.py` | 1306 | P2 (cold-start dispatch in `_handle_general_qa`) |
| `config/settings.py` | 194 | P8, P11, P12 (new config fields in `LLMSettings`) |

---

## Gap Implementations

### P1 — System Prompt Domain Grounding
**File:** `src/prompts/templates.py` — `GLOBAL_KNOWLEDGE_PROMPT`

Replaced generic preamble with a structured `PRIMARY TECHNOLOGY STACK` section that covers:
- **Apache Spark / Azure Databricks:** Jobs→Stages→Tasks model, memory regions (on-heap storage/execution/off-heap), valid executor memory range (2–64 GiB), Delta Lake, Structured Streaming, AQE, key config knobs.
- **Azure Synapse Analytics:** Pipeline orchestration, IR types (Azure IR / SHIR / Azure-SSIS), Dedicated vs Serverless SQL Pool, Copy Activity error codes, Data Flow Spark backend.
- **Azure Data Explorer (Kusto / ADX):** KQL, ingestion paths (queued vs streaming), `Permanent_` vs `Transient_` error prefixes, materialized views, cluster scaling, ADX Follower databases.
- **Azure Data Factory (ADF):** IR types, linked services credential patterns, trigger types, Mapping Data Flows.
- **Supporting Azure Infrastructure:** ADLS Gen2, Key Vault secret rotation, Managed Identity, Azure Monitor/Log Analytics, Private Endpoints DNS issues.

Added 3 new behavioral guidelines:
- Validate Spark config values against known valid ranges.
- Flag `Permanent_` Kusto errors (no auto-retry) vs `Transient_` (may self-heal).
- Identify relevant IR type in Synapse failures.

---

### P2 — Cold-Start Routing
**Files:** `src/prompts/templates.py`, `src/prompts/prompt_engine.py`, `src/engine/chat_engine.py`

**`COLD_START_PROMPT`** template added to `templates.py`:
- Registered in `TEMPLATE_REGISTRY` under `"cold_start"`.
- Variables: `user_question`, `conversation_history`.
- Three routing branches: (a) specific failure info → analyze + ask 1–2 targeted questions, (b) exploratory query → suggest discovery commands, (c) conceptual question → answer directly from domain knowledge.
- Lists up to 4 clarifying questions (technology, error message, new vs recurrence, time window).

**`build_cold_start_prompt()`** added to `PromptEngine` — builds `[system, user]` message list using the template.

**`_handle_general_qa()`** in `ChatEngine` now detects cold-start condition (`active_alert is None` and no `triage_engine`) and dispatches to `build_cold_start_prompt` instead of generic RAG Q&A. Logs `cold_start_routing_applied` event.

---

### P3 — Domain-Specific CoVe Verification Dimensions
**File:** `src/prompts/templates.py` — `CHAIN_OF_VERIFICATION`

Inserted **Step 3 — Domain-specific verification checklist** between the per-claim VERDICT loop and the verified-analysis rewrite step.

Checklist items by technology:
- **Spark:** executor memory range (2–64 GiB), stage number consistency, OOM log evidence, shuffle spill metrics, partition skew evidence, Spark config syntax.
- **Synapse/ADF:** IR type identification, Synapse error codes, Dedicated vs Serverless SQL Pool distinction.
- **Kusto/ADX:** `Permanent_` vs `Transient_` classification, KQL syntax validity, ingestion mapping name match, streaming vs queued latency accuracy.

Annotations `[DOMAIN-CHECK FAILED: reason]` added to Step 4 output format.

---

### P4 — Reflexion Domain-Specific Rubric
**File:** `src/prompts/templates.py` — `REFLEXION_ANALYSIS`

Added **Step 2 — Domain-specific evaluation rubric** to `SELF-REFLECTION PROCEDURE` with criteria for:
- **Spark/Databricks:** stage boundary identification, driver vs executor failure distinction, memory value range validation, partition skew check, shuffle spill recommendation, Structured Streaming checkpoint verification.
- **Synapse/ADF:** IR type correctness, Dedicated vs Serverless SQL Pool distinction, Copy Activity data-plane vs control-plane error distinction, dependency condition mismatches.
- **Kusto/ADX:** `Permanent_`/`Transient_` prefix classification, KQL syntax validity, ingestion mapping/schema/policy check, streaming vs queued latency.

Output format updated to reference domain-specific rubric findings.

---

### P5 — Step-Back Conceptual Anchors
**File:** `src/prompts/templates.py` — `STEP_BACK_ANALYSIS`

Added **`P5: HIGH-LEVEL CONCEPTUAL ANCHORS`** section before the step-back reasoning steps, covering:
- **Spark Execution Model:** Job→Stage→Task hierarchy, memory regions, AQE, partition skew.
- **Synapse Architecture:** Serverless vs Dedicated pool, IR bandwidth/version, Spark Pool warm reuse.
- **Kusto/ADX Architecture:** DM→Engine ingestion pipeline, queued vs streaming consistency, extents, KQL lifecycle.
- **ADF/Synapse IR Concepts:** Azure IR, SHIR (firewall/TLS/proxy failure points), trigger types.

Step 1, 2, 3 updated to reference the specific technology context (e.g., "check Spark UI stage details, KQL ingestion status, Synapse activity logs") and the output format now requests technology-specific commands/queries.

---

### P6 — Few-Shot Examples in Root Cause Analysis
**File:** `src/prompts/templates.py` — `ROOT_CAUSE_FEW_SHOT`

Added **`P6: WORKED EXAMPLES — COMMON SPARK/SYNAPSE FAILURE PATTERNS`** section as inline few-shot examples at the top of the template:
- **Example A — Spark OOM (executor heap):** UnsafeRow.copy stack trace, high task attempt number (.2), executor memory 8g, skew diagnosis, AQE recommendation.
- **Example B — Spark Shuffle Spill → TimeoutException:** 38.7 GB disk spill, `spark.sql.shuffle.partitions` too low, broadcast join failure, recommendations (increase partitions to 2000–4000, enable AQE).
- **Example C — Spark Partition Skew (NULL key):** 7,000× skew ratio, NULL user_id concentration, `spark.sql.adaptive.skewJoin.enabled=true`, `COALESCE(user_id, uuid())` workaround.

Dynamic `{{few_shot_examples}}` variable still rendered below as "ADDITIONAL USER-PROVIDED EXAMPLES".

---

### P7 — ToolRegistry Class
**File:** `src/prompts/prompt_engine.py`

Two new classes added before `DSPyPromptOptimizer`:

**`ToolDefinition(BaseModel)`** — Pydantic model for tool metadata: `name`, `description`, `parameters` (dict), `returns`.

**`ToolRegistry`** — Tool execution registry with:
- `register(definition, fn)` — register a tool and its async/sync callable.
- `get_tools_description()` — format all tools for ReAct prompt injection.
- `execute(tool_name, **kwargs)` — dispatch to callable; handles `asyncio.iscoroutinefunction`, catches exceptions → returns `[ToolError]` string.
- `_register_defaults()` — registers 4 stub tools:
  - `query_kusto(query, database?)` — ADX KQL query stub.
  - `search_confluence(query, space_key?)` — Confluence search stub.
  - `check_icm(incident_id?, pipeline_name?)` — ICM lookup stub.
  - `query_log_analytics(query, timespan?)` — Log Analytics KQL stub.

**`DEFAULT_TOOL_REGISTRY`** singleton created at module level.

`PromptEngine.__init__` now accepts `tool_registry: Optional[ToolRegistry] = None`, defaulting to `DEFAULT_TOOL_REGISTRY`.

---

### P8 — Normalized DSPy Temperatures
**Files:** `src/prompts/prompt_engine.py`, `config/settings.py`

**`DSPyPromptOptimizer.__init__`** now accepts `default_temperature: float = 0.1`. All 8 previously-hardcoded temperature values (0.1, 0.2, 0.3) replaced with `self._default_temperature` (or `t = self._default_temperature` alias at init time).

**`PromptEngine.__init__`** passes `default_temperature` to `DSPyPromptOptimizer(default_temperature=default_temperature)`, ensuring both share the same value.

**`config/settings.py`** — Added to `LLMSettings`:
```python
dspy_optimization_temperature: float = Field(default=0.1, ...)
timeout_seconds: float = Field(default=30.0, ...)
conversation_token_budget: int = Field(default=8000, ...)
```
These are loaded from environment variables (`LLM_DSPY_OPTIMIZATION_TEMPERATURE`, `LLM_TIMEOUT_SECONDS`, `LLM_CONVERSATION_TOKEN_BUDGET`) by pydantic-settings.

---

### P9 — TEMPLATE_REGISTRY Completeness
**File:** `src/prompts/templates.py`

Audit confirmed all 14 `PromptTemplate` objects were already registered **except** `COLD_START_PROMPT` (which was added in P2). Added `"cold_start": COLD_START_PROMPT` to `TEMPLATE_REGISTRY`.

Registry now contains 15 entries:
`root_cause_zero_shot`, `root_cause_few_shot`, `root_cause_cot`, `root_cause_tree_of_thought`, `react_troubleshooting`, `alert_routing`, `severity_classifier`, `conversational_qa`, `similar_incidents_qa`, `log_parser`, `teams_notification`, **`cold_start`** _(new)_, `reflexion_analysis`, `step_back_analysis`, `chain_of_verification`.

---

### P10 — Template Versioning and A/B Testing
**File:** `src/prompts/templates.py`

Added at module level before `GLOBAL_KNOWLEDGE_PROMPT`:

```python
_TEMPLATE_VERSIONS: dict[str, list[PromptTemplate]] = {}
```

Three new functions:
- **`register_template_version(template)`** — Adds a template to `_TEMPLATE_VERSIONS[template.name]`, sorted by `template.version` string. Called automatically for every template at module load time.
- **`get_template_version(name, version=None)`** — Returns the default (lowest-version) template, or a specific version string (e.g. `"2.0-experimental"`). Raises `KeyError` if not found.
- **`list_template_versions(name)`** — Returns all registered version strings for a template name.

At the bottom of `TEMPLATE_REGISTRY`, all registered templates are auto-populated into `_TEMPLATE_VERSIONS`:
```python
for _tmpl in TEMPLATE_REGISTRY.values():
    register_template_version(_tmpl)
```

New versions for A/B experiments can be created at runtime:
```python
# Example: register a v2 experimental CoT template
register_template_version(PromptTemplate(name="root_cause_cot", version="2.0-experimental", ...))
# Select it:
get_template_version("root_cause_cot", version="2.0-experimental")
```

`PromptTemplate.version` already existed in `src/models.py` as `str = Field(default="1.0")`.

---

### P11 — Conversation Token Budget Management
**Files:** `src/prompts/prompt_engine.py`, `config/settings.py`

**`PromptEngine.__init__`** now accepts `conversation_token_budget: int = 8000` (stored as `self.conversation_token_budget`).

**`_truncate_conversation_history(messages)`** helper method added:
1. Converts `ChatMessage` list to `"Role: content"` strings.
2. Drops oldest messages in a while-loop until `sum(len(p) for p in parts) <= char_budget` (budget × 4 chars/token).
3. Prepends `[N older message(s) omitted...]` notice when messages are dropped.
4. Hard-truncates a single oversized message at `char_budget` chars.
5. Returns `"No prior conversation."` if no messages remain.

**`build_conversational_prompt`** updated to call `_truncate_conversation_history(context.get_recent_messages(50))` instead of the previous `get_recent_messages(10)` raw loop.

**`config/settings.py`** — `LLMSettings.conversation_token_budget` field added (default 8000, env var `LLM_CONVERSATION_TOKEN_BUDGET`).

---

### P12 — LLM Call Timeout Enforcement
**Files:** `src/prompts/prompt_engine.py`, `config/settings.py`

**`PromptEngine.__init__`** now accepts `llm_timeout: float = 30.0` (stored as `self.llm_timeout`).

**`invoke_llm`** signature extended with `timeout: Optional[float] = None`:
- `effective_timeout = timeout if timeout is not None else self.llm_timeout`.
- When `effective_timeout > 0`, the actual API call is wrapped: `await asyncio.wait_for(_call(), timeout=effective_timeout)`.
- On `asyncio.TimeoutError`: logs structured event `llm_invocation_timeout` with timeout seconds and message count, then re-raises with a human-readable message.
- Callers can override per-call by passing `timeout=` explicitly, or disable by passing `timeout=0`.

**`config/settings.py`** — `LLMSettings.timeout_seconds` field added (default 30.0, env var `LLM_TIMEOUT_SECONDS`). Set to 0 to disable.

---

## Syntax Verification

```
templates.py   → OK
prompt_engine.py → OK
chat_engine.py → OK
settings.py    → OK
```
All files validated with `python -c "import ast; ast.parse(open(FILE).read())"`.
