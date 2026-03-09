"""
Tests for the prompt engineering module.
"""

from datetime import datetime

import pytest

from src.models import ParsedFailure, PipelineType, AlertSource, Severity
from src.prompts.prompt_engine import PromptEngine, RootCauseOutput
from src.prompts.templates import (
    CURATED_FEW_SHOT_EXAMPLES,
    TEMPLATE_REGISTRY,
    format_few_shot_examples,
    get_template,
)


class TestTemplateRegistry:
    def test_all_templates_registered(self):
        expected = [
            "root_cause_zero_shot",
            "root_cause_few_shot",
            "root_cause_cot",
            "root_cause_tree_of_thought",
            "react_troubleshooting",
            "alert_routing",
            "severity_classifier",
            "conversational_qa",
            "similar_incidents_qa",
            "log_parser",
            "teams_notification",
        ]
        for name in expected:
            assert name in TEMPLATE_REGISTRY, f"Missing template: {name}"

    def test_get_template_valid(self):
        t = get_template("root_cause_zero_shot")
        assert t.name == "root_cause_zero_shot"
        assert t.technique == "zero_shot"

    def test_get_template_invalid(self):
        with pytest.raises(KeyError):
            get_template("nonexistent_template")

    def test_all_templates_renderable(self):
        for name, template in TEMPLATE_REGISTRY.items():
            # Render with dummy values
            kwargs = {var: f"test_{var}" for var in template.variables}
            rendered = template.render(**kwargs)
            assert len(rendered) > 0, f"Template {name} rendered empty"
            # Ensure no unreplaced variables
            for var in template.variables:
                assert f"{{{{{var}}}}}" not in rendered, (
                    f"Template {name} has unreplaced variable: {var}"
                )


class TestFewShotExamples:
    def test_examples_exist(self):
        assert len(CURATED_FEW_SHOT_EXAMPLES) >= 3

    def test_examples_have_content(self):
        for ex in CURATED_FEW_SHOT_EXAMPLES:
            assert len(ex.input_text) > 50
            assert len(ex.output_text) > 100

    def test_format_examples(self):
        formatted = format_few_shot_examples(CURATED_FEW_SHOT_EXAMPLES)
        assert "Example 1" in formatted
        assert "Example 2" in formatted
        assert "INPUT:" in formatted
        assert "OUTPUT:" in formatted


class TestPromptEngine:
    @pytest.fixture
    def engine(self):
        return PromptEngine(llm_client=None)

    @pytest.fixture
    def sample_failure(self):
        return ParsedFailure(
            failure_id="test-001",
            pipeline_name="spark_etl_daily_ingest",
            pipeline_type=PipelineType.SPARK_BATCH,
            source=AlertSource.SPARK,
            timestamp=datetime.utcnow(),
            severity=Severity.HIGH,
            error_class="OutOfMemoryError",
            error_message="java.lang.OutOfMemoryError: Java heap space",
            root_cause_summary="OOM during shuffle",
            log_snippet="ERROR Executor: Exception in task 47.3\njava.lang.OutOfMemoryError",
        )

    def test_build_system_prompt(self, engine):
        prompt = engine.build_system_prompt()
        assert "Alert Whisperer" in prompt
        assert "Spark" in prompt
        assert "Synapse" in prompt
        assert "Kusto" in prompt

    def test_build_root_cause_prompt_zero_shot(self, engine, sample_failure):
        messages = engine.build_root_cause_prompt(sample_failure, technique="zero_shot")
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "OutOfMemoryError" in messages[1]["content"]

    def test_build_root_cause_prompt_few_shot(self, engine, sample_failure):
        messages = engine.build_root_cause_prompt(sample_failure, technique="few_shot")
        assert "Example" in messages[1]["content"]

    def test_build_root_cause_prompt_cot(self, engine, sample_failure):
        messages = engine.build_root_cause_prompt(
            sample_failure, rag_context="Historical incident data here", technique="cot"
        )
        assert "Step 1" in messages[1]["content"]
        assert "Step 2" in messages[1]["content"]

    def test_build_root_cause_prompt_tot(self, engine, sample_failure):
        messages = engine.build_root_cause_prompt(
            sample_failure, rag_context="Context", technique="tree_of_thought"
        )
        assert "Hypothesis 1" in messages[1]["content"]
        assert "Hypothesis 2" in messages[1]["content"]

    def test_auto_technique_selection_simple(self, engine, sample_failure):
        technique = engine._select_technique(sample_failure, "")
        # OOM with no context → zero_shot
        assert technique == "zero_shot"

    def test_auto_technique_selection_with_context(self, engine, sample_failure):
        technique = engine._select_technique(sample_failure, "x" * 600)
        assert technique == "cot"

    def test_auto_technique_selection_ambiguous(self, engine):
        failure = ParsedFailure(
            failure_id="test",
            pipeline_name="test",
            pipeline_type=PipelineType.SPARK_BATCH,
            source=AlertSource.SPARK,
            timestamp=datetime.utcnow(),
            severity=Severity.MEDIUM,
            error_class="Unknown",
            error_message="Intermittent failure sometimes occurs on random tasks",
            root_cause_summary="Unknown",
        )
        technique = engine._select_technique(failure, "")
        assert technique == "tree_of_thought"

    def test_parse_root_cause_response(self, engine):
        response = (
            "**Error Classification**: OutOfMemoryError\n\n"
            "**Root Cause Summary**: Executor ran out of memory.\n\n"
            "**Technical Details**: Task 47 failed during shuffle.\n\n"
            "**Severity Assessment**: HIGH — Daily pipeline blocked.\n\n"
            "**Affected Components**: spark_etl, analytics dashboard"
        )
        output = engine.parse_root_cause_response(response)
        assert output.error_classification == "OutOfMemoryError"
        assert "out of memory" in output.root_cause_summary.lower()

    def test_build_routing_prompt(self, engine, sample_failure):
        messages = engine.build_routing_prompt(sample_failure, '{"test": "ownership"}')
        assert len(messages) == 2
        assert "target_channel" in messages[1]["content"]
