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
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    ordering_constraints: list[tuple[str, str]] = Field(default_factory=list)
    terminal: TerminalCondition | None = None
    required_evidence: list[str] = Field(default_factory=list)
    expected_asset_identity: str | None = None


SCENARIO_CONTRACTS: dict[str, ScenarioContract] = {}
