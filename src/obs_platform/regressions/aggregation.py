from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from obs_platform.db.models import AgentRun, EvaluationResult, JudgeCall, RunFailure


@dataclass(frozen=True)
class PassRateSummary:
    pass_rate: float | None
    counts: dict[str, int]


@dataclass(frozen=True)
class ScenarioPassRateSummary(PassRateSummary):
    scenario_id: str


@dataclass(frozen=True)
class EvaluatorPassRateSummary:
    evaluator_name: str
    total_count: int
    skipped_count: int
    passed_count: int
    pass_rate: float | None


@dataclass(frozen=True)
class AgentMetricsSummary:
    avg_latency_ms: float | None
    p95_latency_ms: float | None
    avg_tokens: float | None
    avg_cost_usd: float | None


@dataclass(frozen=True)
class EvaluationMetricsSummary:
    total_tokens: int
    avg_tokens: float | None
    total_cost_usd: float
    avg_cost_usd: float | None
    avg_latency_ms: float | None


@dataclass(frozen=True)
class RegressionAggregation:
    regression_run_id: int
    overall: PassRateSummary
    by_scenario: list[ScenarioPassRateSummary]
    by_evaluator: list[EvaluatorPassRateSummary]
    failure_distribution: list[tuple[str, int]]
    agent: AgentMetricsSummary
    evaluation: EvaluationMetricsSummary


async def aggregate_regression_run(
    session: AsyncSession,
    regression_run_id: int,
) -> RegressionAggregation:
    overall = await _pass_rate_summary(session, regression_run_id)
    by_scenario = await _scenario_pass_rate_summaries(session, regression_run_id)
    by_evaluator = await _evaluator_pass_rate_summaries(session, regression_run_id)
    failure_distribution = await _failure_distribution(session, regression_run_id)
    agent = await _agent_metrics(session, regression_run_id)
    evaluation = await _evaluation_metrics(session, regression_run_id)
    return RegressionAggregation(
        regression_run_id=regression_run_id,
        overall=overall,
        by_scenario=by_scenario,
        by_evaluator=by_evaluator,
        failure_distribution=failure_distribution,
        agent=agent,
        evaluation=evaluation,
    )


async def _pass_rate_summary(
    session: AsyncSession,
    regression_run_id: int,
) -> PassRateSummary:
    rows = await session.execute(
        select(RunFailure.overall_status, func.count())
        .select_from(RunFailure)
        .join(AgentRun, AgentRun.run_id == RunFailure.run_id)
        .where(AgentRun.regression_run_id == regression_run_id)
        .group_by(RunFailure.overall_status)
    )
    return _pass_rate_summary_from_rows(rows.tuples().all())


async def _scenario_pass_rate_summaries(
    session: AsyncSession,
    regression_run_id: int,
) -> list[ScenarioPassRateSummary]:
    rows = await session.execute(
        select(AgentRun.scenario_id, RunFailure.overall_status, func.count())
        .select_from(RunFailure)
        .join(AgentRun, AgentRun.run_id == RunFailure.run_id)
        .where(AgentRun.regression_run_id == regression_run_id)
        .group_by(AgentRun.scenario_id, RunFailure.overall_status)
        .order_by(AgentRun.scenario_id.asc())
    )
    summaries: dict[str, dict[str, int]] = {}
    for scenario_id, overall_status, count in rows:
        if scenario_id is None:
            continue
        summaries.setdefault(scenario_id, _empty_status_counts())[overall_status] = (
            count
        )
    result = []
    for scenario_id, counts in summaries.items():
        summary = _pass_rate_summary_from_counts(counts)
        result.append(
            ScenarioPassRateSummary(
                scenario_id=scenario_id,
                pass_rate=summary.pass_rate,
                counts=summary.counts,
            )
        )
    return result


