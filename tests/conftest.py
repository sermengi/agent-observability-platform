from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from obs_platform.config import Settings
from obs_platform.main import create_app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app(settings=Settings(_env_file=".env.example"))

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as async_client:
            yield async_client
