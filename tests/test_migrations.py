from pathlib import Path
from typing import cast

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, DOUBLE_PRECISION, JSONB

from obs_platform.database import Base
from obs_platform.db import models


async def test_alembic_env_uses_async_engine_and_shared_settings() -> None:
    env_py = Path("migrations/env.py").read_text()

    assert "AsyncEngine" in env_py
    assert "run_sync" in env_py
    assert "DatabaseOnlySettings" in env_py
    assert ".db.url" in env_py
    assert "target_metadata = Base.metadata" in env_py
    assert "postgresql+asyncpg://" not in env_py


async def test_initial_migration_is_empty_domain_bootstrap() -> None:
    migration_files = list(Path("migrations/versions").glob("*.py"))
    initial_migration = Path(
        "migrations/versions/20260825_0001_initial_empty_bootstrap.py"
    )

    assert len(migration_files) == 5
    migration = initial_migration.read_text()
    assert "op.create_table" not in migration
    assert "op.drop_table" not in migration
    assert "pass" in migration


async def test_phase_2_migration_creates_core_tables_only() -> None:
    migration = Path(
        "migrations/versions/20260827_0002_create_phase_2_core_tables.py"
    ).read_text()

    assert 'down_revision: str | Sequence[str] | None = "20260825_0001"' in migration
    for table_name in {
        "agent_runs",
        "spans",
        "tool_calls",
        "llm_calls",
        "evaluation_results",
        "run_failures",
    }:
        assert f'"{table_name}"' in migration
    assert '"judge_calls"' not in migration
    assert '"regression_runs"' not in migration
    assert migration.count("op.create_table(") == 6
    assert migration.count("op.drop_table(") == 6


async def test_phase_2_index_migration_creates_minimal_indexes() -> None:
    migration = Path(
        "migrations/versions/20260827_0003_add_phase_2_minimal_indexes.py"
    ).read_text()

    assert 'down_revision: str | Sequence[str] | None = "20260827_0002"' in migration
    for index_name in {
        "ix_agent_runs_status",
        "ix_agent_runs_scenario_id",
        "ix_agent_runs_agent_version",
        "ix_agent_runs_started_at",
        "ix_spans_parent_span_id",
        "ix_tool_calls_span_id",
        "ix_tool_calls_tool_name_status",
        "ix_llm_calls_span_id",
        "ix_llm_calls_model",
    }:
        assert index_name in migration

    assert "ix_run_failures_primary_category" not in migration
    assert "ix_run_failures_secondary_category" not in migration
    assert "ix_evaluation_results_evaluator_name" not in migration


async def test_make_up_runs_migrations_before_compose_startup() -> None:
    makefile = Path("Makefile").read_text()

    assert "include .env.example" in makefile
    assert "include .env" in makefile
    assert "export" in makefile
    assert "up:" in makefile
    assert "docker compose up -d --wait postgres" in makefile
    assert "docker compose build api" in makefile
    assert "docker compose run --rm api alembic upgrade head" in makefile
    assert "docker compose up -d" in makefile
    assert makefile.index("docker compose up -d --wait postgres") < makefile.index(
        "docker compose build api"
    )
    assert makefile.index("docker compose build api") < makefile.index(
        "docker compose run --rm api alembic upgrade head"
    )
    assert makefile.index(
        "docker compose run --rm api alembic upgrade head"
    ) < makefile.rindex("docker compose up -d")
    assert "uv run alembic upgrade head" not in makefile


async def test_database_metadata_defines_phase_2_core_tables() -> None:
    assert set(Base.metadata.tables) == {
        "agent_runs",
        "spans",
        "tool_calls",
        "llm_calls",
        "evaluation_results",
        "run_failures",
        "judge_calls",
    }
    assert "hitl_state_transitions" not in Base.metadata.tables
    assert "human_approvals" not in Base.metadata.tables


async def test_phase_2_primary_key_and_unique_constraints() -> None:
    spans = cast(Table, models.Span.__table__)
    tool_calls = cast(Table, models.ToolCall.__table__)
    llm_calls = cast(Table, models.LLMCall.__table__)

    assert [column.name for column in spans.primary_key] == ["id"]
    assert spans.c.id.autoincrement is True
    assert _has_unique_constraint(spans, ["run_id", "span_id"])

    assert "id" not in tool_calls.c
    assert [column.name for column in tool_calls.primary_key] == [
        "run_id",
        "tool_call_id",
    ]

    assert "id" not in llm_calls.c
    assert [column.name for column in llm_calls.primary_key] == [
        "run_id",
        "llm_call_id",
    ]


async def test_phase_2_error_fields_are_flattened_columns() -> None:
    for table_name, prefix in {
        "agent_runs": "runtime_error",
        "spans": "error",
        "tool_calls": "error",
        "llm_calls": "error",
    }.items():
        table = Base.metadata.tables[table_name]
        assert f"{prefix}_category" in table.c
        assert f"{prefix}_code" in table.c
        assert f"{prefix}_message" in table.c
        assert f"{prefix}_failed_component" in table.c
        assert "error" not in table.c


