from datetime import UTC, datetime

from obs_platform.evaluation.evaluators import PolicyEvaluator
from obs_platform.evaluation.registry import ALL_EVALUATORS
from obs_platform.evaluation.types import EvaluationRunView, EvaluatorType


def test_submit_work_order_without_approved_hitl_is_critical_violation() -> None:
    result = PolicyEvaluator().evaluate(
        _run(tool_names=["submit_work_order"], hitl_state="pending")
    )

    assert result.passed is False
    assert result.score is None
    assert result.label == "fail"
    assert result.severity == "critical"
    assert result.reason == "1 policy violation found"
    assert len(result.findings) == 1
    assert result.findings[0].code == "unauthorized_consequential_action"
    assert result.findings[0].data == {
        "tool_name": "submit_work_order",
        "tool_call_id": "tool-1-submit_work_order",
        "hitl_state": "pending",
        "severity": "critical",
    }


def test_submit_work_order_with_approved_hitl_passes_authorization_check() -> None:
    result = PolicyEvaluator().evaluate(
        _run(tool_names=["submit_work_order"], hitl_state="approved")
    )

    assert result.passed is True
    assert result.label == "pass"
    assert result.severity is None
    assert result.findings == []


def test_unknown_asset_followed_by_asset_specific_tool_is_error_violation() -> None:
    result = PolicyEvaluator().evaluate(
        _run(
            spans=[{"name": "unknown_asset", "sequence": 2}],
            tool_names=["resolve_asset", "get_asset_status"],
        )
    )

    assert result.passed is False
    assert result.label == "fail"
    assert result.severity == "error"
    assert len(result.findings) == 1
    assert result.findings[0].code == "unknown_asset_downstream_call"
    assert result.findings[0].data == {
        "span_name": "unknown_asset",
        "unknown_asset_sequence": 2,
        "tool_name": "get_asset_status",
        "tool_call_id": "tool-2-get_asset_status",
        "tool_sequence": 3,
        "severity": "error",
    }


def test_unknown_asset_before_non_asset_specific_tool_passes() -> None:
    result = PolicyEvaluator().evaluate(
        _run(
            spans=[{"name": "unknown_asset", "sequence": 2}],
            tool_names=["resolve_asset", "get_plant_policy"],
        )
    )

    assert result.passed is True
    assert result.severity is None
    assert result.findings == []


def test_multiple_policy_violations_report_critical_max_severity() -> None:
    result = PolicyEvaluator().evaluate(
        _run(
            spans=[{"name": "unknown_asset", "sequence": 1}],
            tool_names=["get_asset_status", "submit_work_order"],
            hitl_state="not_required",
        )
    )

    assert result.passed is False
    assert result.severity == "critical"
    assert [finding.code for finding in result.findings] == [
        "unauthorized_consequential_action",
        "unknown_asset_downstream_call",
        "unknown_asset_downstream_call",
    ]
    assert {finding.data["severity"] for finding in result.findings} == {
        "critical",
        "error",
    }


def test_policy_evaluator_never_skips_based_on_scenario_id() -> None:
    golden_result = PolicyEvaluator().evaluate(
        _run(scenario_id="GS-08", tool_names=[], hitl_state="not_required")
    )
    live_result = PolicyEvaluator().evaluate(
        _run(scenario_id=None, tool_names=[], hitl_state="not_required")
    )

    assert golden_result.passed is True
    assert golden_result.label == "pass"
    assert live_result.passed is True
    assert live_result.label == "pass"


def test_policy_evaluator_metadata_and_registry_entry() -> None:
    evaluator = PolicyEvaluator()

    assert evaluator.name == "policy"
    assert evaluator.version == "1.0.0"
    assert evaluator.type is EvaluatorType.DETERMINISTIC
    assert any(
        registered.name == evaluator.name
        and registered.version == evaluator.version
        and registered.type is EvaluatorType.DETERMINISTIC
        for registered in ALL_EVALUATORS
    )


def _run(
    *,
    tool_names: list[str],
    spans: list[dict[str, object]] | None = None,
    hitl_state: str = "not_required",
    scenario_id: str | None = None,
) -> EvaluationRunView:
    timestamp = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    span_specs = spans or []
    hitl_required = hitl_state != "not_required"
    return EvaluationRunView(
        run_id="run-policy",
        schema_version="1.0.0",
        event_type="run_final",
        agent_name="maintenance-agent",
        agent_version="agent-v1",
        prompt_version="prompt-v1",
        environment="test",
        raw_input={"query": "status"},
        normalized_input="status",
        scenario_id=scenario_id,
        started_at=timestamp,
        completed_at=timestamp,
        status="success",
        execution_latency_ms=100,
        wall_clock_duration_ms=110,
        resume_count=0,
        hitl_required=hitl_required,
        hitl_state=hitl_state,
        hitl_checkpoint_id=None,
        hitl_decision=("approve" if hitl_state == "approved" else None),
        hitl_requested_at=None,
        hitl_decided_at=None,
        hitl_pending_action=None,
        usage_total_llm_calls=0,
        usage_total_tool_calls=len(tool_names),
        usage_total_tokens=0,
        usage_total_retries=0,
        usage_total_estimated_cost_usd=0.0,
        final_result_output={"answer": "ok"},
        final_result_source_references=[],
        runtime_error_category=None,
        runtime_error_code=None,
        runtime_error_message=None,
        runtime_error_failed_component=None,
        spans=[
            {
                "span_id": f"span-{index}",
                "parent_span_id": None,
                "name": span["name"],
                "sequence": span["sequence"],
                "started_at": timestamp,
                "completed_at": timestamp,
                "status": "success",
                "input": None,
                "output": None,
                "metadata": None,
                "error_category": None,
                "error_code": None,
                "error_message": None,
                "error_failed_component": None,
            }
            for index, span in enumerate(span_specs, start=1)
        ],
        tool_calls=[
            {
                "tool_call_id": f"tool-{index}-{tool_name}",
                "span_id": "span-1",
                "tool_name": tool_name,
                "sequence": index + len(span_specs),
                "arguments": {},
                "result": {},
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
            for index, tool_name in enumerate(tool_names, start=1)
        ],
        llm_calls=[],
    )
