from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseModel):
    host: str
    port: int = Field(gt=0, le=65535)
    user: str
    password: str
    name: str


class APISettings(BaseModel):
    host: str
    port: int = Field(gt=0, le=65535)
    log_level: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    db: DatabaseSettings
    api: APISettings
