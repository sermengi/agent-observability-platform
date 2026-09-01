from pathlib import Path

import pytest
from pydantic import ValidationError

from obs_platform.config import APISettings, DatabaseSettings, Settings

DB_ENV = {
    "DB__HOST": "localhost",
    "DB__PORT": "5432",
    "DB__USER": "observability",
    "DB__PASSWORD": "local-password",
    "DB__NAME": "observability",
}


async def test_settings_requires_database_host(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in DB_ENV.items():
        if key != "DB__HOST":
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("API__HOST", "127.0.0.1")
    monkeypatch.setenv("API__PORT", "9000")
    monkeypatch.setenv("API__LOG_LEVEL", "debug")
    for key in ("DB__HOST",):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    assert "db.host" in str(exc_info.value)


async def test_settings_loads_nested_environment_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in DB_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("API__HOST", "127.0.0.1")
    monkeypatch.setenv("API__PORT", "9000")
    monkeypatch.setenv("API__LOG_LEVEL", "debug")

    settings = Settings()

    assert isinstance(settings.db, DatabaseSettings)
    assert isinstance(settings.api, APISettings)
    assert settings.db.host == "localhost"
    assert settings.db.port == 5432
    assert settings.api.host == "127.0.0.1"
    assert settings.api.port == 9000
    assert settings.api.log_level == "debug"


async def test_env_example_documents_all_settings_variables() -> None:
    env_example = Path(".env.example").read_text()

    expected_variables = {
        "DB__HOST",
        "DB__PORT",
        "DB__USER",
        "DB__PASSWORD",
        "DB__NAME",
        "API__HOST",
        "API__PORT",
        "API__LOG_LEVEL",
        "JUDGE__PROVIDER",
        "JUDGE__MODEL",
        "JUDGE__ANTHROPIC_API_KEY",
    }

    for variable in expected_variables:
        assert f"{variable}=" in env_example


async def test_dotenv_is_ignored() -> None:
    import subprocess

    gitignore = Path(".gitignore").read_text()
    assert ".env" in gitignore.splitlines()

    tracked = subprocess.run(
        ["git", "ls-files", ".env"], capture_output=True, text=True, check=True
    ).stdout
    assert tracked.strip() == ""
