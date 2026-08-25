from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from obs_platform.config import Settings
from obs_platform.database import DatabaseUnavailableError
from obs_platform.database import check_database as check_database_settings
from obs_platform.database import create_engine, ping_database, wait_for_database
from obs_platform.routes import health, runs

HealthCheck = Callable[[], Awaitable[None]]


def create_app(
    *,
    settings: Settings | None = None,
    check_database: HealthCheck | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        if check_database is not None:
            app.state.check_database = check_database
            yield
            return

        resolved_settings = settings or Settings()
        engine = create_engine(resolved_settings.db)

        async def pooled_check_database() -> None:
            try:
                await ping_database(engine)
            except Exception as exc:
                raise DatabaseUnavailableError("database is unavailable") from exc

        app.state.settings = resolved_settings
        app.state.db_engine = engine
        app.state.check_database = pooled_check_database
        await wait_for_database(engine)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="Agent Observability Platform", lifespan=lifespan)
    if check_database is not None:
        app.state.check_database = check_database
    elif settings is not None:
        app.state.settings = settings
        app.state.check_database = lambda: check_database_settings(settings.db)
    app.include_router(health.router)
    app.include_router(runs.router, prefix="/v1")
    return app


app = create_app()
