from obs_platform.evaluation.classifier import (
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


def _outcome(
    findings: list[EvaluationFinding],
    *,
    severity: str | None,
) -> EvaluatorOutcome:
    return EvaluatorOutcome(
        evaluator_name="test",
        evaluator_version="1.0.0",
        execution_status=EvaluatorExecutionStatus.COMPLETED,
        result=EvaluationResult(
            passed=False,
            score=None,
            label="fail",
            severity=severity,
            reason="failed",
            findings=findings,
        ),
    )
