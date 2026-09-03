import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from obs_platform.db.models import AgentRun, RegressionRun, ToolCall
from obs_platform.evaluation.contracts import (
    ScenarioContract,
    load_scenario_contract,
)
from obs_platform.evaluation.persistence import persist_regression_linkage
from obs_platform.evaluation.service import run_evaluation as default_run_evaluation
from obs_platform.ingestion.runs import ingest_run_event as default_ingest_run_event
from obs_platform.telemetry.v1.enums import HITLState
from obs_platform.telemetry.v1.models import ExtendedRunEvent

logger = logging.getLogger(__name__)

IngestCallable = Callable[[AsyncSession, ExtendedRunEvent], Awaitable[Any]]
RunEvaluationCallable = Callable[[AsyncSession, str], Awaitable[Any]]


class AgentTarget(ABC):
    @abstractmethod
    async def run_scenario(self, contract: ScenarioContract) -> ExtendedRunEvent:
        raise NotImplementedError

    @abstractmethod
    async def resume_after_approval(
        self,
        checkpoint_id: str,
        decision: str,
    ) -> ExtendedRunEvent:
        raise NotImplementedError


class MockedAgentTarget(AgentTarget):
    def __init__(
        self,
        scripted_events: Mapping[str, ExtendedRunEvent],
        *,
        approved_events: Mapping[str, ExtendedRunEvent] | None = None,
    ) -> None:
        self._scripted_events = {
            scenario_id: event.model_copy(deep=True)
            for scenario_id, event in scripted_events.items()
        }
        self._approved_events = {
            scenario_id: event.model_copy(deep=True)
            for scenario_id, event in (approved_events or {}).items()
        }

    async def run_scenario(self, contract: ScenarioContract) -> ExtendedRunEvent:
        try:
            event = self._scripted_events[contract.scenario_id]
        except KeyError as exc:
            raise KeyError(
                f"no mocked RunEvent configured for {contract.scenario_id}"
            ) from exc
        return event.model_copy(deep=True)

    async def resume_after_approval(
        self,
        checkpoint_id: str,
        decision: str,
    ) -> ExtendedRunEvent:
        if decision != "approve":
            raise ValueError(f"unsupported mocked HITL decision: {decision!r}")

        for event in self._approved_events.values():
            if event.hitl.checkpoint_id == checkpoint_id:
                return event.model_copy(deep=True)
        if len(self._approved_events) == 1:
            return next(iter(self._approved_events.values())).model_copy(deep=True)
        raise KeyError(f"no mocked approved RunEvent for checkpoint {checkpoint_id!r}")


