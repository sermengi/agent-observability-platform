from datetime import UTC, datetime
from inspect import iscoroutinefunction

from obs_platform.db.models import EvaluationResult as EvaluationResultRecord
from obs_platform.evaluation.base import Evaluator
from obs_platform.evaluation.registry import DETERMINISTIC_EVALUATORS
from obs_platform.evaluation.types import (
    EvaluationFinding,
    EvaluationResult,
    EvaluationRunView,
    EvaluatorType,
    LLMCallView,
    SpanView,
    ToolCallView,
)
from obs_platform.telemetry.v1.enums import (
    ExecutionStatus,
    HITLState,
    LLMCallType,
    RunEventType,
    RunStatus,
)


def test_evaluation_run_view_is_constructible_from_plain_literals() -> None:
    timestamp = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

    run = EvaluationRunView(
        run_id="run-1",
        schema_version="1.0.0",
        event_type="run_final",
        agent_name="maintenance-agent",
        agent_version="agent-v1",
        prompt_version="prompt-v1",
        environment="test",
        raw_input={"query": "status"},
        normalized_input="status",
        scenario_id="GS-08",
        started_at=timestamp,
        completed_at=timestamp,
        status="success",
        execution_latency_ms=100,
        wall_clock_duration_ms=110,
        resume_count=0,
        hitl_required=False,
        hitl_state="not_required",
        hitl_checkpoint_id=None,
        hitl_decision=None,
        hitl_requested_at=None,
        hitl_decided_at=None,
        hitl_pending_action=None,
        usage_total_llm_calls=1,
        usage_total_tool_calls=1,
        usage_total_tokens=42,
        usage_total_retries=0,
        usage_total_estimated_cost_usd=0.01,
        final_result_output={"answer": "ok"},
        final_result_source_references=["doc-1"],
        runtime_error_category=None,
        runtime_error_code=None,
        runtime_error_message=None,
        runtime_error_failed_component=None,
        spans=[
            {
                "span_id": "span-1",
                "parent_span_id": None,
                "name": "root",
                "sequence": 1,
                "started_at": timestamp,
                "completed_at": timestamp,
                "status": "success",
                "input": {"query": "status"},
                "output": {"answer": "ok"},
                "metadata": {"phase": "test"},
                "error_category": None,
                "error_code": None,
                "error_message": None,
                "error_failed_component": None,
            }
        ],
        tool_calls=[
            {
                "tool_call_id": "tool-1",
                "span_id": "span-1",
                "tool_name": "get_asset_status",
                "sequence": 2,
                "arguments": {"asset_id": "pump-1"},
                "result": {"status": "healthy"},
                "started_at": timestamp,
                "completed_at": timestamp,
                "latency_ms": 20,
                "retry_count": 0,
                "status": "success",
                "error_category": None,
                "error_code": None,
                "error_message": None,
                "error_failed_component": None,
            }
        ],
        llm_calls=[
            {
                "llm_call_id": "llm-1",
                "span_id": "span-1",
                "sequence": 3,
                "call_type": "synthesis",
                "model": "test-model",
                "provider": "test-provider",
                "started_at": timestamp,
                "completed_at": timestamp,
                "latency_ms": 50,
                "prompt_tokens": 20,
                "completion_tokens": 22,
                "total_tokens": 42,
                "estimated_cost_usd": 0.01,
                "input_payload": {"messages": []},
                "output_payload": {"answer": "ok"},
                "status": "success",
                "error_category": None,
                "error_code": None,
                "error_message": None,
                "error_failed_component": None,
            }
        ],
    )

    assert run.status is RunStatus.SUCCESS
    assert run.event_type is RunEventType.RUN_FINAL
    assert run.hitl_state is HITLState.NOT_REQUIRED
    assert isinstance(run.spans[0], SpanView)
    assert isinstance(run.tool_calls[0], ToolCallView)
    assert isinstance(run.llm_calls[0], LLMCallView)
    assert run.tool_calls[0].status is ExecutionStatus.SUCCESS
    assert run.llm_calls[0].call_type is LLMCallType.SYNTHESIS


def test_evaluation_result_has_no_execution_status_field() -> None:
    assert "status" not in EvaluationResult.model_fields
    assert "execution_status" not in EvaluationResult.model_fields


def test_findings_are_structured_objects() -> None:
    result = EvaluationResult(
        passed=False,
        score=0.0,
        label="fail",
        severity=None,
        reason="failed",
        findings=[
            {
                "code": "example_failure",
                "message": "Example failure",
                "data": {"tool_name": "get_asset_status"},
            }
        ],
    )

    assert isinstance(result.findings[0], EvaluationFinding)
    assert result.findings[0].code == "example_failure"
    assert result.findings[0].data == {"tool_name": "get_asset_status"}


def test_evaluator_interface_uses_class_metadata_and_sync_evaluate() -> None:
    class ExampleEvaluator(Evaluator):
        name = "example"
        version = "1.0.0"
        type = EvaluatorType.DETERMINISTIC

        def evaluate(self, run: EvaluationRunView) -> EvaluationResult:
            return EvaluationResult(
                passed=True,
                score=None,
                label="pass",
                severity=None,
                reason=f"{run.run_id} passed",
                findings=[],
            )

    evaluator = ExampleEvaluator()

    assert evaluator.name == "example"
    assert evaluator.version == "1.0.0"
    assert evaluator.type is EvaluatorType.DETERMINISTIC
    assert not iscoroutinefunction(evaluator.evaluate)


def test_registry_is_plain_static_list() -> None:
    assert isinstance(DETERMINISTIC_EVALUATORS, list)
    assert not hasattr(DETERMINISTIC_EVALUATORS, "register")


def test_evaluator_type_is_not_persisted_on_evaluation_results() -> None:
    assert "type" not in EvaluationResultRecord.__table__.columns
    assert "evaluator_type" not in EvaluationResultRecord.__table__.columns
