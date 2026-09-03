from collections.abc import Generator
from datetime import UTC, datetime

import pytest

from obs_platform.evaluation.contracts import SCENARIO_CONTRACTS, ScenarioContract
from obs_platform.evaluation.evaluators import EvidenceEvaluator
from obs_platform.evaluation.registry import ALL_EVALUATORS
from obs_platform.evaluation.types import EvaluationRunView, EvaluatorType


@pytest.fixture(autouse=True)
def restore_scenario_contracts() -> Generator[None]:
    original_contracts = dict(SCENARIO_CONTRACTS)
    try:
        yield
    finally:
        SCENARIO_CONTRACTS.clear()
        SCENARIO_CONTRACTS.update(original_contracts)


def test_all_required_evidence_present_passes_with_full_score() -> None:
    SCENARIO_CONTRACTS["GS-TEST"] = ScenarioContract(
        scenario_id="GS-TEST",
        required_evidence=["doc-1", "policy-1"],
    )

    result = EvidenceEvaluator().evaluate(
        _run(
            scenario_id="GS-TEST",
            final_result_source_references=["doc-1", "policy-1"],
        )
    )

    assert result.passed is True
    assert result.score == 1.0
    assert result.label == "pass"
    assert result.severity is None
    assert result.findings == []


def test_missing_required_evidence_fails_with_partial_score() -> None:
    SCENARIO_CONTRACTS["GS-TEST"] = ScenarioContract(
        scenario_id="GS-TEST",
        required_evidence=["doc-1", "policy-1"],
    )

    result = EvidenceEvaluator().evaluate(
        _run(scenario_id="GS-TEST", final_result_source_references=["doc-1"])
    )

    assert result.passed is False
    assert result.score == 0.5
    assert result.label == "fail"
    assert result.severity is None
    assert result.reason == "1/2 required evidence references found"
    assert len(result.findings) == 1
    assert result.findings[0].code == "missing_required_evidence"
    assert result.findings[0].data == {"evidence_id": "policy-1"}


def test_non_golden_run_is_not_applicable() -> None:
    result = EvidenceEvaluator().evaluate(
        _run(scenario_id="GS-UNKNOWN", final_result_source_references=["doc-1"])
    )

    assert result.passed is True
    assert result.score is None
    assert result.label == "not_applicable"
    assert result.severity is None
    assert result.findings == []


def test_evidence_evaluator_only_checks_final_result_source_references() -> None:
    SCENARIO_CONTRACTS["GS-TEST"] = ScenarioContract(
        scenario_id="GS-TEST",
        required_evidence=["doc-1"],
    )

    result = EvidenceEvaluator().evaluate(
        _run(
            scenario_id="GS-TEST",
            final_result_source_references=[],
            tool_result={"contains": "doc-1"},
            llm_output_payload={"contains": "doc-1"},
        )
    )

    assert result.passed is False
    assert result.findings[0].code == "missing_required_evidence"


def test_evidence_evaluator_metadata_and_registry_entry() -> None:
    evaluator = EvidenceEvaluator()

    assert evaluator.name == "evidence"
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
    scenario_id: str | None,
    final_result_source_references: list[str],
    tool_result: dict[str, object] | None = None,
    llm_output_payload: dict[str, object] | None = None,
) -> EvaluationRunView:
    timestamp = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    return EvaluationRunView(
        run_id="run-evidence",
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
        usage_total_llm_calls=1,
        usage_total_tool_calls=1,
        usage_total_tokens=10,
        usage_total_retries=0,
        usage_total_estimated_cost_usd=0.01,
        final_result_output={"answer": "ok"},
        final_result_source_references=final_result_source_references,
        runtime_error_category=None,
        runtime_error_code=None,
        runtime_error_message=None,
        runtime_error_failed_component=None,
        spans=[],
        tool_calls=[
            {
                "tool_call_id": "tool-1",
                "span_id": "span-1",
                "tool_name": "get_asset_status",
                "sequence": 1,
                "arguments": {},
                "result": tool_result or {},
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
                "sequence": 1,
                "call_type": "synthesis",
                "model": "test-model",
                "provider": "test-provider",
                "started_at": timestamp,
                "completed_at": timestamp,
                "latency_ms": 20,
                "prompt_tokens": 5,
                "completion_tokens": 5,
                "total_tokens": 10,
                "estimated_cost_usd": 0.01,
                "input_payload": {},
                "output_payload": llm_output_payload or {},
                "status": "success",
                "error_category": None,
                "error_code": None,
                "error_message": None,
                "error_failed_component": None,
            }
        ],
    )
