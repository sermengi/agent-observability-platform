from obs_platform.evaluation.base import Evaluator
from obs_platform.evaluation.evaluators import (
    StructuredOutputEvaluator,
    ToolExecutionEvaluator,
)

DETERMINISTIC_EVALUATORS: list[Evaluator] = [
    ToolExecutionEvaluator(),
    StructuredOutputEvaluator(),
]