class HITLGateViolationError(RuntimeError):
    pass


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

        run_id = regression_run.id
        scenario_ids = list(regression_run.scenario_ids)
        repetitions = regression_run.repetitions
        await self._mark_running(run_id)
        created_run_ids: list[str] = []
        errors: list[RegressionRunError] = []

        for scenario_id in scenario_ids:
            contract = load_scenario_contract(scenario_id)
            for repetition_index in range(repetitions):
                try:
                    child_run_id = await self._run_one(
                        run_id,
                        contract,
                        repetition_index,
                    )
                    created_run_ids.append(child_run_id)
                except HITLGateViolationError:
                    await self._session.rollback()
                    await self._mark_failed(run_id)
                    raise
                except Exception as exc:
                    await self._session.rollback()
                    logger.exception(
                        "regression repetition failed",
                        extra={
                            "regression_run_id": run_id,
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

        await self._mark_completed(run_id)
        return RegressionRunnerResult(
            regression_run_id=run_id,
            created_run_ids=created_run_ids,
            errors=errors,
        )

    async def _run_one(
        self,
        regression_run_id: int,
        contract: ScenarioContract,
        repetition_index: int,
    ) -> str:
        if _requires_hitl_flow(contract):
            return await self._run_one_hitl(
                regression_run_id,
                contract,
                repetition_index,
            )

        event = await self._target.run_scenario(contract)
        run_id = _regression_run_event_id(
            regression_run_id,
            contract.scenario_id,
            repetition_index,
        )
        event = _with_run_id(event, run_id)
        await self._ingest_run_event(self._session, event)
        await persist_regression_linkage(
            self._session,
            run_id=event.run_id,
            regression_run_id=regression_run_id,
            scenario_id=contract.scenario_id,
            repetition_index=repetition_index,
        )
        await self._session.commit()
        await self._run_evaluation(self._session, event.run_id)
        return event.run_id

    async def _run_one_hitl(
        self,
        regression_run_id: int,
        contract: ScenarioContract,
        repetition_index: int,
    ) -> str:
        run_id = _regression_run_event_id(
            regression_run_id,
            contract.scenario_id,
            repetition_index,
        )
        pending_event = _with_run_id(await self._target.run_scenario(contract), run_id)
        await self._ingest_run_event(self._session, pending_event)
        await persist_regression_linkage(
            self._session,
            run_id=run_id,
            regression_run_id=regression_run_id,
            scenario_id=contract.scenario_id,
            repetition_index=repetition_index,
        )

        checkpoint_id = await self._assert_hitl_gate_held(run_id)
        approved_event = _with_run_id(
            await self._target.resume_after_approval(checkpoint_id, "approve"),
            run_id,
        )
        await self._ingest_run_event(self._session, approved_event)
        await persist_regression_linkage(
            self._session,
            run_id=run_id,
            regression_run_id=regression_run_id,
            scenario_id=contract.scenario_id,
            repetition_index=repetition_index,
        )
        await self._session.commit()
        await self._run_evaluation(self._session, run_id)
        return run_id

    async def _assert_hitl_gate_held(self, run_id: str) -> str:
        run = await self._session.get(AgentRun, run_id)
        if run is None:
            raise HITLGateViolationError(
                f"HITL pending snapshot for run {run_id!r} was not persisted"
            )
        if run.hitl_state != HITLState.PENDING.value:
            raise HITLGateViolationError(
                f"HITL gate was not pending for run {run_id!r}: {run.hitl_state!r}"
            )
        if not run.hitl_checkpoint_id:
            raise HITLGateViolationError(
                f"HITL pending snapshot for run {run_id!r} has no checkpoint_id"
            )

        submit_count = await self._session.scalar(
            select(func.count())
            .select_from(ToolCall)
            .where(ToolCall.run_id == run_id)
            .where(ToolCall.tool_name == "submit_work_order")
        )
        if int(submit_count or 0) > 0:
            raise HITLGateViolationError(
                f"HITL gate was bypassed for run {run_id!r}: "
                "submit_work_order was already called"
            )
        checkpoint_id = run.hitl_checkpoint_id
        await self._session.commit()
        return checkpoint_id

    async def _mark_running(self, regression_run_id: int) -> None:
        await self._session.execute(
            update(RegressionRun)
            .where(RegressionRun.id == regression_run_id)
            .values(
                status="running",
                started_at=datetime.now(UTC),
                completed_at=None,
            )
        )
        await self._session.commit()

    async def _mark_completed(self, regression_run_id: int) -> None:
        await self._session.execute(
            update(RegressionRun)
            .where(RegressionRun.id == regression_run_id)
            .values(
                status="completed",
                completed_at=datetime.now(UTC),
            )
        )
        await self._session.commit()

    async def _mark_failed(self, regression_run_id: int) -> None:
        await self._session.execute(
            update(RegressionRun)
            .where(RegressionRun.id == regression_run_id)
            .values(
                status="failed",
                completed_at=datetime.now(UTC),
            )
        )
        await self._session.commit()


def _regression_run_event_id(
    regression_run_id: int,
    scenario_id: str,
    repetition_index: int,
) -> str:
    return f"phase7-regression-{regression_run_id}-{scenario_id}-rep-{repetition_index}"


def _requires_hitl_flow(contract: ScenarioContract) -> bool:
    return (
        contract.terminal is not None
        and contract.terminal.expected_hitl_required is True
    )


def _with_run_id(event: ExtendedRunEvent, run_id: str) -> ExtendedRunEvent:
    return event.model_copy(deep=True, update={"run_id": run_id})
