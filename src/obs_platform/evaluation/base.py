from abc import ABC, abstractmethod
from typing import ClassVar

from obs_platform.evaluation.types import (
    EvaluationResult,
    EvaluationRunView,
    EvaluatorType,
)


class Evaluator(ABC):
    name: ClassVar[str]
    version: ClassVar[str]
    type: ClassVar[EvaluatorType]

    @abstractmethod
    def evaluate(self, run: EvaluationRunView) -> EvaluationResult:
        raise NotImplementedError
