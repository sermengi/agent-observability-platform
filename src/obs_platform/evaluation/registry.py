from obs_platform.evaluation.base import Evaluator
from obs_platform.evaluation.evaluators import (
    EvidenceEvaluator,
    PolicyEvaluator,
    StructuredOutputEvaluator,
    ToolExecutionEvaluator,
    TrajectoryEvaluator,
)
from obs_platform.evaluation.judges.groundedness import GroundednessJudge
from obs_platform.evaluation.judges.uncertainty import UncertaintyJudge

DETERMINISTIC_EVALUATORS: list[Evaluator] = [
    ToolExecutionEvaluator(),
    StructuredOutputEvaluator(),
    TrajectoryEvaluator(),
    PolicyEvaluator(),
    EvidenceEvaluator(),
    GroundednessJudge(),
    UncertaintyJudge(),
]
