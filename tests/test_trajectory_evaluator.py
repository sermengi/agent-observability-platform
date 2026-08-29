from collections.abc import Generator
from datetime import UTC, datetime

import pytest

from obs_platform.evaluation.contracts import (
    SCENARIO_CONTRACTS,
    ScenarioContract,
    TerminalCondition,
)
from obs_platform.evaluation.evaluators import TrajectoryEvaluator
from obs_platform.evaluation.registry import DETERMINISTIC_EVALUATORS
from obs_platform.evaluation.types import EvaluationRunView, EvaluatorType


@pytest.fixture(autouse=True)
def restore_scenario_contracts() -> Generator[None]:
    original_contracts = dict(SCENARIO_CONTRACTS)
    try:
        yield
    finally:
        SCENARIO_CONTRACTS.clear()
        SCENARIO_CONTRACTS.update(original_contracts)


def test_unknown_scenario_is_not_applicable() -> None:
    result = TrajectoryEvaluator().evaluate(
        _run(scenario_id="GS-UNKNOWN", tool_names=["resolve_asset"])
    )

    assert result.passed is True
    assert result.score is None
    assert result.label == "not_applicable"
    assert result.severity is None
    assert result.findings == []


def test_missing_required_tool_fails() -> None:
    SCENARIO_CONTRACTS["GS-TEST"] = ScenarioContract(
        scenario_id="GS-TEST",
        required_tools=["resolve_asset", "get_asset_status"],
    )

    result = TrajectoryEvaluator().evaluate(
        _run(scenario_id="GS-TEST", tool_names=["resolve_asset"])
    )

    assert result.passed is False
    assert result.score == 0.5
    assert result.label == "fail"
    assert result.severity is None
    assert len(result.findings) == 1
    assert result.findings[0].code == "missing_required_tool"
    assert result.findings[0].data == {"tool_name": "get_asset_status"}


def test_forbidden_tool_used_fails_even_when_tool_call_errored() -> None:
    SCENARIO_CONTRACTS["GS-TEST"] = ScenarioContract(
        scenario_id="GS-TEST",
        forbidden_tools=["submit_work_order"],
    )

    result = TrajectoryEvaluator().evaluate(
        _run(
            scenario_id="GS-TEST",
            tool_names=["resolve_asset", "submit_work_order"],
            tool_statuses={"submit_work_order": "error"},
        )
    )

    assert result.passed is False
    assert result.score == 0.0
    assert result.findings[0].code == "forbidden_tool_used"
    assert result.findings[0].data == {
        "tool_name": "submit_work_order",
        "tool_call_id": "tool-2-submit_work_order",
        "status": "error",
    }


def test_ordering_violation_fails_when_both_tools_are_present_out_of_order() -> None:
    SCENARIO_CONTRACTS["GS-TEST"] = ScenarioContract(
        scenario_id="GS-TEST",
        ordering_constraints=[("resolve_asset", "create_work_order_draft")],
    )

    result = TrajectoryEvaluator().evaluate(
        _run(
            scenario_id="GS-TEST",
            tool_names=["create_work_order_draft", "resolve_asset"],
        )
    )

    assert result.passed is False
    assert result.score == 0.0
    assert result.findings[0].code == "ordering_violation"
    assert result.findings[0].data == {
        "before_tool": "resolve_asset",
        "after_tool": "create_work_order_draft",
        "before_sequence": 2,
        "after_sequence": 1,
    }


def test_ordering_constraint_is_skipped_when_one_tool_is_absent() -> None:
    SCENARIO_CONTRACTS["GS-TEST"] = ScenarioContract(
        scenario_id="GS-TEST",
        ordering_constraints=[("resolve_asset", "create_work_order_draft")],
    )

    result = TrajectoryEvaluator().evaluate(
        _run(scenario_id="GS-TEST", tool_names=["resolve_asset"])
    )

    assert result.passed is True
    assert result.score is None
    assert result.label == "pass"
    assert result.findings == []


def test_terminal_condition_mismatch_fails() -> None:
    SCENARIO_CONTRACTS["GS-TEST"] = ScenarioContract(
        scenario_id="GS-TEST",
        terminal=TerminalCondition(
            expected_status="awaiting_approval",
            expected_event_type="run_awaiting_approval",
            expected_hitl_required=True,
            expected_hitl_state="pending",
        ),
    )

    result = TrajectoryEvaluator().evaluate(
        _run(scenario_id="GS-TEST", tool_names=["resolve_asset"])
    )

    assert result.passed is False
    assert result.score == 0.0
    assert result.findings[0].code == "terminal_condition_mismatch"
    assert result.findings[0].data == {
        "field": "status",
        "expected": "awaiting_approval",
        "actual": "success",
    }


def test_matching_contract_passes_and_scores_checked_constraints() -> None:
    SCENARIO_CONTRACTS["GS-TEST"] = ScenarioContract(
        scenario_id="GS-TEST",
        required_tools=["resolve_asset"],
        forbidden_tools=["submit_work_order"],
        ordering_constraints=[("resolve_asset", "create_work_order_draft")],
        terminal=TerminalCondition(expected_status="success"),
    )

    result = TrajectoryEvaluator().evaluate(
        _run(
            scenario_id="GS-TEST",
            tool_names=["resolve_asset", "create_work_order_draft"],
        )
    )

    assert result.passed is True
    assert result.score == 1.0
    assert result.label == "pass"
    assert result.severity is None
    assert result.findings == []


def test_trajectory_evaluator_metadata_and_registry_entry() -> None:
    evaluator = TrajectoryEvaluator()

    assert evaluator.name == "trajectory"
    assert evaluator.version == "1.0.0"
    assert evaluator.type is EvaluatorType.DETERMINISTIC
    assert any(
        registered.name == evaluator.name
        and registered.version == evaluator.version
        and registered.type is EvaluatorType.DETERMINISTIC
        for registered in DETERMINISTIC_EVALUATORS
    )


def _run(
    *,
    scenario_id: str | None,
    tool_names: list[str],
    tool_statuses: dict[str, str] | None = None,
) -> EvaluationRunView:
    timestamp = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    statuses = tool_statuses or {}
    return EvaluationRunView(
        run_id="run-trajectory",
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
        hitl_required=False,
        hitl_state="not_required",
        hitl_checkpoint_id=None,
        hitl_decision=None,
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
        spans=[],
        tool_calls=[
            {
                "tool_call_id": f"tool-{index}-{tool_name}",
                "span_id": "span-1",
                "tool_name": tool_name,
                "sequence": index,
                "arguments": {"opaque": tool_name},
                "result": {"opaque": tool_name},
                "started_at": timestamp,
                "completed_at": timestamp,
                "latency_ms": 20,
                "retry_count": 0,
                "status": statuses.get(tool_name, "success"),
                "error_category": None,
                "error_code": None,
                "error_message": None,
                "error_failed_component": None,
            }
            for index, tool_name in enumerate(tool_names, start=1)
        ],
        llm_calls=[],
    )
