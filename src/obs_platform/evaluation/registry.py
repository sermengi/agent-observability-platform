from obs_platform.evaluation.base import Evaluator
from obs_platform.evaluation.evaluators import (
    EvidenceEvaluator,
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
    EvidenceEvaluator(),
]
