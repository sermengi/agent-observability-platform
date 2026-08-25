from fastapi import FastAPI

from obs_platform.routes import health, runs


def create_app() -> FastAPI:
    app = FastAPI(title="Agent Observability Platform")
    app.include_router(health.router)
    app.include_router(runs.router, prefix="/v1")
    return app


app = create_app()
