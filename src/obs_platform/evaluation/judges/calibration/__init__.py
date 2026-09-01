import json
from enum import StrEnum
from importlib.resources import files
from typing import Any, cast

from pydantic import BaseModel, ConfigDict


class CalibrationLabel(StrEnum):
    GROUNDED = "grounded"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"
    OVERCONFIDENT = "overconfident"
    APPROPRIATELY_HEDGED = "appropriately_hedged"


class GroundednessCalibrationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    evidence_texts: list[str]
    answer_text: str
    expected_label: CalibrationLabel
    notes: str


class UncertaintyCalibrationCase(BaseModel):
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

UNCERTAINTY_CASE_MANIFEST: tuple[str, ...] = (
    "overconfident_impeller_damage",
    "overconfident_bearing_failure",
    "overconfident_sensor_cause",
    "hedged_impeller_damage",
    "hedged_bearing_failure",
    "hedged_sensor_cause",
)


class GroundednessCalibrationNotFoundError(KeyError):
    pass


class UncertaintyCalibrationNotFoundError(KeyError):
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


def load_uncertainty_case(case_id: str) -> UncertaintyCalibrationCase:
    cases = load_all_uncertainty_cases()
    try:
        return cases[case_id]
    except KeyError as exc:
        raise UncertaintyCalibrationNotFoundError(case_id) from exc


def load_all_uncertainty_cases() -> dict[str, UncertaintyCalibrationCase]:
    payload = _load_json_resource("uncertainty_cases.json")
    raw_cases = cast(list[dict[str, Any]], payload["cases"])
    cases = {
        raw_case["case_id"]: UncertaintyCalibrationCase.model_validate(raw_case)
        for raw_case in raw_cases
    }
    manifest_ids = set(UNCERTAINTY_CASE_MANIFEST)
    case_ids = set(cases)
    if case_ids != manifest_ids:
        missing = sorted(manifest_ids - case_ids)
        unexpected = sorted(case_ids - manifest_ids)
        raise ValueError(
            "uncertainty calibration cases do not match manifest: "
            f"missing={missing}, unexpected={unexpected}"
        )
    return {case_id: cases[case_id] for case_id in UNCERTAINTY_CASE_MANIFEST}


def _load_groundedness_payload() -> dict[str, Any]:
    return _load_json_resource("groundedness_cases.json")


def _load_json_resource(filename: str) -> dict[str, Any]:
    fixture_path = files("obs_platform.evaluation.judges.calibration").joinpath(
        filename
    )
    return cast(dict[str, Any], json.loads(fixture_path.read_text()))
