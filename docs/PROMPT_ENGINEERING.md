# Prompt Engineering Guide — Alert Whisperer

This document details every prompt engineering technique implemented in Alert Whisperer, where each is used, and the design rationale.

---

## 1. Clear Instructions

**Where:** Every template in `src/prompts/templates.py`

**Implementation:**
- Each prompt defines the exact task: "Analyze this pipeline failure and provide a root cause summary"
- Output format is specified explicitly with field labels
- Length/style constraints are stated: "2-3 sentences", "under 300 words"
- Text delimiters mark sections: `<<<ERROR>>>`, `<<<LOGS>>>`, `<<<CONTEXT>>>`

**Why:** Vague instructions lead to inconsistent outputs. By specifying format, length, and structure, we get predictable, parseable responses.

---

## 2. Persona / Role Adoption

**Where:** `GLOBAL_KNOWLEDGE_PROMPT` (system prompt)

**Implementation:**
```
"You are Alert Whisperer, an expert AI assistant specialized in data pipeline operations,
incident management, and troubleshooting for enterprise data platforms."
```

The persona includes:
- Domain expertise (Spark, Synapse, Kusto, Azure)
- Operational context (100+ pipelines, TBs daily)
- Behavioral guidelines (plain English first, concrete actions)

**Why:** Role-based prompting measurably improves task performance. An "expert data pipeline diagnostician" produces better root cause analyses than a generic assistant.

---

## 3. Adding Context (RAG)

**Where:** `RAGRetriever.retrieve_for_alert()`, `build_context_string()`

**Implementation:**
- Retrieved chunks are injected into prompts via `<<<CONTEXT>>>` delimiters
- Context is trimmed to fit the token budget (6,000 tokens default)
- Multiple retrieval strategies: error message search, pipeline + error class search, topic-guided search

**Why:** Context is the foundation of RAG. By providing relevant historical incidents, runbook excerpts, and past resolutions, we ground the LLM's analysis in real data rather than relying on parametric knowledge.

---

## 4. Task Breakdown

**Where:** `ROOT_CAUSE_CHAIN_OF_THOUGHT`, `LOG_PARSER`

**Implementation:**
The Chain-of-Thought template breaks analysis into 6 explicit steps:
1. Identify the Error Type
2. Trace the Failure Chain
3. Identify the Root Cause
4. Assess Historical Patterns
5. Determine Impact and Blast Radius
6. Recommend Actions

The Log Parser similarly decomposes: Filter Noise → Find First Error → Extract Key Details → Summarize

**Why:** Complex tasks decomposed into sub-tasks produce better results than monolithic "analyze this" prompts. Each step focuses attention on one aspect.

---

## 5. Text Delimiters

**Where:** All templates

**Implementation:**
```
<<<ERROR>>>
{{error_message}}
<<<END_ERROR>>>

<<<LOGS>>>
{{log_snippet}}
<<<END_LOGS>>>
```

**Why:** Delimiters prevent prompt injection and clearly delineate where template variables end and instructions begin. Critical when injecting user-provided log text that may contain instruction-like patterns.

---

## 6. Output Indicators

**Where:** `ROOT_CAUSE_ZERO_SHOT`, `SEVERITY_CLASSIFIER`, `ALERT_ROUTING`, `LOG_PARSER`

**Implementation:**
Each template specifies the exact output format:
- Root cause analysis: "Error Classification: [one of: OutOfMemoryError, ...]"
- Severity: "Final Severity: [critical/high/medium/low]"
- Routing: JSON schema with specific fields
- Log parsing: Labeled fields (ERROR_CLASS, ROOT_MESSAGE, PLAIN_ENGLISH)

**Why:** Explicit output indicators ensure responses are parseable by the `parse_root_cause_response()` and `parse_routing_response()` methods.

---

## 7. Zero-Shot Prompting

