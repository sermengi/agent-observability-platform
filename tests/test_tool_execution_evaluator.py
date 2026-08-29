from datetime import UTC, datetime

from obs_platform.evaluation.evaluators import ToolExecutionEvaluator
from obs_platform.evaluation.registry import DETERMINISTIC_EVALUATORS
from obs_platform.evaluation.types import EvaluationRunView, EvaluatorType


def test_all_successful_tool_calls_pass() -> None:
    result = ToolExecutionEvaluator().evaluate(
        _run_with_tool_calls(
            [
                {"tool_call_id": "call-1", "tool_name": "resolve_asset"},
                {"tool_call_id": "call-2", "tool_name": "get_asset_status"},
            ]
        )
    )

    assert result.passed is True
    assert result.score == 1.0
    assert result.label == "pass"
    assert result.severity is None
    assert result.reason == "2/2 tool calls succeeded"
    assert result.findings == []


def test_mixed_tool_call_statuses_fail_with_one_finding_per_bad_call() -> None:
    result = ToolExecutionEvaluator().evaluate(
        _run_with_tool_calls(
            [
                {"tool_call_id": "call-1", "tool_name": "resolve_asset"},
                {
                    "tool_call_id": "call-2",
                    "tool_name": "get_asset_status",
                    "status": "failure",
                    "retry_count": 1,
                    "error_category": "tool",
                    "error_message": "asset unavailable",
                },
                {
                    "tool_call_id": "call-3",
                    "tool_name": "get_maintenance_history",
                    "status": "error",
                    "retry_count": 2,
                    "error_category": "runtime",
                    "error_message": "timeout",
                },
            ]
        )
    )

    assert result.passed is False
    assert result.score == 1 / 3
    assert result.label == "fail"
    assert result.severity is None
    assert result.reason == "1/3 tool calls succeeded"
    assert [finding.code for finding in result.findings] == [
        "tool_call_failed",
        "tool_call_error",
    ]
    assert result.findings[0].data == {
        "tool_call_id": "call-2",
        "tool_name": "get_asset_status",
        "status": "failure",
        "retry_count": 1,
        "error_category": "tool",
        "error_message": "asset unavailable",
    }
    assert result.findings[1].data == {
        "tool_call_id": "call-3",
        "tool_name": "get_maintenance_history",
        "status": "error",
        "retry_count": 2,
        "error_category": "runtime",
        "error_message": "timeout",
    }


def test_zero_tool_calls_pass_with_no_score() -> None:
    result = ToolExecutionEvaluator().evaluate(_run_with_tool_calls([]))

    assert result.passed is True
    assert result.score is None
    assert result.label == "pass"
    assert result.severity is None
    assert result.reason == "0/0 tool calls succeeded"
    assert result.findings == []


def test_high_retry_count_on_success_does_not_fail_or_create_finding() -> None:
    result = ToolExecutionEvaluator().evaluate(
        _run_with_tool_calls(
            [
                {
                    "tool_call_id": "call-1",
                    "tool_name": "get_asset_status",
                    "retry_count": 99,
                }
            ]
        )
    )

    assert result.passed is True
    assert result.score == 1.0
    assert result.label == "pass"
    assert result.severity is None
    assert result.findings == []


def test_tool_execution_evaluator_metadata_and_registry_entry() -> None:
    evaluator = ToolExecutionEvaluator()

    assert evaluator.name == "tool_execution"
    assert evaluator.version == "1.0.0"
    assert evaluator.type is EvaluatorType.DETERMINISTIC
    assert any(
        registered.name == evaluator.name
        and registered.version == evaluator.version
        and registered.type is EvaluatorType.DETERMINISTIC
        for registered in DETERMINISTIC_EVALUATORS
    )


def _run_with_tool_calls(
    tool_calls: list[dict[str, object]],
) -> EvaluationRunView:
    timestamp = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    return EvaluationRunView(
        run_id="run-1",
        schema_version="1.0.0",
        event_type="run_final",
        agent_name="maintenance-agent",
        agent_version="agent-v1",
        prompt_version="prompt-v1",
        environment="test",
        raw_input={"query": "status"},
        normalized_input="status",
        scenario_id=None,
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
        usage_total_llm_calls=0,
        usage_total_tool_calls=len(tool_calls),
        usage_total_tokens=0,
        usage_total_retries=0,
        usage_total_estimated_cost_usd=0.0,
        final_result_output={"answer": "ok"},
        final_result_source_references=[],
        runtime_error_category=None,
        runtime_error_code=None,
        runtime_error_message=None,
        runtime_error_failed_component=None,
        spans=[],
        tool_calls=[
            {
                "tool_call_id": tool_call["tool_call_id"],
                "span_id": "span-1",
                "tool_name": tool_call["tool_name"],
                "sequence": index,
                "arguments": {},
                "result": {},
                "started_at": timestamp,
                "completed_at": timestamp,
                "latency_ms": 20,
                "retry_count": tool_call.get("retry_count", 0),
                "status": tool_call.get("status", "success"),
                "error_category": tool_call.get("error_category"),
                "error_code": tool_call.get("error_code"),
                "error_message": tool_call.get("error_message"),
                "error_failed_component": tool_call.get("error_failed_component"),
            }
            for index, tool_call in enumerate(tool_calls, start=1)
        ],
        llm_calls=[],
    )
