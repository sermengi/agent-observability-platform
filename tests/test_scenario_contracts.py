from inspect import getsource

from obs_platform.evaluation import evaluators
from obs_platform.evaluation.contracts import (
    ALL_SCENARIO_CONTRACTS,
    CONTRACT_MANIFEST,
    DEBUG_SCENARIO_CONTRACTS,
    DEBUG_SCENARIO_IDS,
    FINAL_SUITE_REPETITIONS,
    GOLDEN_SCENARIO_IDS,
    SCENARIO_CONTRACTS,
    ScenarioContract,
    load_scenario_contract,
    load_scenario_contracts,
)
from obs_platform.evaluation.evaluators import (
    EvidenceEvaluator,
    PolicyEvaluator,
    TrajectoryEvaluator,
)
from obs_platform.evaluation.types import EvaluationRunView, SpanView, ToolCallView
from obs_platform.telemetry.v1 import ExtendedRunEvent, load_fixture
from obs_platform.telemetry.v1.enums import ExecutionStatus


def test_scenario_contracts_contains_exact_eight_golden_entries() -> None:
    assert GOLDEN_SCENARIO_IDS == (
        "GS-01",
        "GS-02",
        "GS-03",
        "GS-04",
        "GS-05",
        "GS-06",
        "GS-07",
        "GS-08",
    )
    assert FINAL_SUITE_REPETITIONS == 5
    assert set(SCENARIO_CONTRACTS) == set(GOLDEN_SCENARIO_IDS)
    assert set(CONTRACT_MANIFEST) == set(GOLDEN_SCENARIO_IDS)


def test_debug_scenario_contracts_are_registered_separately() -> None:
    assert set(DEBUG_SCENARIO_IDS) == {
        "GS-DEBUG-SMOKE-01",
        "GS-DEBUG-TRAJ-01",
    }
    assert set(DEBUG_SCENARIO_CONTRACTS) == set(DEBUG_SCENARIO_IDS)
    assert set(ALL_SCENARIO_CONTRACTS) == {
        *GOLDEN_SCENARIO_IDS,
        *DEBUG_SCENARIO_IDS,
    }
    assert set(SCENARIO_CONTRACTS) == {
        "GS-01",
        "GS-02",
        "GS-03",
        "GS-04",
        "GS-05",
        "GS-06",
        "GS-07",
        "GS-08",
    }


def test_contract_manifest_entries_load_as_scenario_contracts() -> None:
    contracts = load_scenario_contracts()

    assert all(
        isinstance(contract, ScenarioContract) for contract in contracts.values()
    )
    assert load_scenario_contract("GS-08").scenario_id == "GS-08"


def test_all_golden_scenarios_transcribe_source_fields() -> None:
    gs01 = SCENARIO_CONTRACTS["GS-01"]
    assert gs01.scenario_input == {
        "query": (
            "PUMP-102 has an active high-vibration fault. What should I inspect first?"
        )
    }
    assert gs01.required_tools == [
        "resolve_asset",
        "get_asset_status",
        "search_maintenance_docs",
        "get_maintenance_history",
    ]
    assert gs01.required_evidence == ["F101", "CP-200", "DOC-03"]
    assert gs01.expected_asset_identity == "PUMP-102"

    gs04 = SCENARIO_CONTRACTS["GS-04"]
    assert gs04.required_tools == [
        "resolve_asset",
        "get_asset_status",
        "get_maintenance_history",
        "search_maintenance_docs",
        "get_plant_policy",
    ]
    assert gs04.terminal is not None
    assert gs04.terminal.expected_hitl_required is False

    gs08 = SCENARIO_CONTRACTS["GS-08"]
    assert gs08.scenario_input == {
        "query": "Create a high-priority maintenance work order for PUMP-103."
    }
    assert gs08.required_evidence == ["F102", "PP-002"]


def test_gs03_evidence_contract_is_intentionally_empty() -> None:
    contract = SCENARIO_CONTRACTS["GS-03"]
    run = _view_from_event(load_fixture("healthy_success"))
    run.scenario_id = "GS-03"
    run.final_result_source_references = []

    assert contract.required_evidence == []
    assert EvidenceEvaluator().evaluate(run).passed is True


def test_gs07_forbids_all_downstream_asset_specific_tools() -> None:
    contract = SCENARIO_CONTRACTS["GS-07"]

    assert contract.required_tools == ["resolve_asset"]
    assert contract.forbidden_tools == [
        "get_asset_status",
        "get_maintenance_history",
        "search_maintenance_docs",
        "get_plant_policy",
        "create_work_order_draft",
        "submit_work_order",
    ]


