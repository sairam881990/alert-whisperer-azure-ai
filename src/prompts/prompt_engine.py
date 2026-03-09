"""
Prompt Engine — Orchestrates prompt construction and LLM interaction.

Implements the complete prompt engineering pipeline:
1. Template selection based on task type and complexity
2. Context injection (RAG results, conversation history)
3. Few-shot example selection
4. Prompt rendering and validation
5. LLM invocation with retry logic
6. Response parsing and structured extraction
7. DSPy-style programmatic prompt optimization
8. Reflexion self-refinement
9. Step-Back prompting for ambiguous errors
10. Chain-of-Verification for factual safety

Uses Pydantic AI patterns for type-safe prompting.
"""

from __future__ import annotations

import json
import re
import hashlib
from typing import Any, Optional

import structlog
from pydantic import BaseModel, Field

from src.models import (
    ChatMessage,
    ConversationContext,
    DocumentChunk,
    FewShotExample,
    MessageRole,
    ParsedFailure,
    PromptTemplate,
    ReActStep,
    ReflexionStep,
    ThoughtBranch,
    VerificationClaim,
)
from src.prompts.templates import (
    CURATED_FEW_SHOT_EXAMPLES,
    GLOBAL_KNOWLEDGE_PROMPT,
    SOLUTION_PROMPT,
    STYLE_GUIDE_PROMPT,
    TEMPLATE_REGISTRY,
    format_few_shot_examples,
    get_template,
)

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────
# Pydantic Models for Structured Prompt I/O
# ─────────────────────────────────────────────────

class RootCauseOutput(BaseModel):
    """Structured output from root cause analysis."""
    error_classification: str = Field(description="Classified error type")
    root_cause_summary: str = Field(description="Plain-English root cause explanation")
    technical_details: str = Field(description="Technical analysis")
    severity_assessment: str = Field(description="Severity with justification")
    affected_components: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class RoutingOutput(BaseModel):
    """Structured output from alert routing."""
    target_channel: str
    target_contacts: list[str]
    severity: str
    auto_escalate: bool
    routing_reason: str
    confidence: float = Field(ge=0.0, le=1.0)


class SeverityOutput(BaseModel):
    """Structured output from severity classification."""
    severity: str
    confidence: str
    justification: str
    assessments: list[dict[str, str]] = Field(default_factory=list)


# ─────────────────────────────────────────────────
# DSPy-Style Prompt Optimizer
# ─────────────────────────────────────────────────

