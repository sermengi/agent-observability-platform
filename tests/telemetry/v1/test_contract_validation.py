import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from obs_platform.telemetry.v1 import ExtendedRunEvent

FIXTURE_DIR = Path("src/obs_platform/telemetry/v1/fixtures")


def healthy_success_payload() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((FIXTURE_DIR / "healthy_success.json").read_text()),
    )


def duplicate_first_tool_call(payload: dict[str, Any]) -> None:
    payload["tool_calls"].append(deepcopy(payload["tool_calls"][0]))


def remove_run_id(payload: dict[str, Any]) -> None:
    del payload["run_id"]


def set_invalid_status(payload: dict[str, Any]) -> None:
    payload["status"] = "not-a-run-status"


def dangle_parent_span_id(payload: dict[str, Any]) -> None:
    payload["spans"][0]["parent_span_id"] = "span-missing"


def make_lifecycle_inconsistent(payload: dict[str, Any]) -> None:
    payload["event_type"] = "run_awaiting_approval"
    payload["status"] = "awaiting_approval"
    payload["completed_at"] = payload["started_at"]
    payload["final_result"] = None
    payload["hitl"] = {
        "required": True,
        "state": "pending",
        "checkpoint_id": "checkpoint-invalid",
        "decision": None,
        "requested_at": payload["started_at"],
        "decided_at": None,
        "pending_action": {"draft_id": "draft-invalid"},
    }


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(remove_run_id, id="missing-required-field"),
        pytest.param(set_invalid_status, id="invalid-enum-value"),
        pytest.param(dangle_parent_span_id, id="dangling-parent-span-id"),
        pytest.param(duplicate_first_tool_call, id="duplicate-tool-call-id"),
        pytest.param(make_lifecycle_inconsistent, id="inconsistent-lifecycle"),
    ],
)
def test_invalid_contract_payloads_fail_validation(
    mutate: Any,
) -> None:
    payload = healthy_success_payload()
    mutate(payload)

    with pytest.raises(ValidationError):
        ExtendedRunEvent.model_validate(payload)