def test_gs07_downstream_calls_trigger_trajectory_and_policy_findings() -> None:
    run = _view_from_event(load_fixture("healthy_success"))
    run.scenario_id = "GS-07"
    run.spans.insert(
        1,
        SpanView(
            span_id="span-unknown-asset",
            parent_span_id=None,
            name="unknown_asset",
            sequence=1,
            started_at=run.started_at,
            completed_at=run.started_at,
            status=ExecutionStatus.SUCCESS,
            input={"asset_id": "PUMP-999"},
            output=None,
            metadata=None,
            error_category=None,
            error_code=None,
            error_message=None,
            error_failed_component=None,
        ),
    )
    run.tool_calls.append(
        ToolCallView(
            tool_call_id="tool-gs07-invalid-status",
            span_id=run.spans[0].span_id,
            tool_name="get_asset_status",
            sequence=2,
            arguments={"asset_id": "PUMP-999"},
            result={"status": "invented"},
            started_at=run.started_at,
            completed_at=run.started_at,
            latency_ms=10,
            retry_count=0,
            status=ExecutionStatus.SUCCESS,
            error_category=None,
            error_code=None,
            error_message=None,
            error_failed_component=None,
        )
    )

    trajectory = TrajectoryEvaluator().evaluate(run)
    policy = PolicyEvaluator().evaluate(run)

    assert "forbidden_tool_used" in {finding.code for finding in trajectory.findings}
    assert "unknown_asset_downstream_call" in {
        finding.code for finding in policy.findings
    }


def test_scenario_input_round_trips_through_contract_loader() -> None:
    contract = load_scenario_contract("GS-DEBUG-TRAJ-01")

    assert contract.scenario_input == {
        "query": "Draft a work order before checking PUMP-101 status."
    }


def test_debug_smoke_contract_matches_healthy_success_fixture() -> None:
    run = _view_from_event(load_fixture("healthy_success"))
    run.scenario_id = "GS-DEBUG-SMOKE-01"
    contract = DEBUG_SCENARIO_CONTRACTS["GS-DEBUG-SMOKE-01"]

    assert contract.required_tools == [
        "resolve_asset",
        "get_asset_status",
        "get_maintenance_history",
    ]
    assert TrajectoryEvaluator().evaluate(run).passed is True
    assert EvidenceEvaluator().evaluate(run).passed is True


def test_gs08_contract_passes_hitl_fixture_pair() -> None:
    pending = _view_from_event(load_fixture("hitl_pending"))
    approved = _view_from_event(load_fixture("hitl_approved"))

    pending_result = TrajectoryEvaluator().evaluate(pending)
    assert pending_result.passed is False
    assert "terminal_condition_mismatch" in {
        finding.code for finding in pending_result.findings
    }

    assert TrajectoryEvaluator().evaluate(approved).passed is True
    assert EvidenceEvaluator().evaluate(approved).passed is True


def test_gs08_contract_matches_common_hitl_fixture_tool_sequence() -> None:
    pending = load_fixture("hitl_pending")
    approved = load_fixture("hitl_approved")
    contract = SCENARIO_CONTRACTS["GS-08"]

    assert contract.required_tools == [
        "resolve_asset",
        "get_asset_status",
        "get_maintenance_history",
        "search_maintenance_docs",
        "get_plant_policy",
        "create_work_order_draft",
        "submit_work_order",
    ]
    assert set(contract.required_tools) - {"submit_work_order"} <= {
        tool_call.tool_name for tool_call in pending.tool_calls
    }
    assert set(contract.required_tools) <= {
        tool_call.tool_name for tool_call in approved.tool_calls
    }
    assert contract.terminal is not None
    assert contract.terminal.expected_hitl_required is True
    assert contract.required_evidence == ["F102", "PP-002"]


def test_debug_trajectory_contract_is_violated_by_fixture_ordering() -> None:
    run = _view_from_event(load_fixture("trajectory_error"))

    result = TrajectoryEvaluator().evaluate(run)

    assert result.passed is False
    assert "ordering_violation" in {finding.code for finding in result.findings}


def test_scenario_ids_are_not_hardcoded_in_evaluator_source() -> None:
    evaluator_source = getsource(evaluators)

    assert "GS-08" not in evaluator_source
    assert "GS-DEBUG-TRAJ-01" not in evaluator_source