class DSPyPromptOptimizer:
    """
    DSPy-style programmatic prompt optimization.

    Why DSPy-style optimization for Spark/Synapse/Kusto troubleshooting:
    - Different error types have vastly different optimal prompts:
      a Spark OOM benefits from few-shot examples with data skew patterns,
      while a Kusto mapping error benefits from zero-shot with schema context
    - Support team feedback (upvote/downvote on responses) can be used to
      automatically tune which prompting technique works best for each
      error category over time
    - The same error class may need different prompts for different pipeline
      types (batch vs streaming) — DSPy signatures capture this
    - Prompt templates can be A/B tested: for the same error, compare
      CoT vs ToT outputs and track which produces more accurate diagnoses,
      then automatically prefer the winner

    This is a simplified DSPy-inspired approach using signature-based
    prompt construction with metric-driven selection.
    """

    def __init__(self):
        # Signature registry: maps (error_class, pipeline_type) -> best technique
        self._signature_cache: dict[str, dict[str, Any]] = {}
        # Performance metrics per technique per error class
        self._metrics: dict[str, list[float]] = {}
        # Default signatures
        self._default_signatures = {
            "OutOfMemoryError": {
                "technique": "few_shot",
                "temperature": 0.1,
                "reason": "OOM errors have strong patterns that few-shot examples capture well",
            },
            "TimeoutException": {
                "technique": "cot",
                "temperature": 0.2,
                "reason": "Timeouts need step-by-step reasoning to identify the bottleneck",
            },
            "ConnectionFailure": {
                "technique": "react",
                "temperature": 0.1,
                "reason": "Connection issues need iterative checking of multiple components",
            },
            "ConfigurationError": {
                "technique": "step_back",
                "temperature": 0.1,
                "reason": "Config errors benefit from abstract reasoning before specific diagnosis",
            },
            "SchemaError": {
                "technique": "cot",
                "temperature": 0.1,
                "reason": "Schema mismatches need careful field-by-field comparison",
            },
        }

    def get_optimal_signature(
        self,
        error_class: str,
        pipeline_type: str = "",
        rag_context_length: int = 0,
        is_ambiguous: bool = False,
    ) -> dict[str, Any]:
        """
        Get the optimal prompt signature for a given error scenario.

        Args:
            error_class: The classified error type
            pipeline_type: Type of pipeline (spark_batch, synapse_pipeline, etc.)
            rag_context_length: Amount of RAG context available
            is_ambiguous: Whether the error is ambiguous/intermittent

        Returns:
            Dict with technique, temperature, and reasoning
        """
        cache_key = f"{error_class}_{pipeline_type}_{rag_context_length > 500}_{is_ambiguous}"

        if cache_key in self._signature_cache:
            return self._signature_cache[cache_key]

        # Check for known optimal signature
        if error_class in self._default_signatures and not is_ambiguous:
            signature = {**self._default_signatures[error_class]}
        elif is_ambiguous:
            signature = {
                "technique": "tree_of_thought",
                "temperature": 0.3,
                "reason": "Ambiguous errors need hypothesis exploration via Tree-of-Thought",
            }
        elif rag_context_length > 1000:
            signature = {
                "technique": "cot",
                "temperature": 0.1,
                "reason": "Rich context available — Chain-of-Thought to reason through evidence",
            }
        elif rag_context_length > 0:
            signature = {
                "technique": "few_shot",
                "temperature": 0.1,
                "reason": "Some context — use few-shot examples to guide analysis format",
            }
        else:
            signature = {
                "technique": "zero_shot",
                "temperature": 0.2,
                "reason": "No context — rely on LLM parametric knowledge with zero-shot",
            }

        self._signature_cache[cache_key] = signature
        logger.info(
            "dspy_signature_resolved",
            error_class=error_class,
            technique=signature["technique"],
            reason=signature["reason"],
        )
        return signature

    def record_feedback(
        self,
        error_class: str,
        technique: str,
        score: float,
    ) -> None:
        """
        Record performance feedback to improve future technique selection.
        Score: 0.0 (poor) to 1.0 (excellent).
        """
        key = f"{error_class}_{technique}"
        if key not in self._metrics:
            self._metrics[key] = []
        self._metrics[key].append(score)

        # If enough data, update default signature
        if len(self._metrics[key]) >= 5:
            avg = sum(self._metrics[key][-10:]) / len(self._metrics[key][-10:])
            current = self._default_signatures.get(error_class, {})
            if not current or avg > 0.8:
                self._default_signatures[error_class] = {
                    "technique": technique,
                    "temperature": 0.1,
                    "reason": f"Auto-optimized: avg score {avg:.2f} over {len(self._metrics[key])} samples",
                }
                logger.info(
                    "dspy_signature_updated",
                    error_class=error_class,
                    technique=technique,
                    avg_score=avg,
                )
                # Invalidate cached signatures for this error class
                # so future lookups pick up the new default
                stale_keys = [
                    k for k in self._signature_cache
                    if k.startswith(f"{error_class}_")
                ]
                for k in stale_keys:
                    del self._signature_cache[k]
                if stale_keys:
                    logger.info(
                        "dspy_cache_invalidated",
                        error_class=error_class,
                        keys_removed=len(stale_keys),
                    )


# ─────────────────────────────────────────────────
# Prompt Engine (Enhanced)
# ─────────────────────────────────────────────────

