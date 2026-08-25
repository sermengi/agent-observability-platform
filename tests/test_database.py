import asyncio

from sqlalchemy.ext.asyncio import AsyncEngine

from obs_platform.config import DatabaseSettings
from obs_platform.database import wait_for_database


async def test_database_settings_builds_asyncpg_url() -> None:
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
