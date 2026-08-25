from pathlib import Path

import pytest

from obs_platform.config import Settings


def test_dockerfile_uses_multi_stage_uv_build_and_single_uvicorn_process() -> None:
    dockerfile = Path("Dockerfile").read_text()

    assert "FROM python:3.12-slim AS builder" in dockerfile
    assert "FROM python:3.12-slim AS runtime" in dockerfile
    assert "--mount=type=cache,target=/root/.cache/uv" in dockerfile
    assert "COPY --from=builder" in dockerfile
    assert "CMD [\"uvicorn\", \"obs_platform.main:app\"" in dockerfile
    assert "gunicorn" not in dockerfile
    assert "--reload" not in dockerfile


def test_dockerignore_excludes_local_and_build_artifacts() -> None:
    ignored = set(Path(".dockerignore").read_text().splitlines())

    expected = {
        ".venv",
        ".git",
        "__pycache__",
        ".env",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
    }

    assert expected <= ignored


def test_compose_api_service_is_production_shaped() -> None:
    compose = Path("docker-compose.yml").read_text()

    assert "api:" in compose
    assert "build:" in compose
    assert "dockerfile: Dockerfile" in compose
    assert "env_file:" in compose
    assert "required: false" in compose
    assert "DB__HOST: postgres" in compose
    assert "condition: service_healthy" in compose
    assert "8000:8000" in compose
    assert "--reload" not in compose
    assert "../" not in compose
    assert "volumes:" not in compose.split("api:", maxsplit=1)[1].split(
        "volumes:",
        maxsplit=1,
    )[0]


def test_no_compose_override_file_exists() -> None:
    assert not Path("docker-compose.override.yml").exists()


def test_host_native_settings_use_localhost_from_env_example(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "DB__HOST",
        "DB__PORT",
        "DB__USER",
        "DB__PASSWORD",
        "DB__NAME",
        "API__HOST",
        "API__PORT",
        "API__LOG_LEVEL",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=".env.example")

    assert settings.db.host == "localhost"
