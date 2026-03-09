"""
Tests for Pydantic models — validation, serialization, edge cases.
"""

from datetime import datetime

import pytest

from src.models import (
    AlertSource,
    AlertStatus,
    ChatMessage,
    ConversationContext,
    DocumentChunk,
    FewShotExample,
    HistoricalIncident,
    ICMTicket,
    MessageRole,
    ParsedFailure,
    PipelineType,
    PromptTemplate,
    RawLogEntry,
    RoutingDecision,
    Severity,
    ThoughtBranch,
    TopicNode,
)


class TestRawLogEntry:
    def test_basic_creation(self):
        entry = RawLogEntry(
            timestamp=datetime.utcnow(),
            source=AlertSource.SPARK,
            level="ERROR",
            message="OutOfMemoryError in task 47",
        )
        assert entry.level == "ERROR"
        assert entry.source == AlertSource.SPARK

    def test_level_normalization(self):
        entry = RawLogEntry(
            timestamp=datetime.utcnow(),
            source=AlertSource.KUSTO,
            level="  error  ",
            message="test",
        )
        assert entry.level == "ERROR"

    def test_optional_fields(self):
        entry = RawLogEntry(
            timestamp=datetime.utcnow(),
            source=AlertSource.LOG_ANALYTICS,
            level="WARN",
            message="test",
        )
        assert entry.pipeline_name is None
        assert entry.job_id is None
        assert entry.metadata == {}


class TestParsedFailure:
    def test_log_snippet_truncation(self):
        long_log = "x" * 10000
        failure = ParsedFailure(
            failure_id="test-001",
            pipeline_name="test_pipeline",
            pipeline_type=PipelineType.SPARK_BATCH,
            source=AlertSource.SPARK,
            timestamp=datetime.utcnow(),
            severity=Severity.HIGH,
            error_class="OutOfMemoryError",
            error_message="OOM",
            root_cause_summary="Test",
            log_snippet=long_log,
        )
        assert len(failure.log_snippet) <= 5003  # 5000 + "..."

    def test_severity_enum(self):
        failure = ParsedFailure(
            failure_id="test-002",
            pipeline_name="test",
            pipeline_type=PipelineType.SYNAPSE_PIPELINE,
            source=AlertSource.SYNAPSE,
            timestamp=datetime.utcnow(),
            severity=Severity.CRITICAL,
            error_class="Timeout",
            error_message="Pipeline timeout",
            root_cause_summary="Test",
        )
        assert failure.severity == Severity.CRITICAL
        assert failure.severity.value == "critical"


class TestConversationContext:
    def test_add_message(self):
        ctx = ConversationContext(session_id="test-session")
        ctx.add_message(MessageRole.USER, "Hello")
        ctx.add_message(MessageRole.ASSISTANT, "Hi there")
        assert ctx.message_count == 2
        assert ctx.messages[0].role == MessageRole.USER

    def test_get_recent_messages(self):
        ctx = ConversationContext(session_id="test-session")
        for i in range(30):
            ctx.add_message(MessageRole.USER, f"Message {i}")
        recent = ctx.get_recent_messages(10)
        assert len(recent) == 10
        assert recent[-1].content == "Message 29"


class TestPromptTemplate:
    def test_render(self):
        template = PromptTemplate(
            name="test",
            description="Test template",
            template="Hello {{name}}, error: {{error}}",
            variables=["name", "error"],
            technique="zero_shot",
        )
        result = template.render(name="Alice", error="OOM")
        assert result == "Hello Alice, error: OOM"

    def test_render_missing_variable(self):
        template = PromptTemplate(
            name="test",
            description="Test",
            template="Hello {{name}}",
            variables=["name"],
            technique="zero_shot",
        )
        result = template.render()  # Missing name
        assert "{{name}}" in result  # Unreplaced


class TestTopicNode:
    def test_recursive_structure(self):
        root = TopicNode(
            topic="Root",
            description="Root node",
            children=[
                TopicNode(
                    topic="Child 1",
                    description="First child",
                    children=[
                        TopicNode(topic="Leaf", description="Leaf node"),
                    ],
                ),
            ],
        )
        assert len(root.children) == 1
        assert len(root.children[0].children) == 1
        assert root.children[0].children[0].topic == "Leaf"


class TestThoughtBranch:
    def test_recursive_structure(self):
        branch = ThoughtBranch(
            hypothesis="Data skew",
            evidence=["Uneven task durations", "One partition 10x larger"],
            confidence=0.85,
            children=[
                ThoughtBranch(
                    hypothesis="Customer ID skew",
                    confidence=0.9,
                    is_solution=True,
                ),
            ],
        )
        assert branch.children[0].is_solution is True


class TestICMTicket:
    def test_creation(self):
        ticket = ICMTicket(
            ticket_id="INC-001",
            title="Test incident",
            description="Test",
            severity=Severity.HIGH,
            status=AlertStatus.NEW,
            created_at=datetime.utcnow(),
        )
        assert ticket.ticket_id == "INC-001"
        assert ticket.ttd_minutes is None


class TestRoutingDecision:
    def test_confidence_bounds(self):
        routing = RoutingDecision(
            failure_id="test",
            target_channel="test-channel",
            target_contacts=["alice@test.com"],
            severity=Severity.HIGH,
            routing_reason="Test routing",
            confidence=0.95,
        )
        assert 0.0 <= routing.confidence <= 1.0

    def test_confidence_validation(self):
        with pytest.raises(Exception):
            RoutingDecision(
                failure_id="test",
                target_channel="test",
                target_contacts=[],
                severity=Severity.LOW,
                routing_reason="Test",
                confidence=1.5,  # Invalid: > 1.0
            )
