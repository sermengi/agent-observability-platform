from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from obs_platform.evaluation.base import Evaluator
from obs_platform.evaluation.judges.client import JudgeCallResult, JudgeClient
from obs_platform.evaluation.judges.common import (
    judge_not_applicable_result,
    judge_prompt,
)
from obs_platform.evaluation.types import (
    EvaluationFinding,
    EvaluationResult,
    EvaluationRunView,
    EvaluatorType,
)


class UnsupportedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_text: str
    explanation: str


class GroundednessJudgeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    reason: str
    unsupported_claims: list[UnsupportedClaim]


class GroundednessJudge(Evaluator):
    name = "groundedness"
    version = "1.0.0"
    type = EvaluatorType.LLM_BASED

    def __init__(self, judge_client: JudgeClient | None = None) -> None:
        self._judge_client = judge_client

    def with_judge_client(self, judge_client: JudgeClient) -> "GroundednessJudge":
        return GroundednessJudge(judge_client)

    def evaluate(self, run: EvaluationRunView) -> EvaluationResult:
        raise NotImplementedError("GroundednessJudge must be evaluated asynchronously")

    async def evaluate_async(
        self,
        run: EvaluationRunView,
        call_log: list[JudgeCallResult[Any]],
    ) -> EvaluationResult:
        if (not_applicable := judge_not_applicable_result(run)) is not None:
            return not_applicable

        if self._judge_client is None:
            raise RuntimeError("groundedness judge client is not configured")

        judge_result = await self._judge_client.generate_structured(
            prompt=_groundedness_prompt(run),
            response_model=GroundednessJudgeOutput,
            call_log=call_log,
        )
        output = judge_result.output

        return EvaluationResult(
            passed=output.passed,
            score=output.score,
            label="pass" if output.passed else "fail",
            severity=None,
            reason=output.reason,
            findings=[
                EvaluationFinding(
                    code="unsupported_claim",
                    message=claim.claim_text,
                    data={"explanation": claim.explanation},
                )
                for claim in output.unsupported_claims
            ],
        )


def _groundedness_prompt(run: EvaluationRunView) -> str:
    return judge_prompt(
        task=(
            "Judge whether the final answer is grounded in the provided tool-result "
            "evidence. Identify any unsupported claims."
        ),
        run=run,
    )
