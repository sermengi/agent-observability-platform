from datetime import datetime

from pydantic import BaseModel, ConfigDict

from obs_platform.telemetry.v1.enums import HITLState, RunEventType, RunStatus


class APIResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RunSummary(APIResponseModel):
    run_id: str
    scenario_id: str | None
    agent_name: str
    agent_version: str
    prompt_version: str
    environment: str
    status: RunStatus
    event_type: RunEventType
    hitl_state: HITLState
    started_at: datetime
    completed_at: datetime | None
    execution_latency_ms: int | None
    wall_clock_duration_ms: int | None
    usage_total_tokens: int
    usage_total_estimated_cost_usd: float


class RunListResponse(APIResponseModel):
    items: list[RunSummary]
    total: int
    limit: int
    offset: int
