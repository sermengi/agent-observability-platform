from obs_platform.evaluation.base import Evaluator
from obs_platform.evaluation.evaluators import (
    PolicyEvaluator,
    StructuredOutputEvaluator,
    ToolExecutionEvaluator,
    TrajectoryEvaluator,
)

DETERMINISTIC_EVALUATORS: list[Evaluator] = [
    ToolExecutionEvaluator(),
    StructuredOutputEvaluator(),
    TrajectoryEvaluator(),
    PolicyEvaluator(),
]
