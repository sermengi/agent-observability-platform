from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseModel):
    host: str
    port: int = Field(gt=0, le=65535)
    user: str
    password: str
    name: str

    @property
    def url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.name}"
        )


class APISettings(BaseModel):
    host: str
    port: int = Field(gt=0, le=65535)
    log_level: str


class JudgeSettings(BaseModel):
    provider: Literal["anthropic"] = "anthropic"
    model: str = "claude-sonnet-4-6"
    anthropic_api_key: str | None = None
    max_tokens: int = Field(default=1024, gt=0)

    @property
    def is_configured(self) -> bool:
        return bool(self.model and self.anthropic_api_key)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    db: DatabaseSettings
    api: APISettings
    judge: JudgeSettings = Field(default_factory=JudgeSettings)


class DatabaseOnlySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    db: DatabaseSettings


class JudgeOnlySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    judge: JudgeSettings = Field(default_factory=JudgeSettings)
