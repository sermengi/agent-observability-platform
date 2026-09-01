import json
from typing import Any

from obs_platform.evaluation.types import EvaluationResult, EvaluationRunView
from obs_platform.telemetry.v1.enums import RunStatus


def judge_not_applicable_result(run: EvaluationRunView) -> EvaluationResult | None:
    if run.status is RunStatus.SUCCESS:
        return None
    return EvaluationResult(
        passed=True,
        score=None,
        label="not_applicable",
        severity=None,
        reason="final result output is not expected for this run status",
        findings=[],
    )


def tool_result_evidence(run: EvaluationRunView) -> list[dict[str, Any]]:
    return [
        {
            "tool_call_id": tool_call.tool_call_id,
            "tool_name": tool_call.tool_name,
            "result": tool_call.result,
        }
        for tool_call in run.tool_calls
        if tool_call.result is not None
    ]


def judge_prompt(*, task: str, run: EvaluationRunView) -> str:
    payload = {
        "task": task,
        "evidence": tool_result_evidence(run),
        "final_answer": run.final_result_output,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