**Where:** `ROOT_CAUSE_ZERO_SHOT`, `ALERT_ROUTING`, `CONVERSATIONAL_QA`

**When selected:** Simple, well-known errors (OutOfMemoryError, TimeoutException) without RAG context

**Implementation:** The template provides the task, context, and output format — no examples.

**Why:** For common error patterns, the LLM's parametric knowledge is sufficient. Adding examples would consume tokens without improving quality.

---

## 8. Few-Shot Prompting

**Where:** `ROOT_CAUSE_FEW_SHOT`, `SIMILAR_INCIDENTS_QA`

**When selected:** When no RAG context is available (default fallback)

**Implementation:**
3 curated examples covering the main error categories:
1. Spark OOM (data skew) — teaches: look for UnsafeRow.copy, check task attempt count
2. Synapse missing path (timing) — teaches: distinguish from permission errors
3. Kusto mapping error (schema migration) — teaches: Permanent_ prefix means no auto-retry

**Why:** Examples teach the model our specific analysis style, terminology, and depth. Order matters — the OOM example is first because it's the most common failure type.

---

## 9. Chain-of-Thought (CoT)

**Where:** `ROOT_CAUSE_CHAIN_OF_THOUGHT`, `LOG_PARSER`

**When selected:** Rich RAG context is available (>500 chars)

**Implementation:**
6-step reasoning chain:
```
Step 1 — Identify the Error Type
Step 2 — Trace the Failure Chain
Step 3 — Identify the Root Cause
Step 4 — Assess Historical Patterns (using RAG context)
Step 5 — Determine Impact and Blast Radius
Step 6 — Recommend Actions
```

**Why:** When context is available, CoT forces the model to reason through evidence rather than jumping to conclusions. This produces more accurate root cause analyses, especially for cascading failures.

---

## 10. Tree-of-Thought (ToT)

**Where:** `ROOT_CAUSE_TREE_OF_THOUGHT`

**When selected:** Ambiguous or intermittent errors ("flaky", "sporadic", "sometimes")

**Implementation:**
- Generate 3 hypotheses
- Evaluate evidence FOR and AGAINST each
- Assign confidence scores
- Converge on the most supported hypothesis
- Identify what additional information would confirm

**Why:** Ambiguous failures have multiple plausible root causes. ToT prevents premature commitment to one explanation and surfaces the uncertainty for the operator.

---

## 11. Self-Consistency

**Where:** `SEVERITY_CLASSIFIER`

**When used:** Every severity classification

**Implementation:**
Three independent assessments from different perspectives:
1. Impact-Based: Business impact, who is affected
2. Technical-Based: Technical severity, self-healing potential
3. Historical-Based: Past classifications of similar issues

Final severity = majority vote. Confidence = high (3/3 agree), medium (2/3), low (all differ).

**Why:** Severity classification is high-stakes (determines SLA, routing, and escalation). Self-consistency reduces the chance of misclassification by checking agreement across perspectives.

---

## 12. Active Prompting

**Where:** `ChatEngine._handle_general_qa()`

**Implementation:**
After generating a response, the system checks for uncertainty markers:
```python
uncertainty_markers = ["i'm not sure", "might be", "possibly", "unclear", "cannot determine"]
if any(marker in response.lower() for marker in uncertainty_markers):
    response += "\n⚠️ Confidence Note: My answer contains some uncertainty..."
```

**Why:** Rather than silently providing low-confidence answers, active prompting transparently flags uncertainty and suggests what additional information would improve confidence. This is a feedback loop with the human operator.

---

## 13. ReAct (Reason + Act)

**Where:** `REACT_TROUBLESHOOTING` template

**Implementation:**
Alternating Thought → Action → Observation pattern:
```
Thought 1: What do I know? What do I need to find out?
Action 1: [Tool invocation]
Observation 1: [Results]
Thought 2: What does this tell me?
...
Final Thought: Synthesize into root cause and resolution
```

