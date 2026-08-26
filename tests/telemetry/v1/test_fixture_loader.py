from obs_platform.telemetry.v1 import (
    ExtendedRunEvent,
    FixtureNotFoundError,
    load_all_fixtures,
    load_fixture,
)
from obs_platform.telemetry.v1.fixture_loader import FIXTURE_MANIFEST


def child_ids(event: ExtendedRunEvent) -> dict[str, set[str]]:
    return {
        "spans": {span.span_id for span in event.spans},
        "tool_calls": {tool_call.tool_call_id for tool_call in event.tool_calls},
        "llm_calls": {llm_call.llm_call_id for llm_call in event.llm_calls},
    }


def test_load_fixture_rejects_unknown_name() -> None:
    try:
        load_fixture("nonexistent")
    except FixtureNotFoundError as exc:
        assert exc.args == ("nonexistent",)
    else:
        raise AssertionError("expected FixtureNotFoundError")


def test_load_fixture_returns_distinct_instances() -> None:
    first = load_fixture("healthy_success")
    first.raw_input = "mutated"

    second = load_fixture("healthy_success")

    assert second.raw_input != "mutated"


def test_manifest_entries_correspond_to_existing_files() -> None:
    for name, filename in FIXTURE_MANIFEST.items():
        assert filename.endswith(".json")
        assert load_fixture(name).run_id


def test_all_fixtures_validate() -> None:
    fixtures = load_all_fixtures()

    assert set(fixtures) == set(FIXTURE_MANIFEST)
    assert all(isinstance(event, ExtendedRunEvent) for event in fixtures.values())


def test_hitl_pending_and_approved_share_run_identity() -> None:
    pending = load_fixture("hitl_pending")
    approved = load_fixture("hitl_approved")

    assert pending.run_id == approved.run_id == "run-gs08-001"

    pending_ids = child_ids(pending)
    approved_ids = child_ids(approved)
    for child_type, ids in pending_ids.items():
        assert ids <= approved_ids[child_type]
        assert approved_ids[child_type] - ids


def test_child_ids_unique_per_run() -> None:
    for event in load_all_fixtures().values():
        ids = child_ids(event)
        assert sum(len(values) for values in ids.values()) == len(
            set().union(*ids.values())
        )
