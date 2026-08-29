from obs_platform.evaluation.base import Evaluator
from obs_platform.evaluation.evaluators import ToolExecutionEvaluator

DETERMINISTIC_EVALUATORS: list[Evaluator] = [ToolExecutionEvaluator()]
