from obs_platform.evaluation.base import Evaluator
from obs_platform.evaluation.contracts import SCENARIO_CONTRACTS, TerminalCondition
from obs_platform.evaluation.types import (
    EvaluationFinding,
    EvaluationResult,
    EvaluationRunView,
    EvaluatorType,
)
from obs_platform.telemetry.v1.enums import (
    ExecutionStatus,
    HITLState,
    RunEventType,
    RunStatus,
)


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


class TrajectoryEvaluator(Evaluator):
    name = "trajectory"
    version = "1.0.0"
    type = EvaluatorType.DETERMINISTIC

    def evaluate(self, run: EvaluationRunView) -> EvaluationResult:
        if run.scenario_id is None or run.scenario_id not in SCENARIO_CONTRACTS:
            return EvaluationResult(
                passed=True,
                score=None,
                label="not_applicable",
                severity=None,
                reason="no scenario contract applies",
                findings=[],
            )

        contract = SCENARIO_CONTRACTS[run.scenario_id]
        findings: list[EvaluationFinding] = []
        satisfied_constraints = 0
        total_constraints = 0

        present_tools = {tool_call.tool_name for tool_call in run.tool_calls}

        for tool_name in contract.required_tools:
            total_constraints += 1
            if tool_name in present_tools:
                satisfied_constraints += 1
            else:
                findings.append(
                    EvaluationFinding(
                        code="missing_required_tool",
                        message=f"Required tool {tool_name} was not called",
                        data={"tool_name": tool_name},
                    )
                )

        for tool_name in contract.forbidden_tools:
            total_constraints += 1
            forbidden_calls = [
                tool_call
                for tool_call in run.tool_calls
                if tool_call.tool_name == tool_name
            ]
            if forbidden_calls:
                for tool_call in forbidden_calls:
                    findings.append(
                        EvaluationFinding(
                            code="forbidden_tool_used",
                            message=f"Forbidden tool {tool_name} was called",
                            data={
                                "tool_name": tool_name,
                                "tool_call_id": tool_call.tool_call_id,
                                "status": tool_call.status.value,
                            },
                        )
                    )
            else:
                satisfied_constraints += 1

        first_sequences = _first_tool_sequences(run)
        for before_tool, after_tool in contract.ordering_constraints:
            if before_tool not in first_sequences or after_tool not in first_sequences:
                continue

            total_constraints += 1
            before_sequence = first_sequences[before_tool]
            after_sequence = first_sequences[after_tool]
            if before_sequence < after_sequence:
                satisfied_constraints += 1
            else:
                findings.append(
                    EvaluationFinding(
                        code="ordering_violation",
                        message=(
                            f"Tool {before_tool} did not run before {after_tool}"
                        ),
                        data={
                            "before_tool": before_tool,
                            "after_tool": after_tool,
                            "before_sequence": before_sequence,
                            "after_sequence": after_sequence,
                        },
                    )
                )

        if contract.terminal is not None:
            terminal_mismatches = _terminal_mismatches(run, contract.terminal)
            for mismatch in terminal_mismatches:
                total_constraints += 1
                findings.append(
                    EvaluationFinding(
                        code="terminal_condition_mismatch",
                        message=(
                            f"Terminal condition {mismatch['field']} did not match"
                        ),
                        data=mismatch,
                    )
                )
            expected_terminal_checks = _terminal_check_count(contract.terminal)
            satisfied_constraints += expected_terminal_checks - len(
                terminal_mismatches
            )
            total_constraints += expected_terminal_checks - len(terminal_mismatches)

        passed = not findings
        return EvaluationResult(
            passed=passed,
            score=(
                satisfied_constraints / total_constraints
                if total_constraints > 0
                else None
            ),
            label="pass" if passed else "fail",
            severity=None,
            reason=(
                f"{satisfied_constraints}/{total_constraints} "
                "trajectory constraints satisfied"
            ),
            findings=findings,
        )


def _finding_code(status: ExecutionStatus) -> str:
    if status is ExecutionStatus.FAILURE:
        return "tool_call_failed"
    return "tool_call_error"


def _first_tool_sequences(run: EvaluationRunView) -> dict[str, int]:
    first_sequences: dict[str, int] = {}
    for tool_call in sorted(run.tool_calls, key=lambda call: call.sequence):
        first_sequences.setdefault(tool_call.tool_name, tool_call.sequence)
    return first_sequences


def _terminal_mismatches(
    run: EvaluationRunView,
    terminal: TerminalCondition,
) -> list[dict[str, object]]:
    checks: list[tuple[str, object | None, object]] = [
        ("status", terminal.expected_status, run.status),
        ("event_type", terminal.expected_event_type, run.event_type),
        ("hitl_required", terminal.expected_hitl_required, run.hitl_required),
        ("hitl_state", terminal.expected_hitl_state, run.hitl_state),
    ]
    return [
        {
            "field": field,
            "expected": _value(expected),
            "actual": _value(actual),
        }
        for field, expected, actual in checks
        if expected is not None and actual != expected
    ]


def _terminal_check_count(terminal: TerminalCondition) -> int:
    return sum(
        expected is not None
        for expected in (
            terminal.expected_status,
            terminal.expected_event_type,
            terminal.expected_hitl_required,
            terminal.expected_hitl_state,
        )
    )


def _value(value: object) -> object:
    if isinstance(value, RunStatus | RunEventType | HITLState | ExecutionStatus):
        return value.value
    return value
