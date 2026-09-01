import json
from enum import StrEnum
from importlib.resources import files
from typing import Any, cast

from pydantic import BaseModel, ConfigDict


class CalibrationLabel(StrEnum):
    GROUNDED = "grounded"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"


class GroundednessCalibrationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    evidence_texts: list[str]
    answer_text: str
    expected_label: CalibrationLabel
    notes: str


GROUNDEDNESS_CASE_MANIFEST: tuple[str, ...] = (
    "grounded_asset_status",
    "grounded_policy_limit",
    "grounded_maintenance_history",
    "unsupported_issue_003_seal_replacement",
    "unsupported_unseen_temperature",
    "unsupported_created_work_order",
    "ambiguous_fault_code_inference",
    "ambiguous_trend_interpretation",
    "ambiguous_policy_recommendation",
)


class GroundednessCalibrationNotFoundError(KeyError):
    pass


def load_groundedness_case(case_id: str) -> GroundednessCalibrationCase:
    cases = load_all_groundedness_cases()
    try:
        return cases[case_id]
    except KeyError as exc:
        raise GroundednessCalibrationNotFoundError(case_id) from exc


def load_all_groundedness_cases() -> dict[str, GroundednessCalibrationCase]:
    payload = _load_groundedness_payload()
    raw_cases = cast(list[dict[str, Any]], payload["cases"])
    cases = {
        raw_case["case_id"]: GroundednessCalibrationCase.model_validate(raw_case)
        for raw_case in raw_cases
    }
    manifest_ids = set(GROUNDEDNESS_CASE_MANIFEST)
    case_ids = set(cases)
    if case_ids != manifest_ids:
        missing = sorted(manifest_ids - case_ids)
        unexpected = sorted(case_ids - manifest_ids)
        raise ValueError(
            "groundedness calibration cases do not match manifest: "
            f"missing={missing}, unexpected={unexpected}"
        )
    return {case_id: cases[case_id] for case_id in GROUNDEDNESS_CASE_MANIFEST}


def _load_groundedness_payload() -> dict[str, Any]:
    fixture_path = files("obs_platform.evaluation.judges.calibration").joinpath(
        "groundedness_cases.json"
    )
    return cast(dict[str, Any], json.loads(fixture_path.read_text()))
