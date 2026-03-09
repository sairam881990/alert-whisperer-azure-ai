"""
Tests for the chat engine — intent classification and session management.
"""

import pytest

from src.engine.chat_engine import UserIntent, classify_intent


class TestIntentClassification:
    def test_similar_incidents(self):
        assert classify_intent("Show me similar failures in the last 90 days") == UserIntent.SIMILAR_INCIDENTS
        assert classify_intent("Have we seen this before?") == UserIntent.SIMILAR_INCIDENTS
        assert classify_intent("Any historical incidents like this?") == UserIntent.SIMILAR_INCIDENTS
        assert classify_intent("Is this a recurrence?") == UserIntent.SIMILAR_INCIDENTS

    def test_runbook_lookup(self):
        assert classify_intent("What are the runbook steps?") == UserIntent.RUNBOOK_LOOKUP
        assert classify_intent("How do I fix this?") == UserIntent.RUNBOOK_LOOKUP
        assert classify_intent("Show me the troubleshooting procedure") == UserIntent.RUNBOOK_LOOKUP
        assert classify_intent("Steps to resolve this issue") == UserIntent.RUNBOOK_LOOKUP

    def test_root_cause(self):
        assert classify_intent("What caused this failure?") == UserIntent.ROOT_CAUSE_DETAIL
        assert classify_intent("Explain the root cause") == UserIntent.ROOT_CAUSE_DETAIL
        assert classify_intent("Why did this happen?") == UserIntent.ROOT_CAUSE_DETAIL
        assert classify_intent("Give me more detail about the error") == UserIntent.ROOT_CAUSE_DETAIL

    def test_rerun(self):
        assert classify_intent("Rerun the pipeline") == UserIntent.RERUN_PIPELINE
        assert classify_intent("Can you restart this job?") == UserIntent.RERUN_PIPELINE
        assert classify_intent("Retry the failed task") == UserIntent.RERUN_PIPELINE

    def test_view_logs(self):
        assert classify_intent("Show me the logs") == UserIntent.VIEW_LOGS
        assert classify_intent("What does the stack trace say?") == UserIntent.VIEW_LOGS
        assert classify_intent("Show error log output") == UserIntent.VIEW_LOGS

    def test_escalation(self):
        assert classify_intent("I need to escalate this") == UserIntent.ESCALATION
        assert classify_intent("Page the on-call team") == UserIntent.ESCALATION
        assert classify_intent("This is critical, need help") == UserIntent.ESCALATION

    def test_trend_analysis(self):
        assert classify_intent("What's the trend for this error?") == UserIntent.TREND_ANALYSIS
        assert classify_intent("How often does this happen?") == UserIntent.TREND_ANALYSIS
        assert classify_intent("Show me the error frequency pattern") == UserIntent.TREND_ANALYSIS
        assert classify_intent("Is this increasing in the past 7 days?") == UserIntent.TREND_ANALYSIS

    def test_general_qa_fallback(self):
        assert classify_intent("Hello") == UserIntent.GENERAL_QA
        assert classify_intent("What is Spark?") == UserIntent.GENERAL_QA
        assert classify_intent("Tell me about the pipeline") == UserIntent.GENERAL_QA
