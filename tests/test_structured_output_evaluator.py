from datetime import UTC, datetime
from typing import Any

from obs_platform.evaluation.evaluators import StructuredOutputEvaluator
from obs_platform.evaluation.registry import ALL_EVALUATORS
from obs_platform.evaluation.types import EvaluationRunView, EvaluatorType


def test_success_run_with_non_empty_final_result_output_passes() -> None:
    result = StructuredOutputEvaluator().evaluate(
        _run_with_final_result(status="success", final_result_output={"value": 1})
    )

    assert result.passed is True
    assert result.score is None
    assert result.label == "pass"
    assert result.severity is None
    assert result.findings == []


def test_success_run_with_empty_final_result_output_fails() -> None:
    result = StructuredOutputEvaluator().evaluate(
        _run_with_final_result(status="success", final_result_output={})
    )

    assert result.passed is False
    assert result.score is None
    assert result.label == "fail"
    assert result.severity is None
    assert result.reason == "final result output is empty"
    assert len(result.findings) == 1
    assert result.findings[0].code == "empty_output"
    assert result.findings[0].data == {"run_id": "run-structured-output"}


def test_runs_without_expected_final_result_are_not_applicable() -> None:
    for status in ("tool_error", "runtime_error", "awaiting_approval"):
        result = StructuredOutputEvaluator().evaluate(
            _run_with_final_result(status=status, final_result_output=None)
        )

        assert result.passed is True
        assert result.score is None
        assert result.label == "not_applicable"
        assert result.severity is None
        assert result.findings == []


def test_output_internal_keys_and_source_references_are_not_inspected() -> None:
    result = StructuredOutputEvaluator().evaluate(
        _run_with_final_result(
            status="success",
            final_result_output={"producer_specific_payload": {"opaque": True}},
            final_result_source_references=[],
        )
    )

    assert result.passed is True
    assert result.label == "pass"
    assert result.findings == []


def test_structured_output_evaluator_metadata_and_registry_entry() -> None:
    evaluator = StructuredOutputEvaluator()

    assert evaluator.name == "structured_output"
    assert evaluator.version == "1.0.0"
    assert evaluator.type is EvaluatorType.DETERMINISTIC
    assert any(
        registered.name == evaluator.name
        and registered.version == evaluator.version
        and registered.type is EvaluatorType.DETERMINISTIC
        for registered in ALL_EVALUATORS
    )


def _run_with_final_result(
    *,
    status: str,
    final_result_output: dict[str, Any] | None,
    final_result_source_references: list[str] | None = None,
) -> EvaluationRunView:
    timestamp = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    return EvaluationRunView(
        run_id="run-structured-output",
        schema_version="1.0.0",
        event_type=(
            "run_awaiting_approval" if status == "awaiting_approval" else "run_final"
        ),
        agent_name="maintenance-agent",
        agent_version="agent-v1",
        prompt_version="prompt-v1",
        environment="test",
        raw_input={"query": "status"},
        normalized_input="status",
        scenario_id=None,
        started_at=timestamp,
        completed_at=(None if status == "awaiting_approval" else timestamp),
        status=status,
        execution_latency_ms=(None if status == "awaiting_approval" else 100),
        wall_clock_duration_ms=(None if status == "awaiting_approval" else 110),
        resume_count=0,
        hitl_required=(status == "awaiting_approval"),
        hitl_state=("pending" if status == "awaiting_approval" else "not_required"),
        hitl_checkpoint_id=None,
        hitl_decision=None,
        hitl_requested_at=None,
        hitl_decided_at=None,
        hitl_pending_action=None,
        usage_total_llm_calls=0,
        usage_total_tool_calls=0,
        usage_total_tokens=0,
        usage_total_retries=0,
        usage_total_estimated_cost_usd=0.0,
        final_result_output=final_result_output,
        final_result_source_references=final_result_source_references or [],
        runtime_error_category=None,
        runtime_error_code=None,
        runtime_error_message=None,
        runtime_error_failed_component=None,
        spans=[],
        tool_calls=[],
        llm_calls=[],
    )
