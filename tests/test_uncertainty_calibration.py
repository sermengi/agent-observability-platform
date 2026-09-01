import ast
from collections.abc import Iterable
from inspect import getsource
from pathlib import Path

from obs_platform.evaluation.judges import calibration
from obs_platform.evaluation.judges.calibration import (
    UNCERTAINTY_CASE_MANIFEST,
    CalibrationLabel,
    UncertaintyCalibrationCase,
    UncertaintyCalibrationNotFoundError,
    load_all_uncertainty_cases,
    load_uncertainty_case,
)


def test_uncertainty_calibration_manifest_contains_exact_phase_6_cases() -> None:
    cases = load_all_uncertainty_cases()

    assert set(cases) == set(UNCERTAINTY_CASE_MANIFEST)
    assert len(cases) == 6
    assert _label_count(cases.values(), CalibrationLabel.OVERCONFIDENT) == 3
    assert _label_count(cases.values(), CalibrationLabel.APPROPRIATELY_HEDGED) == 3
    assert all(isinstance(case, UncertaintyCalibrationCase) for case in cases.values())


def test_uncertainty_calibration_rejects_unknown_case() -> None:
    try:
        load_uncertainty_case("nonexistent")
    except UncertaintyCalibrationNotFoundError as exc:
        assert exc.args == ("nonexistent",)
    else:
        raise AssertionError("expected UncertaintyCalibrationNotFoundError")


def test_uncertainty_case_pairs_share_evidence_with_different_certainty() -> None:
    overconfident = load_uncertainty_case("overconfident_impeller_damage")
    hedged = load_uncertainty_case("hedged_impeller_damage")

    assert overconfident.evidence_texts == hedged.evidence_texts
    assert "is damaged" in overconfident.answer_text
    assert "may be damaged" in hedged.answer_text


def test_uncertainty_calibration_notebook_exists_for_manual_review() -> None:
    notebook_path = Path("notebooks/phase_6_uncertainty_calibration.ipynb")

    assert notebook_path.is_file()
    assert "UncertaintyJudge" in notebook_path.read_text()


def test_uncertainty_calibration_has_no_pytest_expectation_assertions() -> None:
    calibration_source = getsource(calibration)

    assert UNCERTAINTY_CASE_MANIFEST
    for path in Path("tests").rglob("test_*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert not (
            "UncertaintyJudge" in imported_names
            and (
                "load_all_uncertainty_cases" in imported_names
                or "load_uncertainty_case" in imported_names
            )
        )
    assert "UncertaintyJudge" not in calibration_source


def _label_count(
    cases: Iterable[UncertaintyCalibrationCase],
    expected_label: CalibrationLabel,
) -> int:
    return sum(bool(case.expected_label is expected_label) for case in cases)
