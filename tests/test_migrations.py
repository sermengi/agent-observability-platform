from pathlib import Path

from obs_platform.database import Base


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

    assert len(migration_files) == 1
    migration = migration_files[0].read_text()
    assert "op.create_table" not in migration
    assert "op.drop_table" not in migration
    assert "pass" in migration


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


async def test_database_metadata_starts_without_domain_tables() -> None:
    assert Base.metadata.tables == {}
