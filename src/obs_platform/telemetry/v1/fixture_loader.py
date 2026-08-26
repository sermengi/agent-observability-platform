import json
from importlib.resources import files
from typing import Any, cast

from obs_platform.telemetry.v1.models import ExtendedRunEvent

FIXTURE_MANIFEST: dict[str, str] = {
    "healthy_success": "healthy_success.json",
    "tool_failure": "tool_failure.json",
    "trajectory_error": "trajectory_error.json",
    "retrieval_failure": "retrieval_failure.json",
    "unsupported_claim_candidate": "unsupported_claim_candidate.json",
    "policy_violation": "policy_violation.json",
    "hitl_pending": "hitl_pending.json",
    "hitl_approved": "hitl_approved.json",
}


class FixtureNotFoundError(KeyError):
    pass


def load_fixture(name: str) -> ExtendedRunEvent:
    try:
        filename = FIXTURE_MANIFEST[name]
    except KeyError as exc:
        raise FixtureNotFoundError(name) from exc

    fixture_path = files("obs_platform.telemetry.v1.fixtures").joinpath(filename)
    payload = cast(dict[str, Any], json.loads(fixture_path.read_text()))
    return ExtendedRunEvent.model_validate(payload)


def load_all_fixtures() -> dict[str, ExtendedRunEvent]:
    return {name: load_fixture(name) for name in FIXTURE_MANIFEST}