**Why:** ReAct is designed for interactive troubleshooting where the model needs to query external systems between reasoning steps. Each observation refines the analysis.

---

## 14. Topic Tree Retrieval

**Where:** `TopicTreeRetriever` in `src/rag/retriever.py`

**Implementation:**
4-level hierarchy:
```
Data Pipeline Errors
├── Spark Errors
│   ├── OutOfMemory
│   ├── Shuffle Failures
│   ├── Executor Lost
│   ├── Schema Mismatch
│   └── Timeouts
├── Synapse Errors
│   ├── Pipeline Timeout
│   ├── Copy Activity Failures
│   ├── SQL Pool Errors
│   └── Auth Failures
├── Kusto Errors
│   ├── Ingestion Failures
│   ├── Query Failures
│   └── Cluster Issues
└── Infrastructure
    ├── Network Connectivity
    ├── Storage Issues
    └── Auth & Secrets
```

Errors are classified into branches using keyword matching, then RAG retrieval is scoped to the relevant branch.

**Why:** Flat vector search can return tangentially related results. Topic tree navigation first narrows the search space to the relevant error category, then retrieves within that category for higher precision.

---

## 15. Q&A Over Large Document Sets

**Where:** `RAGRetriever`, `VectorStore`, `build_context_string()`

**Implementation:**
- Documents are chunked (1000 chars, 200 overlap) at indexing time
- Multi-query retrieval: error message + pipeline context + topic keywords
- Similarity threshold filtering (0.3 minimum)
- Reranking with multi-factor scoring
- Context window budgeting (trim to token limit)
- Formatted context string with source labels

**Why:** Enterprise knowledge bases (Confluence runbooks, ICM history) are too large to fit in a single prompt. Chunking + retrieval + reranking ensures only the most relevant fragments reach the LLM.

---

## 16. Reflexion (Self-Refinement)

**Where:** `PromptEngine.invoke_with_reflexion()`, `ChatEngine._handle_root_cause_detail()`, `REFLEXION_ANALYSIS` template

**When used:** Root cause analysis for critical and high-severity alerts

**Implementation:**
1. Generate an initial root cause analysis using the standard technique (CoT/ToT/etc.)
2. Pass the initial analysis back through a Reflexion template that prompts self-critique
3. The model identifies what it got right, what it missed, and what to improve
4. A refined analysis is generated incorporating the improvements
5. Process repeats up to `max_iterations` (default: 1 for latency)

```python
refined, steps = await self.prompt_engine.invoke_with_reflexion(
    messages, session.active_alert, max_iterations=1
)
```

**Why for Spark/Synapse/Kusto context:**
- First-pass OOM diagnoses often miss the distinction between driver OOM (different fix) and executor OOM (data skew fix). Reflexion catches this on the self-critique pass.
- Initial Kusto ingestion error analyses may suggest generic "check the mapping" advice. Reflexion refines this to "check if the mapping was renamed during the recent schema migration."
- Cascading failure chains (Spark → ADLS → Synapse) are complex enough that the first-pass analysis often gets the causal ordering wrong. Self-reflection corrects the timeline.
- For Sev1/Sev2 incidents where accuracy matters most, the added latency of one Reflexion iteration is worth the improved diagnostic quality.

---

## 17. FLARE (Forward-Looking Active Retrieval)

**Where:** `FLARERetriever` in `src/rag/retriever.py`, `ChatEngine.process_message()`

**When used:** Post-generation — after every response, checks for low-confidence segments

**Implementation:**
1. After generating a response, scan for hedging language ("might be", "possibly", "I'm not sure")
2. Extract the surrounding context for each low-confidence segment
3. Retrieve additional documents for those segments
4. Append supplemental context to the response

```python
if self.rag_retriever.flare:
    flare_result = await self.rag_retriever.retrieve_with_flare(
        generated_text=response, existing_context=response,
    )
    if flare_result and flare_result.chunks:
        response += "\n\n---\nAdditional Context (retrieved via active follow-up):\n..."
```