class PromptEngine:
    """
    Central prompt engine that constructs, renders, and manages prompts.

    Supports:
    - Automatic technique selection (zero-shot → few-shot → CoT)
    - DSPy-style programmatic optimization
    - Context window management
    - Structured output parsing
    - Conversation history management
    - Reflexion self-refinement
    - Step-Back prompting for ambiguous errors
    - Chain-of-Verification for factual safety
    """

    def __init__(
        self,
        llm_client: Any = None,
        max_context_tokens: int = 120000,
        default_temperature: float = 0.1,
    ):
        self.llm_client = llm_client
        self.max_context_tokens = max_context_tokens
        self.default_temperature = default_temperature
        self.dspy_optimizer = DSPyPromptOptimizer()

    def build_system_prompt(
        self,
        include_solution_framework: bool = True,
        custom_instructions: str = "",
    ) -> str:
        """
        Build the full system prompt combining global knowledge,
        style guide, and solution framework.
        """
        parts = [GLOBAL_KNOWLEDGE_PROMPT, STYLE_GUIDE_PROMPT]

        if include_solution_framework:
            parts.append(SOLUTION_PROMPT)

        if custom_instructions:
            parts.append(f"\n=== ADDITIONAL INSTRUCTIONS ===\n{custom_instructions}")

        return "\n\n".join(parts)

    def build_root_cause_prompt(
        self,
        failure: ParsedFailure,
        rag_context: str = "",
        technique: str = "auto",
    ) -> list[dict[str, str]]:
        """
        Build a root cause analysis prompt with automatic technique selection.

        Args:
            failure: The parsed failure to analyze
            rag_context: Retrieved context from RAG
            technique: Prompting technique to use (auto, zero_shot, few_shot, cot, tree_of_thought, step_back)

        Returns:
            List of message dicts ready for LLM API
        """
        # Auto-select technique using DSPy optimizer
        if technique == "auto":
            signature = self.dspy_optimizer.get_optimal_signature(
                error_class=failure.error_class,
                pipeline_type=failure.pipeline_type.value,
                rag_context_length=len(rag_context),
                is_ambiguous=self._is_ambiguous_error(failure.error_message),
            )
            technique = signature["technique"]

        logger.info("prompt_technique_selected", technique=technique, failure_id=failure.failure_id)

        # Build messages
        messages = [{"role": "system", "content": self.build_system_prompt()}]

        if technique == "zero_shot":
            template = get_template("root_cause_zero_shot")
            user_content = template.render(
                error_message=failure.error_message,
                pipeline_name=failure.pipeline_name,
                pipeline_type=failure.pipeline_type.value,
                log_snippet=failure.log_snippet[:3000],
                timestamp=failure.timestamp.isoformat(),
            )

        elif technique == "few_shot":
            template = get_template("root_cause_few_shot")
            examples_text = format_few_shot_examples(CURATED_FEW_SHOT_EXAMPLES)
            user_content = template.render(
                error_message=failure.error_message,
                pipeline_name=failure.pipeline_name,
                pipeline_type=failure.pipeline_type.value,
                log_snippet=failure.log_snippet[:3000],
                timestamp=failure.timestamp.isoformat(),
                few_shot_examples=examples_text,
            )

        elif technique == "cot":
            template = get_template("root_cause_cot")
            user_content = template.render(
                error_message=failure.error_message,
                pipeline_name=failure.pipeline_name,
                pipeline_type=failure.pipeline_type.value,
                log_snippet=failure.log_snippet[:3000],
                context=rag_context[:6000],
            )

        elif technique == "tree_of_thought":
            template = get_template("root_cause_tree_of_thought")
            user_content = template.render(
                error_message=failure.error_message,
                pipeline_name=failure.pipeline_name,
                log_snippet=failure.log_snippet[:3000],
                context=rag_context[:6000],
            )

        elif technique == "step_back":
            template = get_template("step_back_analysis")
            user_content = template.render(
                error_message=failure.error_message,
                pipeline_name=failure.pipeline_name,
                pipeline_type=failure.pipeline_type.value,
                context=rag_context[:6000],
            )

        else:
            raise ValueError(f"Unknown technique: {technique}")

        messages.append({"role": "user", "content": user_content})
        return messages

    def build_reflexion_prompt(
        self,
        initial_analysis: str,
        failure: ParsedFailure,
        feedback_signals: str = "",
    ) -> list[dict[str, str]]:
        """
        Build a Reflexion prompt for self-refinement of an initial analysis.

        Args:
            initial_analysis: The first-pass analysis to refine
            failure: Original failure context
            feedback_signals: Optional feedback (user corrections, metric disagreements)

        Returns:
            List of message dicts for LLM API
        """
        messages = [{"role": "system", "content": self.build_system_prompt()}]

        template = get_template("reflexion_analysis")
        error_context = (
            f"Pipeline: {failure.pipeline_name}\n"
            f"Error: {failure.error_message[:500]}\n"
            f"Error Class: {failure.error_class}\n"
            f"Severity: {failure.severity.value}"
        )

        user_content = template.render(
            initial_analysis=initial_analysis,
            error_context=error_context,
            feedback_signals=feedback_signals or "No explicit feedback. Self-assess based on completeness, accuracy, and actionability.",
        )

        messages.append({"role": "user", "content": user_content})
        return messages

    def build_chain_of_verification_prompt(
        self,
        analysis: str,
        evidence: str,
        alert_context: str = "",
    ) -> list[dict[str, str]]:
        """
        Build a Chain-of-Verification prompt to fact-check an analysis.

        Args:
            analysis: The analysis to verify
            evidence: Available evidence (logs, RAG context, etc.)
            alert_context: Alert-specific context

        Returns:
            List of message dicts for LLM API
        """
        messages = [{"role": "system", "content": self.build_system_prompt(include_solution_framework=False)}]

        template = get_template("chain_of_verification")
        user_content = template.render(
            analysis_to_verify=analysis,
            available_evidence=evidence or "No additional evidence available.",
            alert_context=alert_context or "No specific alert context.",
        )

        messages.append({"role": "user", "content": user_content})
        return messages

    def build_conversational_prompt(
        self,
        user_question: str,
        context: ConversationContext,
        rag_context: str = "",
    ) -> list[dict[str, str]]:
        """Build a conversational Q&A prompt with full context."""
        messages = [{"role": "system", "content": self.build_system_prompt()}]

        # Build conversation history string
        history_parts = []
        for msg in context.get_recent_messages(10):
            role_label = "Support Engineer" if msg.role == MessageRole.USER else "Alert Whisperer"
            history_parts.append(f"{role_label}: {msg.content}")
        conversation_history = "\n".join(history_parts) if history_parts else "No prior conversation."

        # Build alert context string
        alert_context = "No active alert."
        if context.active_alert:
            alert = context.active_alert
            alert_context = (
                f"Pipeline: {alert.pipeline_name}\n"
                f"Error: {alert.error_message}\n"
                f"Error Class: {alert.error_class}\n"
                f"Severity: {alert.severity.value}\n"
                f"Root Cause: {alert.root_cause_summary}\n"
                f"Timestamp: {alert.timestamp.isoformat()}"
            )

        template = get_template("conversational_qa")
        user_content = template.render(
            user_question=user_question,
            alert_context=alert_context,
            rag_context=rag_context or "No additional context retrieved.",
            conversation_history=conversation_history,
        )

        messages.append({"role": "user", "content": user_content})
        return messages

    def build_routing_prompt(
        self,
        failure: ParsedFailure,
        ownership_map: str,
    ) -> list[dict[str, str]]:
        """Build the alert routing prompt."""
        messages = [{"role": "system", "content": self.build_system_prompt(include_solution_framework=False)}]

        failure_summary = (
            f"Pipeline: {failure.pipeline_name}\n"
            f"Type: {failure.pipeline_type.value}\n"
            f"Error Class: {failure.error_class}\n"
            f"Error: {failure.error_message[:500]}\n"
            f"Root Cause: {failure.root_cause_summary}\n"
            f"Timestamp: {failure.timestamp.isoformat()}"
        )

        template = get_template("alert_routing")
        user_content = template.render(
            failure_summary=failure_summary,
            ownership_map=ownership_map,
            severity=failure.severity.value,
        )

        messages.append({"role": "user", "content": user_content})
        return messages

    def build_severity_prompt(
        self,
        failure: ParsedFailure,
        pipeline_metadata: str = "",
        blast_radius: str = "",
    ) -> list[dict[str, str]]:
        """Build the severity classification prompt with self-consistency."""
        messages = [{"role": "system", "content": self.build_system_prompt(include_solution_framework=False)}]

        template = get_template("severity_classifier")
        user_content = template.render(
            error_message=failure.error_message[:500],
            pipeline_name=failure.pipeline_name,
            pipeline_metadata=pipeline_metadata or "No additional metadata available.",
            blast_radius=blast_radius or "Unknown — requires investigation.",
        )

        messages.append({"role": "user", "content": user_content})
        return messages

    def build_log_parsing_prompt(
        self,
        raw_logs: str,
        pipeline_name: str,
        expected_behavior: str = "",
    ) -> list[dict[str, str]]:
        """Build the log parsing prompt."""
        messages = [{"role": "system", "content": self.build_system_prompt(include_solution_framework=False)}]

        template = get_template("log_parser")
        user_content = template.render(
            raw_logs=raw_logs[:8000],
            pipeline_name=pipeline_name,
            expected_behavior=expected_behavior or "Pipeline should complete successfully without errors.",
        )

        messages.append({"role": "user", "content": user_content})
        return messages

    def build_notification_prompt(
        self,
        failure: ParsedFailure,
        routing_info: str,
        similar_count: int,
        actions: list[str],
    ) -> list[dict[str, str]]:
        """Build the Teams notification generation prompt."""
        messages = [{"role": "system", "content": STYLE_GUIDE_PROMPT}]

        template = get_template("teams_notification")
        user_content = template.render(
            failure_summary=f"{failure.pipeline_name}: {failure.error_class} — {failure.root_cause_summary}",
            root_cause=failure.root_cause_summary,
            severity=failure.severity.value,
            routing=routing_info,
            similar_incidents=f"{similar_count} similar incidents found in last 90 days" if similar_count else "No similar incidents found",
            actions="\n".join(f"- {a}" for a in actions),
        )

        messages.append({"role": "user", "content": user_content})
        return messages

    async def invoke_llm(
        self,
        messages: list[dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: int = 4096,
        response_format: Optional[str] = None,
    ) -> str:
        """Invoke the LLM with the constructed messages."""
        if not self.llm_client:
            logger.warning("llm_client_not_configured")
            return "[LLM not configured — returning mock response for development]"

        try:
            kwargs: dict[str, Any] = {
                "model": self.llm_client.model if hasattr(self.llm_client, "model") else "gpt-4o",
                "messages": messages,
                "temperature": temperature or self.default_temperature,
                "max_tokens": max_tokens,
            }

            if response_format == "json":
                kwargs["response_format"] = {"type": "json_object"}

            response = await self.llm_client.chat.completions.create(**kwargs)
            return response.choices[0].message.content or ""

        except Exception as e:
            logger.error("llm_invocation_error", error=str(e))
            raise

    async def invoke_with_reflexion(
        self,
        messages: list[dict[str, str]],
        failure: ParsedFailure,
        max_iterations: int = 2,
    ) -> tuple[str, list[ReflexionStep]]:
        """
        Invoke LLM with Reflexion: generate initial response, then
        self-refine iteratively.

        Returns:
            Tuple of (final_response, reflexion_steps)
        """
        # Generate initial response
        initial_response = await self.invoke_llm(messages)
        steps = []

        current_response = initial_response
        for i in range(max_iterations):
            # Build reflexion prompt
            reflexion_msgs = self.build_reflexion_prompt(
                initial_analysis=current_response,
                failure=failure,
            )

            # Get refined response
            reflexion_output = await self.invoke_llm(reflexion_msgs)

            step = ReflexionStep(
                iteration=i + 1,
                initial_response=current_response[:500],
                reflection=reflexion_output[:500],
                refined_response=reflexion_output,
            )
            steps.append(step)

            # Check if refinement is meaningful
            if "no changes needed" in reflexion_output.lower():
                break

            current_response = reflexion_output

        return current_response, steps

    async def invoke_with_verification(
        self,
        analysis: str,
        evidence: str,
        alert_context: str = "",
    ) -> tuple[str, list[VerificationClaim]]:
        """
        Apply Chain-of-Verification to an analysis.

        Returns:
            Tuple of (verified_analysis, claims_list)
        """
        messages = self.build_chain_of_verification_prompt(
            analysis=analysis,
            evidence=evidence,
            alert_context=alert_context,
        )

        verified_output = await self.invoke_llm(messages)

        # Parse verification claims from output
        claims = self._parse_verification_claims(verified_output)

        return verified_output, claims

    def parse_routing_response(self, response: str) -> Optional[RoutingOutput]:
        """Parse LLM routing response into structured RoutingOutput."""
        try:
            json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                return RoutingOutput(**data)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning("routing_parse_failed", error=str(e))
        return None

    def parse_root_cause_response(self, response: str) -> RootCauseOutput:
        """Parse LLM root cause response into structured output."""
        error_class = self._extract_section(response, r"\*\*Error Classification\*\*:\s*(.+?)(?:\n|$)")
        root_cause = self._extract_section(response, r"\*\*Root Cause Summary\*\*[^:]*:\s*(.+?)(?:\n\n|\*\*)")
        technical = self._extract_section(response, r"\*\*Technical Details\*\*[^:]*:\s*(.+?)(?:\n\n|\*\*)")
        severity = self._extract_section(response, r"\*\*Severity Assessment\*\*:\s*(.+?)(?:\n\n|\*\*)")
        components = self._extract_list(response, r"\*\*Affected Components\*\*:\s*(.+?)(?:\n\n|\*\*)")

        return RootCauseOutput(
            error_classification=error_class or "Unknown",
            root_cause_summary=root_cause or response[:500],
            technical_details=technical or "",
            severity_assessment=severity or "medium",
            affected_components=components,
        )

    # ─── Internal Helpers ─────────────────────────

    def _select_technique(self, failure: ParsedFailure, rag_context: str) -> str:
        """
        Auto-select the best prompting technique.
        Now delegates to DSPy optimizer for data-driven selection.
        """
        return self.dspy_optimizer.get_optimal_signature(
            error_class=failure.error_class,
            pipeline_type=failure.pipeline_type.value,
            rag_context_length=len(rag_context),
            is_ambiguous=self._is_ambiguous_error(failure.error_message),
        )["technique"]

    @staticmethod
    def _is_ambiguous_error(error_message: str) -> bool:
        """Check if an error message indicates an ambiguous/intermittent issue."""
        ambiguous_indicators = [
            "intermittent", "random", "sometimes", "flaky", "sporadic",
            "occasionally", "nondeterministic", "race condition", "transient",
        ]
        return any(ind in error_message.lower() for ind in ambiguous_indicators)

    @staticmethod
    def _parse_verification_claims(output: str) -> list[VerificationClaim]:
        """Parse Chain-of-Verification output into structured claims."""
        claims = []
        claim_pattern = r"CLAIM:\s*(.+?)(?:\n|$).*?EVIDENCE:\s*(.+?)(?:\n|$).*?VERDICT:\s*(\w+)"
        matches = re.finditer(claim_pattern, output, re.DOTALL | re.IGNORECASE)

        for match in matches:
            claim_text = match.group(1).strip()
            evidence = match.group(2).strip()
            verdict = match.group(3).strip().upper()

            claims.append(
                VerificationClaim(
                    claim=claim_text,
                    evidence=evidence,
                    verified=verdict == "VERIFIED",
                    correction=None if verdict == "VERIFIED" else f"Status: {verdict}",
                )
            )

        return claims

    @staticmethod
    def _extract_section(text: str, pattern: str) -> str:
        """Extract a section from LLM output using regex."""
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _extract_list(text: str, pattern: str) -> list[str]:
        """Extract a list from LLM output."""
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            raw = match.group(1)
            items = re.split(r'[,\n•\-]', raw)
            return [item.strip() for item in items if item.strip()]
        return []
