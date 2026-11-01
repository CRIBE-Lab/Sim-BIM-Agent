"""Run configuration: environment variables, then a TOML file, then defaults."""

from decimal import Decimal
from pathlib import Path
from typing import Literal, override

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


class RoleModelSchema(BaseModel):
    model: str
    providers: list[str] = Field(
        description="OpenRouter provider slugs tried in order; no other provider is used."
    )
    max_tokens: int = Field(
        gt=0, description="Caps thinking plus answer; the only reliable thinking limit."
    )
    reasoning_effort: Literal["xhigh", "high", "medium", "low", "none"] | None = None
    max_images: int = Field(
        default=8,
        ge=1,
        description="Images per request the providers accept (Cerebras: 2).",
    )
    parallel_tool_calls: bool = True


class SimBimSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SIM_BIM_",
        env_nested_delimiter="__",
        nested_model_default_partial_update=True,
    )

    openrouter_api_key: SecretStr = Field(validation_alias="OPENROUTER_API_KEY")
    coordinator: RoleModelSchema = RoleModelSchema(
        model="deepseek/deepseek-v4.1-flash-1017",
        providers=["together", "fireworks"],
        max_tokens=24000,
        reasoning_effort="high",
    )
    reader: RoleModelSchema = RoleModelSchema(
        model="qwen/qwen3.8-27b",
        providers=["near-ai", "chutes"],
        max_tokens=24000,
        reasoning_effort="medium",
        max_images=4,
    )
    cost_limit_usd: Decimal = Field(default=Decimal("1.00"), gt=0)
    max_concurrent_readers: int = Field(
        default=8,
        ge=1,
        description="Reader agents running at once, across sheets and views.",
    )
    request_timeout_s: float = Field(default=600.0, gt=0)
    output_retries: int = Field(default=3, ge=0)
    request_limit: int = Field(
        default=200,
        ge=1,
        description="Model requests allowed across all agents of a run.",
    )
    otlp_endpoint: str | None = Field(
        default=None,
        description="OTLP/HTTP traces endpoint, e.g. http://phoenix:6006/v1/traces.",
    )

    @override
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, env_settings, TomlConfigSettingsSource(settings_cls))


def load_settings(toml_path: Path | None) -> SimBimSettings:
    """Load settings, layering ``toml_path`` under environment variables."""

    class _Settings(SimBimSettings):
        model_config = SettingsConfigDict(
            env_prefix="SIM_BIM_",
            env_nested_delimiter="__",
            nested_model_default_partial_update=True,
            toml_file=toml_path,
        )

    return _Settings()