**Why for Spark/Synapse/Kusto context:**
- Simple questions ("what's the runbook for OOM?") don't need extra retrieval — the initial context is sufficient. FLARE avoids wasting latency on unnecessary lookups.
- Complex cascading failures (Spark → ADLS → Kusto ingestion) benefit from mid-generation retrieval when the LLM realizes it needs info about a downstream component.
- Error chains in Synapse pipelines can span multiple activities; FLARE retrieves additional context for each hop in the failure chain as it encounters knowledge gaps.
- Saves ~40% of retrieval calls compared to always-retrieve approaches because most routine queries (runbook lookups, log views) don't trigger FLARE.

---

## 18. Step-Back Prompting

**Where:** `STEP_BACK_ANALYSIS` template, `ChatEngine._handle_root_cause_detail()`

**When used:** Ambiguous or intermittent errors detected via `_is_ambiguous_error()`

**Implementation:**
1. **Abstract the problem** — categorize into a general problem type before examining specifics
2. **Identify general principles** — what are the universal troubleshooting principles for this category?
3. **Apply principles to the specific error** — which general root cause best fits the evidence?
4. **Synthesize** — combine abstract reasoning with specific evidence

```python
is_ambiguous = self.prompt_engine._is_ambiguous_error(session.active_alert.error_message)
technique = "step_back" if is_ambiguous else "auto"
```

**Why for Spark/Synapse/Kusto context:**
- "Intermittent OOM on executor 3" could be caused by data skew, memory leak, competing workloads, or spot instance preemption. Step-Back first identifies this as "resource contention" (the abstract category) then systematically checks each possibility within that category.
- Synapse "random timeouts" on Copy activities could be network, source system, or pool contention. Step-Back reasoning first establishes "which layer is the bottleneck?" before diving into any specific hypothesis.
- Kusto ingestion errors that are "sporadic" often indicate throttling or partition-level issues — stepping back to ask "is this a steady-state problem or a burst problem?" immediately narrows the investigation.
- When operators say "this keeps happening randomly," they're often conflating multiple distinct failures. Step-Back disentangles the problem categories first.

---

## 19. DSPy-Style Programmatic Prompt Optimization

**Where:** `DSPyPromptOptimizer` in `src/prompts/prompt_engine.py`

**When used:** Every root cause analysis (replaces the old ad-hoc `_select_technique`)

**Implementation:**
- **Signature-based selection:** Maps (error_class, pipeline_type, context_available, is_ambiguous) → optimal technique + temperature
- **Default signatures:** Curated per error class (OOM → few_shot, Timeout → CoT, Connection → ReAct, Config → step_back)
- **Feedback loop:** `record_feedback(error_class, technique, score)` tracks performance and auto-updates signatures when enough data accumulates
- **Cache:** Results are cached by scenario key for fast repeated lookups

```python
signature = self.dspy_optimizer.get_optimal_signature(
    error_class=failure.error_class,
    pipeline_type=failure.pipeline_type.value,
    rag_context_length=len(rag_context),
    is_ambiguous=self._is_ambiguous_error(failure.error_message),
)
technique = signature["technique"]
```

**Why for Spark/Synapse/Kusto context:**
- Different error types have vastly different optimal prompts: a Spark OOM benefits from few-shot examples with data skew patterns, while a Kusto mapping error benefits from step-back with schema context.
- Over time, the feedback loop learns that CoT produces better results for Synapse timeout errors than zero-shot, and automatically adjusts.
- The same error class may need different prompts for different pipeline types (batch vs streaming) — DSPy signatures capture this.
- Enables A/B testing: for the same error, compare CoT vs ToT outputs and track which produces more accurate diagnoses, then automatically prefer the winner.

---

## 20. Chain-of-Verification (CoV)

