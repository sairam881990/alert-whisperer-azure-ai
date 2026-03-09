"""Prompt Engineering Module — Templates, Engine, and Few-Shot Examples."""

from src.prompts.prompt_engine import PromptEngine, RootCauseOutput, RoutingOutput, SeverityOutput
from src.prompts.templates import (
    CURATED_FEW_SHOT_EXAMPLES,
    GLOBAL_KNOWLEDGE_PROMPT,
    SOLUTION_PROMPT,
    STYLE_GUIDE_PROMPT,
    TEMPLATE_REGISTRY,
    format_few_shot_examples,
    get_template,
)

__all__ = [
    "PromptEngine",
    "RootCauseOutput",
    "RoutingOutput",
    "SeverityOutput",
    "GLOBAL_KNOWLEDGE_PROMPT",
    "STYLE_GUIDE_PROMPT",
    "SOLUTION_PROMPT",
    "TEMPLATE_REGISTRY",
    "CURATED_FEW_SHOT_EXAMPLES",
    "format_few_shot_examples",
    "get_template",
]