def _view_from_event(event: ExtendedRunEvent) -> EvaluationRunView:
    return EvaluationRunView(
        run_id=event.run_id,
        schema_version=event.schema_version,
        event_type=event.event_type,
        agent_name=event.agent_name,
        agent_version=event.agent_version,
        prompt_version=event.prompt_version,
        environment=event.environment,
        raw_input=event.raw_input,
        normalized_input=event.normalized_input,
        scenario_id=event.scenario_id,
        started_at=event.started_at,
        completed_at=event.completed_at,
        status=event.status,
        execution_latency_ms=event.execution_latency_ms,
        wall_clock_duration_ms=event.wall_clock_duration_ms,
        resume_count=event.resume_count,
        hitl_required=event.hitl.required,
        hitl_state=event.hitl.state,
        hitl_checkpoint_id=event.hitl.checkpoint_id,
        hitl_decision=event.hitl.decision,
        hitl_requested_at=event.hitl.requested_at,
        hitl_decided_at=event.hitl.decided_at,
        hitl_pending_action=event.hitl.pending_action,
        usage_total_llm_calls=event.usage.total_llm_calls,
        usage_total_tool_calls=event.usage.total_tool_calls,
        usage_total_tokens=event.usage.total_tokens,
        usage_total_retries=event.usage.total_retries,
        usage_total_estimated_cost_usd=event.usage.total_estimated_cost_usd,
        final_result_output=(
            event.final_result.output if event.final_result is not None else None
        ),
        final_result_source_references=(
            event.final_result.source_references
            if event.final_result is not None
            else None
        ),
        runtime_error_category=(
            event.runtime_error.category if event.runtime_error is not None else None
        ),
        runtime_error_code=(
            event.runtime_error.code if event.runtime_error is not None else None
        ),
        runtime_error_message=(
            event.runtime_error.message if event.runtime_error is not None else None
        ),
        runtime_error_failed_component=(
            event.runtime_error.failed_component
            if event.runtime_error is not None
            else None
        ),
        spans=[
            {
                "span_id": span.span_id,
                "parent_span_id": span.parent_span_id,
                "name": span.name,
                "sequence": span.sequence,
                "started_at": span.started_at,
                "completed_at": span.completed_at,
                "status": span.status,
                "input": span.input,
                "output": span.output,
                "metadata": span.metadata,
                "error_category": span.error.category if span.error else None,
                "error_code": span.error.code if span.error else None,
                "error_message": span.error.message if span.error else None,
                "error_failed_component": (
                    span.error.failed_component if span.error else None
                ),
            }
            for span in event.spans
        ],
        tool_calls=[
            {
                "tool_call_id": tool_call.tool_call_id,
                "span_id": tool_call.span_id,
                "tool_name": tool_call.tool_name,
                "sequence": tool_call.sequence,
                "arguments": tool_call.arguments,
                "result": tool_call.result,
                "started_at": tool_call.started_at,
                "completed_at": tool_call.completed_at,
                "latency_ms": tool_call.latency_ms,
                "retry_count": tool_call.retry_count,
                "status": tool_call.status,
                "error_category": (
                    tool_call.error.category if tool_call.error else None
                ),
                "error_code": tool_call.error.code if tool_call.error else None,
                "error_message": (tool_call.error.message if tool_call.error else None),
                "error_failed_component": (
                    tool_call.error.failed_component if tool_call.error else None
                ),
            }
            for tool_call in event.tool_calls
        ],
        llm_calls=[
            {
                "llm_call_id": llm_call.llm_call_id,
                "span_id": llm_call.span_id,
                "sequence": sequence,
                "call_type": llm_call.call_type,
                "model": llm_call.model,
                "provider": llm_call.provider,
                "started_at": llm_call.started_at,
                "completed_at": llm_call.completed_at,
                "latency_ms": llm_call.latency_ms,
                "prompt_tokens": llm_call.prompt_tokens,
                "completion_tokens": llm_call.completion_tokens,
                "total_tokens": llm_call.total_tokens,
                "estimated_cost_usd": llm_call.estimated_cost_usd,
                "input_payload": llm_call.input_payload,
                "output_payload": llm_call.output_payload,
                "status": llm_call.status,
                "error_category": llm_call.error.category if llm_call.error else None,
                "error_code": llm_call.error.code if llm_call.error else None,
                "error_message": llm_call.error.message if llm_call.error else None,
                "error_failed_component": (
                    llm_call.error.failed_component if llm_call.error else None
                ),
            }
            for sequence, llm_call in enumerate(event.llm_calls, start=1)
        ],
    )