**Where:** `CHAIN_OF_VERIFICATION` template, `PromptEngine.invoke_with_verification()`

**When used:** High-stakes analyses where factual accuracy is critical

**Implementation:**
1. **Extract claims** — list every factual claim in the analysis
2. **Verify each claim** — check against evidence (logs, RAG context)
3. **Classify** — VERIFIED / UNVERIFIED / CONTRADICTED
4. **Correct** — rewrite the analysis with corrections and confidence labels
5. **Score reliability** — percentage of verified claims

```python
verified_output, claims = await self.prompt_engine.invoke_with_verification(
    analysis=initial_analysis,
    evidence=rag_context,
    alert_context=alert_summary,
)
```

**Why for Spark/Synapse/Kusto context:**
- Root cause analyses can hallucinate specific Spark config parameters (e.g., claiming `spark.sql.shuffle.partitions` was set to 200 when it wasn't in the logs). CoV catches this by checking each claim against the evidence.
- When the LLM says "this error was caused by a schema change in the upstream table," CoV verifies whether the available evidence actually mentions a schema change.
- For ICM incident write-ups, factual errors can lead to incorrect runbook recommendations. Verifying each claim before presenting to the support engineer prevents cascading misinformation.
- The reliability score (e.g., "85% of claims verified") gives support engineers a clear signal of how much they can trust the analysis vs. needing manual verification.

---

## 21. Hybrid Search (BM25 + Vector)

**Where:** `BM25Index` and `VectorStore.search()` in `src/rag/vector_store.py`

**Implementation:**
- **BM25 index** runs in parallel with ChromaDB vector search
- Results are **merged** with configurable weighting: `alpha * vector_score + (1-alpha) * bm25_score` (default: 60% vector, 40% BM25)
- BM25 scores are normalized to [0,1] before merging
- BM25-only results that aren't in vector results are fetched from ChromaDB for content

```python
result = await self.vector_store.search(
    query=query, top_k=5, use_hybrid=True,
)
```

**Why for Spark/Synapse/Kusto context:**
- **Exact error codes need lexical matching:** "Permanent_MappingNotFound" is a specific Kusto error code that embeddings may not capture precisely. BM25 ensures exact token matches rank highly.
- **Stack trace class names:** "org.apache.spark.sql.catalyst.expressions.UnsafeRow.copy" is a specific class path that semantic search may approximate but BM25 matches exactly.
- **Spark configuration keys:** `spark.sql.adaptive.skewJoin.enabled` is a literal string that must be matched token-for-token when searching runbooks.
- **Hybrid approach captures both intent and specifics:** Vector search understands "memory problem" → OOM, while BM25 catches the specific error code "java.lang.OutOfMemoryError: Java heap space."

---

## 22. CRAG (Corrective RAG)

**Where:** `CRAGEvaluator` in `src/rag/retriever.py`, integrated into `RAGRetriever.retrieve_for_alert()`

**Implementation:**
1. **Grade each chunk:** Calculate relevance score from token overlap (40%) + vector similarity (60%)
2. **Classify:** CORRECT (≥0.5), AMBIGUOUS (≥0.3), INCORRECT (<0.3)
3. **Correct:** Keep correct chunks, include ambiguous at lower priority, remove incorrect
4. **Fallback:** When ALL chunks are irrelevant, signal "no relevant context" to avoid hallucination

```python
scores = self.crag.grade_retrieval(failure.error_message, chunks_list)
chunks_list, crag_action = self.crag.apply_correction(chunks_list, scores)
```

**Why for Spark/Synapse/Kusto context:**
- Generic OOM runbooks get retrieved for specific Delta Lake compaction OOMs — CRAG detects this mismatch and filters the irrelevant chunks.
- Old/outdated incident resolutions may match on keywords but describe deprecated configs (Spark 2.x advice for Spark 3.x clusters). CRAG downgrades these.
- Kusto ingestion errors look similar across different mapping formats but require very different fixes — CRAG filters out false-positive matches.
- When a truly novel failure occurs (never seen before), CRAG correctly identifies that no relevant context exists, preventing the LLM from confidently generating wrong advice based on superficially similar but irrelevant documents.

---

## 23. HyDE (Hypothetical Document Embeddings)

**Where:** `HyDEGenerator` in `src/rag/retriever.py`, integrated into `RAGRetriever.retrieve_for_alert()`

**Implementation:**
1. **Classify the error** into a domain template (spark_oom, spark_shuffle, synapse_timeout, kusto_ingestion, generic)
2. **Generate a hypothetical resolution document** using the template
3. **Embed this hypothetical document** as an additional search query
4. **Retrieve** using the hypothetical document's embedding, which is closer in vector space to actual resolution documents

```python
hyde_query = self.hyde.generate_hypothetical_document(
    query=failure.error_message[:300],
    pipeline_name=failure.pipeline_name,
    error_class=failure.error_class,
)
queries.append(hyde_query[:500])
```

**Why for Spark/Synapse/Kusto context:**
- User queries are terse ("OOM on daily job") but knowledge docs are verbose runbooks — HyDE bridges this vocabulary gap by generating a hypothetical resolution document that embeds closer to real ones.
- Support engineers describe symptoms ("job keeps failing at stage 12") not solutions — HyDE generates what the ideal resolution doc would look like, improving retrieval of actual resolution content.
- Error messages use Spark/Kusto internal terminology but runbooks use operational language — hypothetical docs bridge both vocabularies.
- Short KQL error codes like `Permanent_MappingNotFound` map poorly to long-form Confluence articles; HyDE creates an intermediary document that matches better semantically.

---

## 24. Parent-Child Chunking

**Where:** `ParentChildChunkManager` in `src/rag/vector_store.py`

**Implementation:**
- **Parent chunks** (2000 chars): Preserve full context of a section
- **Child chunks** (400 chars, 50 overlap): Small, precise units for embedding
- **Child chunks are indexed** in the vector store for precise retrieval
- **Parent chunks are stored** in memory for context expansion
- When a child chunk is retrieved, the full parent content can be **expanded** into the prompt

```python
parents, children = self._parent_child_manager.create_parent_child_chunks(
    doc_id="runbook_001", content=full_document_text, ...
)
# Children go into vector store, parents stored for expansion
await self.add_documents(children)
```

**Why for Spark/Synapse/Kusto context:**
- Runbooks have sections (Overview → Diagnosis → Resolution → Prevention) — a child chunk about "increase executor memory" is only useful with the parent context about WHICH error it applies to.
- ICM incidents have structured fields (Title, Description, Root Cause, Resolution) — retrieving just the resolution without the error context produces incomplete guidance.
- Log analysis requires seeing both the specific error line (child) AND the surrounding context (parent) to understand the failure chain.
- Standard 1000-char chunks are either too small (losing context) or too large (diluting relevance scores). Parent-child gives us precise retrieval with full context.

---

## 25. Agentic RAG (Multi-Source Orchestration)

**Where:** `AgenticRAGOrchestrator` in `src/rag/retriever.py`, integrated into `RAGRetriever.retrieve_for_query()`

**Implementation:**
1. **Plan retrieval:** Based on intent, determine which sources to query and in what order
2. **Route by intent:**
   - "Similar incidents" → ICM first, then Confluence
   - "Runbook steps" → Confluence first
   - "View logs" → Log Analytics first
   - "Root cause" → ICM + Confluence + GraphRAG
   - General → all sources
3. **Execute plan:** Query each source with appropriate mode (hybrid/semantic/keyword)
4. **Merge results:** Aggregate chunks from all sources

```python
plan = self.agentic_rag.plan_retrieval(query=query, active_alert=active_alert)
result = await self.agentic_rag.execute_plan(plan)
```

**Why for Spark/Synapse/Kusto context:**
- A question about "similar incidents" should query ICM first (where incidents live), while "runbook steps" should query Confluence first (where runbooks live). Static retrieval queries all sources equally and wastes tokens on irrelevant results.
- Complex investigations may need sequential retrieval: first find the error pattern in logs, then search ICM for matching incidents, then fetch the relevant runbook. This requires intelligent planning.
- Different source types have different retrieval needs: Kusto logs need keyword-exact search (BM25), Confluence needs semantic search, ICM benefits from structured field matching. Agentic RAG selects the right mode per source.
- Reduces total retrieval latency by skipping unnecessary sources (why search Log Analytics when the user is asking for runbook steps?).

---

## 26. GraphRAG (Cascading Failure Tracing)

**Where:** `GraphRAGEngine` in `src/rag/retriever.py`, `ChatEngine._handle_blast_radius()`, `ChatEngine._handle_escalation()`

**Implementation:**
- **Knowledge graph** with nodes (pipelines, components, error types) and edges (FEEDS_INTO, RUNS_ON, COMMONLY_FAILS_WITH, etc.)
- **Blast radius tracing:** BFS from the failed pipeline to find all affected downstream pipelines and components
- **Failure chain tracing:** Upstream dependencies → failure point → downstream impact
- **Context injection:** Graph relationships are serialized into a text context for LLM prompts

```python
blast = self.graph_rag.trace_blast_radius("spark_etl_daily_ingest", depth=3)
chain = self.graph_rag.find_failure_chain("spark_etl_daily_ingest", "OutOfMemoryError")
```

**Why for Spark/Synapse/Kusto context:**
- Data pipelines have complex dependency graphs (ingestion → transform → serve). When `spark_etl_daily_ingest` fails, the support engineer needs to know that `synapse_pipeline_customer360`, `synapse_pipeline_daily_report`, AND `kusto_ingestion_telemetry` are all affected.
- Cascading failures (ADLS throttling → Spark read fails → Synapse pipeline aborts → Kusto table goes stale) need multi-hop reasoning that flat retrieval cannot provide.
- Root cause analysis requires tracing BACKWARDS through the dependency graph to find the true origin of the problem.
- Escalation decisions require blast radius: a Sev3 single-pipeline failure may need upgrade to Sev2 if the dependency graph shows it blocks 5 downstream consumer pipelines.

---

## Technique Selection Matrix

| Scenario | Technique(s) | Rationale |
|----------|-------------|-----------|
| Known error, no context | Zero-shot | LLM knows the pattern |
| Unknown error, no context | Few-shot | Examples guide analysis |
| Any error, rich RAG context | Chain-of-Thought | Reason through evidence |
| Ambiguous/intermittent error | Tree-of-Thought + Step-Back | Explore hypotheses, abstract first |
| Severity classification | Self-Consistency | Reduce misclassification |
| Interactive troubleshooting | ReAct | Tool-augmented reasoning |
| Low-confidence response | Active Prompting + FLARE | Transparent uncertainty + active retrieval |
| Hierarchical knowledge lookup | Topic Tree | Precision retrieval |
| Critical/High severity RCA | Reflexion | Self-refine for accuracy |
| Any analysis with evidence | Chain-of-Verification | Fact-check claims |
| Exact error code matching | Hybrid Search (BM25+Vector) | Lexical + semantic |
| Retrieved context may be stale | CRAG | Filter irrelevant chunks |
| Terse query, verbose docs | HyDE | Bridge vocabulary gap |
| Long runbooks, structured incidents | Parent-Child Chunking | Precise retrieval + full context |
| Multi-source investigation | Agentic RAG | Intelligent source routing |
| Cascading failures, blast radius | GraphRAG | Dependency tracing |
| Repeated error class patterns | DSPy Optimization | Data-driven technique selection |
