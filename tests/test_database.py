import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from obs_platform.config import DatabaseSettings
from obs_platform.database import DatabaseUnavailableError, check_database, wait_for_database


def test_database_settings_builds_asyncpg_url() -> None:
    settings = DatabaseSettings(
        host="db.example.test",
        port=15432,
        user="observability",
        password="local-password",
        name="observability",
    )

    assert (
        settings.url
        == "postgresql+asyncpg://observability:local-password@db.example.test:15432/observability"
    )


@pytest.mark.anyio
async def test_check_database_reports_unreachable_database() -> None:
    settings = DatabaseSettings(
        host="127.0.0.1",
        port=1,
        user="observability",
        password="local-password",
        name="observability",
    )

    with pytest.raises(DatabaseUnavailableError):
        await check_database(settings, attempts=1, delay_seconds=0)


@pytest.mark.anyio
async def test_wait_for_database_retries_until_connection_succeeds() -> None:
    attempts = 0

    async def delayed_success(_: AsyncEngine) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("database is still starting")

    await wait_for_database(
        object(),
        attempts=5,
        delay_seconds=0,
        ping=delayed_success,
        sleep=asyncio.sleep,
    )

    assert attempts == 3


@pytest.mark.anyio
async def test_wait_for_database_raises_after_attempts_exhausted() -> None:
    attempts = 0

    async def always_fails(_: AsyncEngine) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError("database is unavailable")

    with pytest.raises(DatabaseUnavailableError):
        await wait_for_database(
            object(),
            attempts=2,
            delay_seconds=0,
            ping=always_fails,
            sleep=asyncio.sleep,
        )

    assert attempts == 2
