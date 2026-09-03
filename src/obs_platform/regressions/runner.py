import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from obs_platform.db.models import RegressionRun
from obs_platform.evaluation.contracts import (
    ScenarioContract,
    load_scenario_contract,
)
from obs_platform.evaluation.persistence import persist_regression_linkage
from obs_platform.evaluation.service import run_evaluation as default_run_evaluation
from obs_platform.ingestion.runs import ingest_run_event as default_ingest_run_event
from obs_platform.telemetry.v1.models import ExtendedRunEvent

logger = logging.getLogger(__name__)

IngestCallable = Callable[[AsyncSession, ExtendedRunEvent], Awaitable[Any]]
RunEvaluationCallable = Callable[[AsyncSession, str], Awaitable[Any]]


class AgentTarget(ABC):
    @abstractmethod
    async def run_scenario(self, contract: ScenarioContract) -> ExtendedRunEvent:
        raise NotImplementedError


class MockedAgentTarget(AgentTarget):
    def __init__(self, scripted_events: Mapping[str, ExtendedRunEvent]) -> None:
        self._scripted_events = {
            scenario_id: event.model_copy(deep=True)
            for scenario_id, event in scripted_events.items()
        }

    async def run_scenario(self, contract: ScenarioContract) -> ExtendedRunEvent:
        try:
            event = self._scripted_events[contract.scenario_id]
        except KeyError as exc:
            raise KeyError(
                f"no mocked RunEvent configured for {contract.scenario_id}"
            ) from exc
        return event.model_copy(deep=True)


@dataclass(frozen=True)
class RegressionRunError:
    scenario_id: str
    repetition_index: int
    error_type: str
    message: str


@dataclass(frozen=True)
class RegressionRunnerResult:
    regression_run_id: int
    created_run_ids: list[str] = field(default_factory=list)
    errors: list[RegressionRunError] = field(default_factory=list)


class RegressionRunner:
    def __init__(
        self,
        *,
        session: AsyncSession,
        target: AgentTarget,
        ingest_run_event: IngestCallable = default_ingest_run_event,
        run_evaluation: RunEvaluationCallable = default_run_evaluation,
    ) -> None:
        self._session = session
        self._target = target
        self._ingest_run_event = ingest_run_event
        self._run_evaluation = run_evaluation

    async def run(self, regression_run_id: int) -> RegressionRunnerResult:
        regression_run = await self._session.get(RegressionRun, regression_run_id)
        if regression_run is None:
            raise ValueError(f"regression run {regression_run_id!r} does not exist")

        await self._mark_running(regression_run)
        created_run_ids: list[str] = []
        errors: list[RegressionRunError] = []

        for scenario_id in regression_run.scenario_ids:
            contract = load_scenario_contract(scenario_id)
            for repetition_index in range(regression_run.repetitions):
                try:
                    run_id = await self._run_one(
                        regression_run.id,
                        contract,
                        repetition_index,
                    )
                    created_run_ids.append(run_id)
                except Exception as exc:
                    await self._session.rollback()
                    logger.exception(
                        "regression repetition failed",
                        extra={
                            "regression_run_id": regression_run.id,
                            "scenario_id": scenario_id,
                            "repetition_index": repetition_index,
                        },
                    )
                    errors.append(
                        RegressionRunError(
                            scenario_id=scenario_id,
                            repetition_index=repetition_index,
                            error_type=type(exc).__name__,
                            message=str(exc),
                        )
                    )

        await self._mark_completed(regression_run)
        return RegressionRunnerResult(
            regression_run_id=regression_run.id,
            created_run_ids=created_run_ids,
            errors=errors,
        )

    async def _run_one(
        self,
        regression_run_id: int,
        contract: ScenarioContract,
        repetition_index: int,
    ) -> str:
        event = await self._target.run_scenario(contract)
        run_id = _regression_run_event_id(contract.scenario_id, repetition_index)
        event = event.model_copy(
            deep=True,
            update={
                "run_id": run_id,
            },
        )
        await self._ingest_run_event(self._session, event)
        await persist_regression_linkage(
            self._session,
            run_id=event.run_id,
            regression_run_id=regression_run_id,
            scenario_id=contract.scenario_id,
            repetition_index=repetition_index,
        )
        await self._run_evaluation(self._session, event.run_id)
        return event.run_id

    async def _mark_running(self, regression_run: RegressionRun) -> None:
        regression_run.status = "running"
        regression_run.started_at = datetime.now(UTC)
        regression_run.completed_at = None
        await self._session.commit()

    async def _mark_completed(self, regression_run: RegressionRun) -> None:
        regression_run.status = "completed"
        regression_run.completed_at = datetime.now(UTC)
        await self._session.commit()


def _regression_run_event_id(scenario_id: str, repetition_index: int) -> str:
    return f"phase7-{scenario_id}-rep-{repetition_index}"
