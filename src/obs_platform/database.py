import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from obs_platform.config import DatabaseSettings

DatabasePing = Callable[[AsyncEngine], Awaitable[None]]
Sleep = Callable[[float], Awaitable[Any]]


class Base(DeclarativeBase):
    pass


class DatabaseUnavailableError(RuntimeError):
    pass


def create_engine(settings: DatabaseSettings) -> AsyncEngine:
    return create_async_engine(settings.url, pool_pre_ping=True)


async def ping_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def wait_for_database(
    engine: AsyncEngine,
    *,
    attempts: int = 5,
    delay_seconds: float = 0.25,
    ping: DatabasePing = ping_database,
    sleep: Sleep = asyncio.sleep,
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            await ping(engine)
            return
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                await sleep(delay_seconds)

    raise DatabaseUnavailableError("database is unavailable") from last_error


async def check_database(
    settings: DatabaseSettings,
    *,
    attempts: int = 5,
    delay_seconds: float = 0.25,
) -> None:
    engine = create_engine(settings)
    try:
        await wait_for_database(
            engine,
            attempts=attempts,
            delay_seconds=delay_seconds,
        )
    finally:
        await engine.dispose()