async def _evaluator_pass_rate_summaries(
    session: AsyncSession,
    regression_run_id: int,
) -> list[EvaluatorPassRateSummary]:
    rows = await session.execute(
        select(
            EvaluationResult.evaluator_name,
            func.count().label("total_count"),
            func.count()
            .filter(EvaluationResult.status == "skipped")
            .label("skipped_count"),
            func.count()
            .filter(EvaluationResult.passed.is_(True))
            .label("passed_count"),
        )
        .select_from(EvaluationResult)
        .join(AgentRun, AgentRun.run_id == EvaluationResult.run_id)
        .where(AgentRun.regression_run_id == regression_run_id)
        .group_by(EvaluationResult.evaluator_name)
        .order_by(EvaluationResult.evaluator_name.asc())
    )
    summaries = []
    for row in rows:
        denominator = row.total_count - row.skipped_count
        summaries.append(
            EvaluatorPassRateSummary(
                evaluator_name=row.evaluator_name,
                total_count=row.total_count,
                skipped_count=row.skipped_count,
                passed_count=row.passed_count,
                pass_rate=row.passed_count / denominator if denominator else None,
            )
        )
    return summaries


async def _failure_distribution(
    session: AsyncSession,
    regression_run_id: int,
) -> list[tuple[str, int]]:
    rows = await session.execute(
        select(RunFailure.primary_category, func.count())
        .select_from(RunFailure)
        .join(AgentRun, AgentRun.run_id == RunFailure.run_id)
        .where(
            AgentRun.regression_run_id == regression_run_id,
            RunFailure.primary_category.is_not(None),
        )
        .group_by(RunFailure.primary_category)
        .order_by(func.count().desc(), RunFailure.primary_category.asc())
    )
    return [(category, count) for category, count in rows]


async def _agent_metrics(
    session: AsyncSession,
    regression_run_id: int,
) -> AgentMetricsSummary:
    row = (
        await session.execute(
            select(
                func.avg(AgentRun.execution_latency_ms).label("avg_latency_ms"),
                func.percentile_cont(0.95)
                .within_group(AgentRun.execution_latency_ms)
                .filter(AgentRun.execution_latency_ms.is_not(None))
                .label("p95_latency_ms"),
                func.avg(AgentRun.usage_total_tokens).label("avg_tokens"),
                func.avg(AgentRun.usage_total_estimated_cost_usd).label("avg_cost_usd"),
            ).where(AgentRun.regression_run_id == regression_run_id)
        )
    ).one()
    return AgentMetricsSummary(
        avg_latency_ms=_float_or_none(row.avg_latency_ms),
        p95_latency_ms=_float_or_none(row.p95_latency_ms),
        avg_tokens=_float_or_none(row.avg_tokens),
        avg_cost_usd=_float_or_none(row.avg_cost_usd),
    )


async def _evaluation_metrics(
    session: AsyncSession,
    regression_run_id: int,
) -> EvaluationMetricsSummary:
    row = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(JudgeCall.prompt_tokens + JudgeCall.completion_tokens), 0
                ).label("total_tokens"),
                func.avg(JudgeCall.prompt_tokens + JudgeCall.completion_tokens).label(
                    "avg_tokens"
                ),
                func.coalesce(func.sum(JudgeCall.estimated_cost_usd), 0.0).label(
                    "total_cost_usd"
                ),
                func.avg(JudgeCall.estimated_cost_usd).label("avg_cost_usd"),
                func.avg(JudgeCall.latency_ms).label("avg_latency_ms"),
            )
            .select_from(JudgeCall)
            .join(AgentRun, AgentRun.run_id == JudgeCall.run_id)
            .where(AgentRun.regression_run_id == regression_run_id)
        )
    ).one()
    return EvaluationMetricsSummary(
        total_tokens=row.total_tokens,
        avg_tokens=_float_or_none(row.avg_tokens),
        total_cost_usd=float(row.total_cost_usd),
        avg_cost_usd=_float_or_none(row.avg_cost_usd),
        avg_latency_ms=_float_or_none(row.avg_latency_ms),
    )


def _pass_rate_summary_from_rows(
    rows: Iterable[tuple[str, int]],
) -> PassRateSummary:
    counts = _empty_status_counts()
    for overall_status, count in rows:
        counts[overall_status] = count
    return _pass_rate_summary_from_counts(counts)


def _pass_rate_summary_from_counts(counts: dict[str, int]) -> PassRateSummary:
    total = sum(counts.values())
    return PassRateSummary(
        pass_rate=counts["pass"] / total if total else None,
        counts=counts,
    )


def _empty_status_counts() -> dict[str, int]:
    return {"pass": 0, "fail": 0, "incomplete": 0}


def _float_or_none(value: float | int | Decimal | None) -> float | None:
    return float(value) if value is not None else None