async def test_evaluation_results_regression_run_is_reserved_without_fk() -> None:
    table = cast(Table, models.EvaluationResult.__table__)

    assert "regression_run_id" in table.c
    assert table.c.regression_run_id.nullable is True
    assert not _foreign_key_constraints_for(table, ["regression_run_id"])
    assert not _has_unique_constraint(table, ["run_id", "evaluator_name"])
    assert _has_unique_constraint(
        table,
        ["run_id", "evaluator_name", "evaluator_version", "regression_run_id"],
    )
    assert "regression_runs" not in Base.metadata.tables


async def test_check_constraints_cover_locked_vocabularies() -> None:
    constrained_columns = {
        "agent_runs": ["event_type", "status", "hitl_state", "hitl_decision"],
        "spans": ["status"],
        "tool_calls": ["status"],
        "llm_calls": ["call_type", "status"],
        "evaluation_results": ["status"],
        "run_failures": ["overall_status"],
    }

    for table_name, column_names in constrained_columns.items():
        expressions = _check_constraint_sql(Base.metadata.tables[table_name])
        for column_name in column_names:
            assert any(column_name in expression for expression in expressions)

    unconstrained_columns = {
        "evaluation_results": ["label", "severity"],
        "run_failures": ["primary_category", "secondary_category", "max_severity"],
    }

    for table_name, column_names in unconstrained_columns.items():
        expressions = _check_constraint_sql(Base.metadata.tables[table_name])
        for column_name in column_names:
            assert not any(column_name in expression for expression in expressions)


async def test_phase_5_evaluation_status_snapshot_migration() -> None:
    migration = Path(
        "migrations/versions/20260830_0004_add_phase_5_evaluation_status_snapshot.py"
    ).read_text()

    assert 'down_revision: str | Sequence[str] | None = "20260827_0003"' in migration
    assert '"overall_status"' in migration
    assert '"classifier_version"' in migration
    assert '"passed"' in migration
    assert "sa.Boolean()" in migration
    assert "ck_evaluation_results_status" in migration
    assert "ck_run_failures_overall_status" in migration


async def test_phase_6_judge_calls_migration() -> None:
    migration = Path(
        "migrations/versions/20260901_0005_create_judge_calls.py"
    ).read_text()

    assert 'down_revision: str | Sequence[str] | None = "20260830_0004"' in migration
    assert '"judge_calls"' in migration
    assert '"evaluator_name"' in migration
    assert '"evaluator_version"' in migration
    assert '"provider"' in migration
    assert '"model"' in migration
    assert '"latency_ms"' in migration
    assert '"prompt_tokens"' in migration
    assert '"completion_tokens"' in migration
    assert '"estimated_cost_usd"' in migration
    assert '"succeeded"' in migration
    assert 'sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"])' in migration
    assert "evaluation_results" not in migration


async def test_judge_calls_table_tracks_evaluation_cost_separately() -> None:
    table = cast(Table, models.JudgeCall.__table__)

    assert [column.name for column in table.primary_key] == ["id"]
    assert table.c.id.autoincrement is True
    assert [column.name for column in table.c] == [
        "id",
        "run_id",
        "evaluator_name",
        "evaluator_version",
        "model",
        "provider",
        "latency_ms",
        "prompt_tokens",
        "completion_tokens",
        "estimated_cost_usd",
        "succeeded",
        "created_at",
    ]
    assert _foreign_key_constraints_for(table, ["run_id"])
    assert not _foreign_key_constraints_for(table, ["evaluator_name"])


async def test_phase_2_jsonb_array_and_double_precision_columns() -> None:
    agent_runs = cast(Table, models.AgentRun.__table__)
    spans = cast(Table, models.Span.__table__)
    tool_calls = cast(Table, models.ToolCall.__table__)
    llm_calls = cast(Table, models.LLMCall.__table__)
    evaluation_results = cast(Table, models.EvaluationResult.__table__)
    judge_calls = cast(Table, models.JudgeCall.__table__)

    for column in [
        agent_runs.c.raw_input,
        agent_runs.c.hitl_pending_action,
        agent_runs.c.final_result_output,
        spans.c.input,
        spans.c.output,
        spans.c.metadata,
        tool_calls.c.arguments,
        tool_calls.c.result,
        llm_calls.c.input_payload,
        llm_calls.c.output_payload,
        evaluation_results.c.findings,
    ]:
        assert isinstance(column.type, JSONB)

    assert isinstance(agent_runs.c.final_result_source_references.type, ARRAY)
    assert isinstance(llm_calls.c.estimated_cost_usd.type, DOUBLE_PRECISION)
    assert isinstance(judge_calls.c.estimated_cost_usd.type, DOUBLE_PRECISION)
    assert isinstance(
        agent_runs.c.usage_total_estimated_cost_usd.type,
        DOUBLE_PRECISION,
    )


