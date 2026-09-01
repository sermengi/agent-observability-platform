import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from obs_platform.evaluation.base import Evaluator
from obs_platform.evaluation.judges.client import JudgeCallResult, JudgeClient
from obs_platform.evaluation.types import (
    EvaluationFinding,
    EvaluationResult,
    EvaluationRunView,
    EvaluatorType,
)
from obs_platform.telemetry.v1.enums import RunStatus


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

    def __init__(self, judge_client: JudgeClient) -> None:
        self._judge_client = judge_client

    def evaluate(self, run: EvaluationRunView) -> EvaluationResult:
        raise NotImplementedError("GroundednessJudge must be evaluated asynchronously")

    async def evaluate_async(
        self,
        run: EvaluationRunView,
        call_log: list[JudgeCallResult[Any]],
    ) -> EvaluationResult:
        if run.status is not RunStatus.SUCCESS:
            return EvaluationResult(
                passed=True,
                score=None,
                label="not_applicable",
                severity=None,
                reason="final result output is not expected for this run status",
                findings=[],
            )

        judge_result = await self._judge_client.generate_structured(
            prompt=_groundedness_prompt(run),
            response_model=GroundednessJudgeOutput,
        )
        call_log.append(judge_result)
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
    evidence = [
        {
            "tool_call_id": tool_call.tool_call_id,
            "tool_name": tool_call.tool_name,
            "result": tool_call.result,
        }
        for tool_call in run.tool_calls
        if tool_call.result is not None
    ]
    payload = {
        "task": (
            "Judge whether the final answer is grounded in the provided tool-result "
            "evidence. Identify any unsupported claims."
        ),
        "evidence": evidence,
        "final_answer": run.final_result_output,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
