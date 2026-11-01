"""OpenRouter models with provider pinning, reported cost and thinking-exhaustion errors."""

import time
from decimal import Decimal
from typing import override

from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings
from pydantic_ai.providers.openrouter import OpenRouterModelProfile, OpenRouterProvider
from pydantic_ai.settings import ModelSettings

from sim_bim.agents.runlog import RunLog
from sim_bim.config import RoleModelSchema


class ThinkingExhaustedError(UnexpectedModelBehavior):
    """The model spent ``max_tokens`` before finishing; thinking counts against it."""


class LoggedOpenRouterModel(OpenRouterModel):
    """OpenRouter chat model that records each response and fills ``usage.cost``.

    OpenRouter reports the billed cost in the response; copying it into
    ``usage.cost`` makes ``UsageLimits.cost_limit`` enforce real spend. Cache
    breakpoints are placed on instructions, tool definitions and the latest
    message; some providers (Fireworks, Near AI) only cache with them.
    Without ``service_tier`` OpenRouter may restrict routing to flex-tier
    endpoints and reject the pinned providers.
    """

    def __init__(
        self, role: str, config: RoleModelSchema, api_key: str, log: RunLog
    ) -> None:
        provider = OpenRouterProvider(api_key=api_key, app_title="Sim-BIM Agent")
        profile = OpenRouterModelProfile(
            openrouter_supports_cache_control=True, openrouter_supports_tool_cache=True
        )
        super().__init__(config.model, provider=provider, profile=profile)
        self.role = role
        self.config = config
        self.log = log

    @override
    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        started = time.monotonic()
        response = await super().request(
            messages, model_settings, model_request_parameters
        )
        details = response.provider_details or {}
        if (cost := details.get("cost")) is not None:
            response.usage.cost = Decimal(str(cost))
        self.log.event(
            "model_response",
            role=self.role,
            model=self.config.model,
            provider=details.get("downstream_provider"),
            finish_reason=response.finish_reason,
            input_tokens=response.usage.input_tokens,
            cache_read_tokens=response.usage.cache_read_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=response.usage.cost,
            seconds=round(time.monotonic() - started, 2),
            tool_calls=[
                p.tool_name for p in response.parts if isinstance(p, ToolCallPart)
            ],
        )
        if response.finish_reason == "length":
            raise ThinkingExhaustedError(
                f"{self.role} model {self.config.model} used all {self.config.max_tokens} output "
                "tokens before finishing; raise max_tokens for this role or lower reasoning_effort"
            )
        return response

    def request_settings(self, timeout_s: float) -> OpenRouterModelSettings:
        settings = OpenRouterModelSettings(
            max_tokens=self.config.max_tokens,
            timeout=timeout_s,
            openrouter_provider={
                "order": self.config.providers,
                "allow_fallbacks": False,
            },
            openrouter_usage={"include": True},
            openrouter_cache_instructions=True,
            openrouter_cache_tool_definitions=True,
            openrouter_cache_messages=True,
            extra_body={"service_tier": "standard"},
        )
        if self.config.reasoning_effort is not None:
            settings["openrouter_reasoning"] = {"effort": self.config.reasoning_effort}
        return settings