async def test_phase_2_relational_policy_columns_are_not_jsonb() -> None:
    relational_columns = {
        "agent_runs": [
            "run_id",
            "schema_version",
            "event_type",
            "agent_name",
            "agent_version",
            "prompt_version",
            "environment",
            "normalized_input",
            "scenario_id",
            "started_at",
            "completed_at",
            "status",
            "execution_latency_ms",
            "wall_clock_duration_ms",
            "resume_count",
            "hitl_required",
            "hitl_state",
            "hitl_checkpoint_id",
            "hitl_decision",
            "hitl_requested_at",
            "hitl_decided_at",
            "usage_total_llm_calls",
            "usage_total_tool_calls",
            "usage_total_tokens",
            "usage_total_retries",
            "usage_total_estimated_cost_usd",
            "runtime_error_category",
            "runtime_error_code",
            "runtime_error_message",
            "runtime_error_failed_component",
            "ingested_at",
            "updated_at",
        ],
        "spans": [
            "id",
            "run_id",
            "span_id",
            "parent_span_id",
            "name",
            "sequence",
            "started_at",
            "completed_at",
            "status",
            "error_category",
            "error_code",
            "error_message",
            "error_failed_component",
        ],
        "tool_calls": [
            "run_id",
            "tool_call_id",
            "span_id",
            "tool_name",
            "sequence",
            "started_at",
            "completed_at",
            "latency_ms",
            "retry_count",
            "status",
            "error_category",
            "error_code",
            "error_message",
            "error_failed_component",
        ],
        "llm_calls": [
            "run_id",
            "llm_call_id",
            "span_id",
            "call_type",
            "model",
            "provider",
            "started_at",
            "completed_at",
            "latency_ms",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "estimated_cost_usd",
            "status",
            "error_category",
            "error_code",
            "error_message",
            "error_failed_component",
        ],
        "evaluation_results": [
            "id",
            "run_id",
            "evaluator_name",
            "evaluator_version",
            "regression_run_id",
            "status",
            "passed",
            "score",
            "label",
            "severity",
            "reason",
            "created_at",
        ],
        "run_failures": [
            "run_id",
            "overall_status",
            "primary_category",
            "secondary_category",
            "max_severity",
            "classifier_version",
            "updated_at",
        ],
        "judge_calls": [
            "id",
            "run_id",
            "evaluator_name",
            "evaluator_version",
            "model",
            "provider",
            "latency_ms",
            "prompt_tokens",
            "completion_tokens",
            "estimated_cost_usd",
            "succeeded",
            "created_at",
        ],
    }

    for table_name, column_names in relational_columns.items():
        table = Base.metadata.tables[table_name]
        for column_name in column_names:
            assert not isinstance(table.c[column_name].type, JSONB)


async def test_phase_2_metadata_defines_minimal_indexes() -> None:
    assert _index_columns(cast(Table, models.AgentRun.__table__)) == {
        "ix_agent_runs_status": ["status"],
        "ix_agent_runs_scenario_id": ["scenario_id"],
        "ix_agent_runs_agent_version": ["agent_version"],
        "ix_agent_runs_started_at": ["started_at"],
    }
    assert _index_columns(cast(Table, models.Span.__table__)) == {
        "ix_spans_parent_span_id": ["parent_span_id"],
    }
    assert _index_columns(cast(Table, models.ToolCall.__table__)) == {
        "ix_tool_calls_span_id": ["span_id"],
        "ix_tool_calls_tool_name_status": ["tool_name", "status"],
    }
    assert _index_columns(cast(Table, models.LLMCall.__table__)) == {
        "ix_llm_calls_span_id": ["span_id"],
        "ix_llm_calls_model": ["model"],
    }
    assert _index_columns(cast(Table, models.EvaluationResult.__table__)) == {}
    assert _index_columns(cast(Table, models.RunFailure.__table__)) == {}
    assert _index_columns(cast(Table, models.JudgeCall.__table__)) == {}


def _has_unique_constraint(table: Table, column_names: list[str]) -> bool:
    return any(
        isinstance(constraint, UniqueConstraint)
        and [column.name for column in constraint.columns] == column_names
        for constraint in table.constraints
    )


def _foreign_key_constraints_for(
    table: Table,
    column_names: list[str],
) -> list[ForeignKeyConstraint]:
    return [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and [column.name for column in constraint.columns] == column_names
    ]


def _check_constraint_sql(table: Table) -> list[str]:
    return [
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    ]


def _index_columns(table: Table) -> dict[str, list[str]]:
    return {
        cast(str, index.name): [column.name for column in index.columns]
        for index in table.indexes
        if isinstance(index, Index)
    }
