import ast
from collections.abc import Iterable
from inspect import getsource
from pathlib import Path

from obs_platform.evaluation.judges import calibration
from obs_platform.evaluation.judges.calibration import (
    GROUNDEDNESS_CASE_MANIFEST,
    CalibrationLabel,
    GroundednessCalibrationCase,
    GroundednessCalibrationNotFoundError,
    load_all_groundedness_cases,
    load_groundedness_case,
)
from obs_platform.telemetry.v1 import load_fixture


def test_groundedness_calibration_manifest_contains_exact_phase_6_cases() -> None:
    cases = load_all_groundedness_cases()

    assert set(cases) == set(GROUNDEDNESS_CASE_MANIFEST)
    assert len(cases) == 9
    assert _label_count(cases.values(), CalibrationLabel.GROUNDED) == 3
    assert _label_count(cases.values(), CalibrationLabel.UNSUPPORTED) == 3
    assert _label_count(cases.values(), CalibrationLabel.AMBIGUOUS) == 3
    assert all(isinstance(case, GroundednessCalibrationCase) for case in cases.values())


def test_groundedness_calibration_rejects_unknown_case() -> None:
    try:
        load_groundedness_case("nonexistent")
    except GroundednessCalibrationNotFoundError as exc:
        assert exc.args == ("nonexistent",)
    else:
        raise AssertionError("expected GroundednessCalibrationNotFoundError")


def test_groundedness_calibration_includes_issue_003_seal_replacement_case() -> None:
    candidate = load_fixture("unsupported_claim_candidate")
    case = load_groundedness_case("unsupported_issue_003_seal_replacement")

    assert case.expected_label is CalibrationLabel.UNSUPPORTED
    assert "seal was replaced yesterday" in case.answer_text.lower()
    assert candidate.final_result is not None
    assert case.answer_text == str(candidate.final_result.output["summary"])
    assert case.evidence_texts == [
        f"{tool_call.tool_name}: {tool_call.result}"
        for tool_call in candidate.tool_calls
    ]


def test_groundedness_calibration_notebook_exists_for_manual_review() -> None:
    notebook_path = Path("notebooks/phase_6_groundedness_calibration.ipynb")

    assert notebook_path.is_file()
    assert "GroundednessJudge" in notebook_path.read_text()


def test_groundedness_calibration_has_no_pytest_expectation_assertions() -> None:
    calibration_source = getsource(calibration)

    assert GROUNDEDNESS_CASE_MANIFEST
    for path in Path("tests").rglob("test_*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert not (
            "GroundednessJudge" in imported_names
            and (
                "load_all_groundedness_cases" in imported_names
                or "load_groundedness_case" in imported_names
            )
        )
    assert "GroundednessJudge" not in calibration_source


def _label_count(
    cases: Iterable[GroundednessCalibrationCase],
    expected_label: CalibrationLabel,
) -> int:
    return sum(bool(case.expected_label is expected_label) for case in cases)
