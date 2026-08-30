from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from obs_platform.evaluation.types import (
    EvaluationResult,
    EvaluatorExecutionStatus,
    FailureType,
    OverallEvaluationStatus,
)


class SeverityRank(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


FAILURE_TYPE_SEVERITY: dict[FailureType, str | None] = {
    FailureType.POLICY_VIOLATION: "critical",
    FailureType.UNSUPPORTED_CLAIM: "error",
    FailureType.OUTPUT_VALIDATION_ERROR: "error",
    FailureType.TRAJECTORY_ERROR: "error",
    FailureType.TOOL_FAILURE: "error",
    FailureType.RETRIEVAL_FAILURE: "warning",
    FailureType.UNKNOWN: None,
}

FINDING_TO_FAILURE_TYPE: dict[str, FailureType] = {
    "tool_call_failed": FailureType.TOOL_FAILURE,
    "tool_call_error": FailureType.TOOL_FAILURE,
    "empty_output": FailureType.OUTPUT_VALIDATION_ERROR,
    "missing_required_tool": FailureType.TRAJECTORY_ERROR,
    "forbidden_tool_used": FailureType.TRAJECTORY_ERROR,
    "ordering_violation": FailureType.TRAJECTORY_ERROR,
    "terminal_condition_mismatch": FailureType.TRAJECTORY_ERROR,
    "missing_required_evidence": FailureType.RETRIEVAL_FAILURE,
    "unauthorized_consequential_action": FailureType.POLICY_VIOLATION,
    "unknown_asset_downstream_call": FailureType.TRAJECTORY_ERROR,
}

FAILURE_TYPE_PRIORITY: list[FailureType] = [
    FailureType.POLICY_VIOLATION,
    FailureType.OUTPUT_VALIDATION_ERROR,
    FailureType.TOOL_FAILURE,
    FailureType.TRAJECTORY_ERROR,
    FailureType.RETRIEVAL_FAILURE,
    FailureType.UNSUPPORTED_CLAIM,
    FailureType.UNKNOWN,
]

_SEVERITY_ORDER = {
    "info": 0,
    "warning": 1,
    "error": 2,
    "critical": 3,
}


class EvaluatorOutcome(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    evaluator_name: str
    evaluator_version: str
    execution_status: EvaluatorExecutionStatus
    result: EvaluationResult | None


class RunFailureResult(BaseModel):
    overall_status: OverallEvaluationStatus
    primary_category: FailureType | None
    secondary_category: FailureType | None
    max_severity: str | None


class FailureClassifier:
    name = "failure_classifier"
    version = "1.0.0"

    def classify(self, outcomes: list[EvaluatorOutcome]) -> RunFailureResult:
        overall_status = _overall_status(outcomes)
        detected_types = _detected_failure_types(outcomes)

        if overall_status is not OverallEvaluationStatus.FAIL or not detected_types:
            return RunFailureResult(
                overall_status=overall_status,
                primary_category=None,
                secondary_category=None,
                max_severity=None,
            )

        ordered_types = [
            failure_type
            for failure_type in FAILURE_TYPE_PRIORITY
            if failure_type in detected_types
        ]
        return RunFailureResult(
            overall_status=overall_status,
            primary_category=ordered_types[0],
            secondary_category=(ordered_types[1] if len(ordered_types) > 1 else None),
            max_severity=_max_severity(ordered_types),
        )


def _overall_status(outcomes: list[EvaluatorOutcome]) -> OverallEvaluationStatus:
    if any(
        outcome.execution_status is EvaluatorExecutionStatus.COMPLETED
        and outcome.result is not None
        and outcome.result.label == "fail"
        for outcome in outcomes
    ):
        return OverallEvaluationStatus.FAIL
    if any(
        outcome.execution_status is EvaluatorExecutionStatus.FAILED
        for outcome in outcomes
    ):
        return OverallEvaluationStatus.INCOMPLETE
    return OverallEvaluationStatus.PASS


def _detected_failure_types(outcomes: list[EvaluatorOutcome]) -> set[FailureType]:
    failure_types: set[FailureType] = set()
    for outcome in outcomes:
        if (
            outcome.execution_status is not EvaluatorExecutionStatus.COMPLETED
            or outcome.result is None
            or outcome.result.label != "fail"
        ):
            continue
        for finding in outcome.result.findings:
            failure_types.add(
                FINDING_TO_FAILURE_TYPE.get(finding.code, FailureType.UNKNOWN)
            )
    return failure_types


def _max_severity(failure_types: list[FailureType]) -> str | None:
    severities = []
    for failure_type in failure_types:
        severity = FAILURE_TYPE_SEVERITY[failure_type]
        if severity is not None:
            severities.append(severity)
    if not severities:
        return None
    return max(severities, key=lambda severity: _SEVERITY_ORDER[severity])
