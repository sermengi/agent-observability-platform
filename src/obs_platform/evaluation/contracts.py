import json
from importlib.resources import files
from typing import Any, cast

from pydantic import Field

from obs_platform.evaluation.types import EvaluationModel
from obs_platform.telemetry.v1.enums import HITLState, RunEventType, RunStatus


class TerminalCondition(EvaluationModel):
    expected_status: RunStatus | None = None
    expected_event_type: RunEventType | None = None
    expected_hitl_required: bool | None = None
    expected_hitl_state: HITLState | None = None


class ScenarioContract(EvaluationModel):
    scenario_id: str
    scenario_input: dict[str, Any] = Field(default_factory=dict)
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    ordering_constraints: list[tuple[str, str]] = Field(default_factory=list)
    terminal: TerminalCondition | None = None
    required_evidence: list[str] = Field(default_factory=list)
    expected_asset_identity: str | None = None


SCENARIO_CONTRACTS_VERSION = "1.0.0"


CONTRACT_MANIFEST: dict[str, str] = {
    "GS-08": "gs_08.json",
    "GS-DEBUG-TRAJ-01": "gs_debug_traj_01.json",
}


class ScenarioContractNotFoundError(KeyError):
    pass


def load_scenario_contract(scenario_id: str) -> ScenarioContract:
    try:
        filename = CONTRACT_MANIFEST[scenario_id]
    except KeyError as exc:
        raise ScenarioContractNotFoundError(scenario_id) from exc

    contract_path = files("obs_platform.evaluation.scenario_contracts").joinpath(
        filename
    )
    payload = cast(dict[str, Any], json.loads(contract_path.read_text()))
    contract = ScenarioContract.model_validate(payload)
    if contract.scenario_id != scenario_id:
        raise ValueError(
            f"scenario contract {filename} declares {contract.scenario_id}, "
            f"expected {scenario_id}"
        )
    return contract


def load_scenario_contracts() -> dict[str, ScenarioContract]:
    return {
        scenario_id: load_scenario_contract(scenario_id)
        for scenario_id in CONTRACT_MANIFEST
    }


SCENARIO_CONTRACTS: dict[str, ScenarioContract] = load_scenario_contracts()
