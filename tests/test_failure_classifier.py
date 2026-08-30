from obs_platform.evaluation.classifier import (
    FAILURE_TYPE_PRIORITY,
    FAILURE_TYPE_SEVERITY,
    EvaluatorOutcome,
    FailureClassifier,
)
from obs_platform.evaluation.types import (
    EvaluationFinding,
    EvaluationResult,
    EvaluatorExecutionStatus,
    FailureType,
)


def test_failure_type_severity_covers_all_failure_types() -> None:
    assert set(FAILURE_TYPE_SEVERITY) == set(FailureType)
    assert FAILURE_TYPE_SEVERITY == {
        FailureType.POLICY_VIOLATION: "critical",
        FailureType.UNSUPPORTED_CLAIM: "error",
        FailureType.OUTPUT_VALIDATION_ERROR: "error",
        FailureType.TRAJECTORY_ERROR: "error",
        FailureType.TOOL_FAILURE: "error",
        FailureType.RETRIEVAL_FAILURE: "warning",
        FailureType.UNKNOWN: None,
    }


def test_classifier_max_severity_comes_from_failure_type_mapping() -> None:
    result = FailureClassifier().classify(
        [
            _outcome(
                [
                    EvaluationFinding(
                        code="missing_required_evidence",
                        message="missing evidence",
                        data={"severity": "critical"},
                    )
                ],
                severity="critical",
            )
        ]
    )

    assert result.primary_category is FailureType.RETRIEVAL_FAILURE
    assert result.max_severity == "warning"


def test_policy_finding_is_primary_policy_violation() -> None:
    result = FailureClassifier().classify(
        [
            _outcome(
                [
                    EvaluationFinding(
                        code="unauthorized_consequential_action",
                        message="unauthorized action",
                        data={},
                    )
                ]
            )
        ]
    )

    assert result.primary_category is FailureType.POLICY_VIOLATION
    assert result.secondary_category is None
    assert result.max_severity == "critical"


def test_unknown_asset_policy_finding_maps_to_trajectory_error() -> None:
    result = FailureClassifier().classify(
        [
            _outcome(
                [
                    EvaluationFinding(
                        code="unknown_asset_downstream_call",
                        message="invalid workflow",
                        data={},
                    )
                ]
            )
        ]
    )

    assert result.primary_category is FailureType.TRAJECTORY_ERROR
    assert result.secondary_category is None
    assert result.max_severity == "error"


def test_policy_violation_wins_primary_when_other_failure_types_exist() -> None:
    result = FailureClassifier().classify(
        [
            _outcome(
                [
                    EvaluationFinding(
                        code="missing_required_evidence",
                        message="missing evidence",
                        data={},
                    )
                ]
            ),
            _outcome(
                [
                    EvaluationFinding(
                        code="unauthorized_consequential_action",
                        message="unauthorized action",
                        data={},
                    )
                ]
            ),
        ]
    )

    assert result.primary_category is FailureType.POLICY_VIOLATION
    assert result.secondary_category is FailureType.RETRIEVAL_FAILURE
    assert result.max_severity == "critical"


def test_non_policy_failures_use_static_priority_order() -> None:
    result = FailureClassifier().classify(
        [
            _outcome(
                [
                    EvaluationFinding(
                        code="missing_required_tool",
                        message="missing tool",
                        data={},
                    )
                ]
            ),
            _outcome(
                [
                    EvaluationFinding(
                        code="tool_call_failed",
                        message="tool failed",
                        data={},
                    )
                ]
            ),
        ]
    )

    assert FAILURE_TYPE_PRIORITY.index(FailureType.TOOL_FAILURE) < (
        FAILURE_TYPE_PRIORITY.index(FailureType.TRAJECTORY_ERROR)
    )
    assert result.primary_category is FailureType.TOOL_FAILURE
    assert result.secondary_category is FailureType.TRAJECTORY_ERROR


def test_max_severity_includes_secondary_failure_type() -> None:
    result = FailureClassifier().classify(
        [
            _outcome(
                [
                    EvaluationFinding(
                        code="missing_required_evidence",
                        message="missing evidence",
                        data={},
                    ),
                    EvaluationFinding(
                        code="unknown_finding_code",
                        message="unknown failure",
                        data={},
                    ),
                ]
            )
        ]
    )

    assert result.primary_category is FailureType.RETRIEVAL_FAILURE
    assert result.secondary_category is FailureType.UNKNOWN
    assert result.max_severity == "warning"


def test_incomplete_without_completed_failures_has_no_failure_categories() -> None:
    result = FailureClassifier().classify(
        [
            _outcome([], label="pass"),
            EvaluatorOutcome(
                evaluator_name="failed",
                evaluator_version="1.0.0",
                execution_status=EvaluatorExecutionStatus.FAILED,
                result=None,
            ),
        ]
    )

    assert result.primary_category is None
    assert result.secondary_category is None
    assert result.max_severity is None


def test_secondary_category_is_single_nullable_text_column() -> None:
    from sqlalchemy import Text

    from obs_platform.db.models import RunFailure

    column = RunFailure.__table__.c.secondary_category

    assert isinstance(column.type, Text)
    assert column.nullable is True


def _outcome(
    findings: list[EvaluationFinding],
    *,
    severity: str | None = None,
    label: str = "fail",
) -> EvaluatorOutcome:
    passed = label != "fail"
    return EvaluatorOutcome(
        evaluator_name="test",
        evaluator_version="1.0.0",
        execution_status=EvaluatorExecutionStatus.COMPLETED,
        result=EvaluationResult(
            passed=passed,
            score=None,
            label=label,
            severity=severity,
            reason="failed",
            findings=findings,
        ),
    )
