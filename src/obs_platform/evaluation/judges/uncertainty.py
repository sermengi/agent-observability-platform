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


class OverconfidentClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_text: str
    evidence_gap: str
    explanation: str


class UncertaintyJudgeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    reason: str
    overconfident_claims: list[OverconfidentClaim]


class UncertaintyJudge(Evaluator):
    name = "uncertainty"
    version = "1.0.0"
    type = EvaluatorType.LLM_BASED

    def __init__(self, judge_client: JudgeClient | None = None) -> None:
        self._judge_client = judge_client

    def with_judge_client(self, judge_client: JudgeClient) -> "UncertaintyJudge":
        return UncertaintyJudge(judge_client)

    def evaluate(self, run: EvaluationRunView) -> EvaluationResult:
        raise NotImplementedError("UncertaintyJudge must be evaluated asynchronously")

    async def evaluate_async(
        self,
        run: EvaluationRunView,
        call_log: list[JudgeCallResult[Any]],
    ) -> EvaluationResult:
        if (not_applicable := judge_not_applicable_result(run)) is not None:
            return not_applicable

        if self._judge_client is None:
            raise RuntimeError("uncertainty judge client is not configured")

        judge_result = await self._judge_client.generate_structured(
            prompt=_uncertainty_prompt(run),
            response_model=UncertaintyJudgeOutput,
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
                    code="overconfident_hypothesis",
                    message=claim.claim_text,
                    data={
                        "evidence_gap": claim.evidence_gap,
                        "explanation": claim.explanation,
                    },
                )
                for claim in output.overconfident_claims
            ],
        )


def _uncertainty_prompt(run: EvaluationRunView) -> str:
    return judge_prompt(
        task=(
            "Judge whether the final answer states hypotheses or inferences as "
            "settled fact without appropriate uncertainty. Identify any "
            "overconfident hypotheses."
        ),
        run=run,
    )
