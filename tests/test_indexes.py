from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obs_platform.config import DatabaseSettings
from obs_platform.database import create_engine


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine(
        DatabaseSettings(
            host="localhost",
            port=5432,
            user="observability",
            password="change-me",
            name="observability",
        )
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


async def test_run_listing_status_filter_uses_status_index(
    session: AsyncSession,
) -> None:
    await _seed_agent_runs(session)

    plan = await _explain(
        session,
        """
        SELECT run_id
        FROM agent_runs
        WHERE status = 'awaiting_approval'
        """,
    )

    assert "ix_agent_runs_status" in plan
    assert "Seq Scan on agent_runs" not in plan

    await _delete_index_test_rows(session)


async def test_tool_failure_rate_query_uses_tool_name_status_index(
    session: AsyncSession,
) -> None:
    await _seed_tool_calls(session)

    plan = await _explain(
        session,
        """
        SELECT tool_name, status, count(*)
        FROM tool_calls
        WHERE tool_name = 'rare_tool'
        GROUP BY tool_name, status
        """,
    )

    assert "ix_tool_calls_tool_name_status" in plan
    assert "Seq Scan on tool_calls" not in plan

    await _delete_index_test_rows(session)


async def test_runs_using_model_query_uses_llm_calls_model_index(
    session: AsyncSession,
) -> None:
    await _seed_llm_calls(session)

    plan = await _explain(
        session,
        """
        SELECT run_id
        FROM agent_runs
        WHERE EXISTS (
            SELECT 1
            FROM llm_calls
            WHERE llm_calls.run_id = agent_runs.run_id
              AND llm_calls.model = 'rare-model'
        )
        """,
    )

    assert "ix_llm_calls_model" in plan
    assert "Seq Scan on llm_calls" not in plan

    await _delete_index_test_rows(session)


async def test_deferred_failure_and_evaluator_indexes_do_not_exist(
    session: AsyncSession,
) -> None:
    rows = await session.execute(
        text(
            """
            SELECT tablename, indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename IN ('run_failures', 'evaluation_results')
            """
        )
    )
    index_names = {row.indexname for row in rows}

    assert "ix_run_failures_primary_category" not in index_names
    assert "ix_run_failures_secondary_category" not in index_names
    assert "ix_evaluation_results_evaluator_name" not in index_names


async def _seed_agent_runs(session: AsyncSession) -> None:
    await _delete_index_test_rows(session)
    await session.execute(
        text(
            """
            INSERT INTO agent_runs (
                run_id,
                schema_version,
                event_type,
                agent_name,
                agent_version,
                prompt_version,
                environment,
                raw_input,
                started_at,
                completed_at,
                status,
                resume_count,
                hitl_required,
                hitl_state,
                ingested_at,
                updated_at
            )
            SELECT
                'idx-run-' || series::text,
                '1.0',
                CASE WHEN series = 1 THEN 'run_awaiting_approval' ELSE 'run_final' END,
                'index-test-agent',
                CASE WHEN series % 2 = 0 THEN 'v1' ELSE 'v2' END,
                'prompt-index-test',
                'test',
                '{"source": "index-test"}'::jsonb,
                now() - (series || ' seconds')::interval,
                CASE WHEN series = 1 THEN NULL ELSE now() END,
                CASE WHEN series = 1 THEN 'awaiting_approval' ELSE 'success' END,
                0,
                series = 1,
                CASE WHEN series = 1 THEN 'pending' ELSE 'not_required' END,
                now(),
                now()
            FROM generate_series(1, 5000) AS series
            """
        )
    )
    await session.execute(text("ANALYZE agent_runs"))
    await session.commit()


async def _seed_tool_calls(session: AsyncSession) -> None:
    await _seed_agent_runs(session)
    await _seed_spans(session)
    await session.execute(
        text(
            """
            INSERT INTO tool_calls (
                run_id,
                tool_call_id,
                span_id,
                tool_name,
                sequence,
                arguments,
                started_at,
                completed_at,
                retry_count,
                status
            )
            SELECT
                'idx-run-' || series::text,
                'idx-tool-' || series::text,
                spans.id,
                CASE WHEN series = 1 THEN 'rare_tool' ELSE 'common_tool' END,
                series,
                '{"source": "index-test"}'::jsonb,
                now(),
                now(),
                0,
                CASE WHEN series = 1 THEN 'failure' ELSE 'success' END
            FROM generate_series(1, 5000) AS series
            JOIN spans ON spans.run_id = 'idx-run-' || series::text
            """
        )
    )
    await session.execute(text("ANALYZE tool_calls"))
    await session.commit()


async def _seed_llm_calls(session: AsyncSession) -> None:
    await _seed_agent_runs(session)
    await _seed_spans(session)
    await session.execute(
        text(
            """
            INSERT INTO llm_calls (
                run_id,
                llm_call_id,
                span_id,
                call_type,
                model,
                provider,
                started_at,
                completed_at,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                estimated_cost_usd,
                status
            )
            SELECT
                'idx-run-' || series::text,
                'idx-llm-' || series::text,
                spans.id,
                'synthesis',
                CASE WHEN series = 1 THEN 'rare-model' ELSE 'common-model' END,
                'openai',
                now(),
                now(),
                1,
                1,
                2,
                0.001,
                'success'
            FROM generate_series(1, 5000) AS series
            JOIN spans ON spans.run_id = 'idx-run-' || series::text
            """
        )
    )
    await session.execute(text("ANALYZE llm_calls"))
    await session.commit()


async def _seed_spans(session: AsyncSession) -> None:
    await session.execute(
        text(
            """
            INSERT INTO spans (
                run_id,
                span_id,
                name,
                sequence,
                started_at,
                status
            )
            SELECT
                'idx-run-' || series::text,
                'idx-span-' || series::text,
                'index test span',
                1,
                now(),
                'success'
            FROM generate_series(1, 5000) AS series
            """
        )
    )
    await session.execute(text("ANALYZE spans"))
    await session.commit()


async def _explain(session: AsyncSession, sql: str) -> str:
    rows = await session.execute(text(f"EXPLAIN {sql}"))
    return "\n".join(row[0] for row in rows)


async def _delete_index_test_rows(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM llm_calls WHERE run_id LIKE 'idx-run-%'"))
    await session.execute(text("DELETE FROM tool_calls WHERE run_id LIKE 'idx-run-%'"))
    await session.execute(text("DELETE FROM spans WHERE run_id LIKE 'idx-run-%'"))
    await session.execute(text("DELETE FROM agent_runs WHERE run_id LIKE 'idx-run-%'"))
    await session.commit()
