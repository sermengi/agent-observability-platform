from obs_platform.evaluation.base import Evaluator
from obs_platform.evaluation.types import (
    EvaluationFinding,
    EvaluationResult,
    EvaluationRunView,
    EvaluatorType,
)
from obs_platform.telemetry.v1.enums import ExecutionStatus, RunStatus


class ToolExecutionEvaluator(Evaluator):
    name = "tool_execution"
    version = "1.0.0"
    type = EvaluatorType.DETERMINISTIC

    def evaluate(self, run: EvaluationRunView) -> EvaluationResult:
        total_calls = len(run.tool_calls)
        success_count = sum(
            1
            for tool_call in run.tool_calls
            if tool_call.status is ExecutionStatus.SUCCESS
        )
        findings = [
            EvaluationFinding(
                code=_finding_code(tool_call.status),
                message=(
                    f"Tool call {tool_call.tool_call_id} ended with "
                    f"{tool_call.status.value}"
                ),
                data={
                    "tool_call_id": tool_call.tool_call_id,
                    "tool_name": tool_call.tool_name,
                    "status": tool_call.status.value,
                    "retry_count": tool_call.retry_count,
                    "error_category": tool_call.error_category,
                    "error_message": tool_call.error_message,
                },
            )
            for tool_call in run.tool_calls
            if tool_call.status is not ExecutionStatus.SUCCESS
        ]

        passed = not findings
        return EvaluationResult(
            passed=passed,
            score=(success_count / total_calls if total_calls > 0 else None),
            label="pass" if passed else "fail",
            severity=None,
            reason=f"{success_count}/{total_calls} tool calls succeeded",
            findings=findings,
        )


class StructuredOutputEvaluator(Evaluator):
    name = "structured_output"
    version = "1.0.0"
    type = EvaluatorType.DETERMINISTIC

    def evaluate(self, run: EvaluationRunView) -> EvaluationResult:
        if run.status is not RunStatus.SUCCESS:
            return EvaluationResult(
                passed=True,
                score=None,
                label="not_applicable",
                severity=None,
                reason="final result output is not expected for this run status",
                findings=[],
            )

        if run.final_result_output:
            return EvaluationResult(
                passed=True,
                score=None,
                label="pass",
                severity=None,
                reason="final result output is non-empty",
                findings=[],
            )

        return EvaluationResult(
            passed=False,
            score=None,
            label="fail",
            severity=None,
            reason="final result output is empty",
            findings=[
                EvaluationFinding(
                    code="empty_output",
                    message="Final result output is empty",
                    data={"run_id": run.run_id},
                )
            ],
        )


def _finding_code(status: ExecutionStatus) -> str:
    if status is ExecutionStatus.FAILURE:
        return "tool_call_failed"
    return "tool_call_error"
