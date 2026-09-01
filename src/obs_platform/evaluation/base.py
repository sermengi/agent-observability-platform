from abc import ABC, abstractmethod
from typing import Any, ClassVar

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

    async def evaluate_async(
        self,
        run: EvaluationRunView,
        call_log: list[Any],
    ) -> EvaluationResult:
        raise NotImplementedError
