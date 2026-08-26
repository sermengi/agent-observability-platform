import json
from pathlib import Path
from typing import Any, cast

from obs_platform.telemetry.v1 import ExtendedRunEvent, LLMCallType, RunStatus

FIXTURE_DIR = Path("src/obs_platform/telemetry/v1/fixtures")

FIXTURE_NAMES = {
    "healthy_success",
    "tool_failure",
    "trajectory_error",
    "retrieval_failure",
    "unsupported_claim_candidate",
    "policy_violation",
    "hitl_pending",
    "hitl_approved",
}


def read_fixture_payload(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((FIXTURE_DIR / f"{name}.json").read_text()))


def validate_fixture(name: str) -> ExtendedRunEvent:
    return ExtendedRunEvent.model_validate(read_fixture_payload(name))


def child_ids(event: ExtendedRunEvent) -> dict[str, set[str]]:
    return {
        "spans": {span.span_id for span in event.spans},
        "tool_calls": {tool_call.tool_call_id for tool_call in event.tool_calls},
        "llm_calls": {llm_call.llm_call_id for llm_call in event.llm_calls},
    }


def test_all_canonical_fixture_files_exist() -> None:
    fixture_files = {path.stem for path in FIXTURE_DIR.glob("*.json")}

    assert fixture_files == FIXTURE_NAMES


def test_all_fixtures_parse_as_json_and_validate() -> None:
    for name in FIXTURE_NAMES:
        assert validate_fixture(name).run_id


def test_tool_failure_is_only_tool_error_fixture() -> None:
    statuses = {name: validate_fixture(name).status for name in FIXTURE_NAMES}

    assert statuses["tool_failure"] is RunStatus.TOOL_ERROR
    assert [
        name for name, status in statuses.items() if status is RunStatus.TOOL_ERROR
    ] == ["tool_failure"]


def test_content_level_failure_candidates_are_runtime_successes() -> None:
    for name in {
        "trajectory_error",
        "retrieval_failure",
        "unsupported_claim_candidate",
        "policy_violation",
    }:
        assert validate_fixture(name).status is RunStatus.SUCCESS


def test_hitl_fixture_pair_shares_run_id_and_carried_child_ids() -> None:
    pending = validate_fixture("hitl_pending")
    approved = validate_fixture("hitl_approved")

    assert pending.run_id == approved.run_id == "run-gs08-001"

    pending_ids = child_ids(pending)
    approved_ids = child_ids(approved)
    for child_type, ids in pending_ids.items():
        assert ids <= approved_ids[child_type]


def test_hitl_approved_adds_submit_work_order_tool_call() -> None:
    pending = validate_fixture("hitl_pending")
    approved = validate_fixture("hitl_approved")

    new_tool_call_ids = (
        child_ids(approved)["tool_calls"] - child_ids(pending)["tool_calls"]
    )

    assert new_tool_call_ids
    assert any(
        tool_call.tool_name == "submit_work_order"
        and tool_call.tool_call_id in new_tool_call_ids
        for tool_call in approved.tool_calls
    )


def test_fixture_readme_documents_each_scenario() -> None:
    readme = (FIXTURE_DIR / "README.md").read_text()

    for name in FIXTURE_NAMES:
        assert name in readme
    assert "intentionally adversarial" in readme


def test_fixture_llm_calls_have_realistic_mock_usage() -> None:
    for name in FIXTURE_NAMES:
        for llm_call in validate_fixture(name).llm_calls:
            assert llm_call.model == "claude-sonnet-4-6"
            assert llm_call.provider == "anthropic"
            assert llm_call.prompt_tokens is not None
            assert llm_call.completion_tokens is not None
            assert llm_call.total_tokens is not None
            assert llm_call.latency_ms is not None
            assert llm_call.estimated_cost_usd is not None


def test_fixture_llm_costs_match_mock_formula() -> None:
    for name in FIXTURE_NAMES:
        for llm_call in validate_fixture(name).llm_calls:
            assert llm_call.prompt_tokens is not None
            assert llm_call.completion_tokens is not None
            assert llm_call.estimated_cost_usd is not None

            expected_cost = round(
                llm_call.prompt_tokens * 0.000003
                + llm_call.completion_tokens * 0.000015,
                6,
            )

            assert llm_call.estimated_cost_usd == expected_cost


def test_fixture_llm_call_types_are_producer_call_types() -> None:
    allowed_call_types = {
        LLMCallType.INTERPRETATION,
        LLMCallType.EVIDENCE_GATHERING,
        LLMCallType.SYNTHESIS,
    }

    for name in FIXTURE_NAMES:
        for llm_call in validate_fixture(name).llm_calls:
            assert llm_call.call_type in allowed_call_types


def test_tool_failure_fixture_never_reaches_synthesis() -> None:
    event = validate_fixture("tool_failure")

    assert all(
        llm_call.call_type is not LLMCallType.SYNTHESIS for llm_call in event.llm_calls
    )
