"""Shared run session: models, budget, persistence and the agent runner."""

from dataclasses import dataclass, field
from decimal import Decimal
from functools import partial
from typing import Any, Self

from loguru import logger
from PIL import Image
from pydantic_ai import Agent, BinaryContent
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

from sim_bim.agents.llm import LoggedOpenRouterModel
from sim_bim.agents.runlog import RunLog, RunStateSchema
from sim_bim.config import SimBimSettings
from sim_bim.drawing.readings import ViewBoxSchema


def trim_images(messages: list[ModelMessage], max_images: int) -> list[ModelMessage]:
    """Keep the first request's images and the latest others up to ``max_images``.

    Older images become text. Replaced images stay replaced, so the cached
    prompt prefix only grows.
    """
    first = messages[0] if messages else None
    first_images = sum(
        isinstance(item, BinaryContent) and item.is_image
        for part in (first.parts if isinstance(first, ModelRequest) else [])
        if isinstance(part, UserPromptPart) and isinstance(part.content, list)
        for item in part.content
    )
    keep = max(0, max_images - first_images)
    carriers: list[tuple[UserPromptPart | ToolReturnPart, int]] = []
    for message in messages[1:]:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, UserPromptPart | ToolReturnPart):
                continue
            items = part.content
            if isinstance(items, list):
                carriers += [
                    (part, index)
                    for index, item in enumerate(items)
                    if isinstance(item, BinaryContent) and item.is_image
                ]
    for part, index in carriers[: len(carriers) - keep]:
        current = part.content
        if isinstance(current, list):
            replaced = list(current)
            replaced[index] = "[earlier image removed to save context]"
            part.content = replaced
    return messages


@dataclass(frozen=True)
class RoleModel:
    model: Model
    settings: ModelSettings
    max_images: int


def _role(model: LoggedOpenRouterModel, timeout_s: float) -> RoleModel:
    return RoleModel(
        model=model,
        settings=model.request_settings(timeout_s),
        max_images=model.config.max_images,
    )


@dataclass
class Session:
    settings: SimBimSettings
    log: RunLog
    state: RunStateSchema
    sheets: dict[str, Image.Image]
    brief: str
    coordinator_model: RoleModel
    reader_model: RoleModel
    usage: RunUsage = field(default_factory=RunUsage)
    prior_cost_usd: Decimal = Decimal(0)
    views: dict[str, tuple[str, ViewBoxSchema]] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        settings: SimBimSettings,
        log: RunLog,
        sheets: dict[str, Image.Image],
        brief: str,
    ) -> Self:
        key = settings.openrouter_api_key.get_secret_value()
        state = log.load_state()
        return cls(
            settings=settings,
            log=log,
            state=state,
            prior_cost_usd=state.cost_usd,
            sheets=sheets,
            brief=brief,
            coordinator_model=_role(
                LoggedOpenRouterModel("coordinator", settings.coordinator, key, log),
                settings.request_timeout_s,
            ),
            reader_model=_role(
                LoggedOpenRouterModel("reader", settings.reader, key, log),
                settings.request_timeout_s,
            ),
        )

    def spent(self) -> Decimal:
        return self.prior_cost_usd + (self.usage.cost or Decimal(0))

    def save(self) -> None:
        self.state.cost_usd = self.spent()
        self.log.save_state(self.state)

    async def run_agent[OutputT](
        self,
        agent: Agent[Any, OutputT],
        name: str,
        prompt: list[str | BinaryContent],
        deps: object,
        *,
        model: RoleModel,
    ) -> OutputT:
        """Run ``agent`` to completion, saving its history after every step.

        A saved history for ``name`` is resumed instead of sending ``prompt``
        again. All agents share one usage record, so the cost limit covers
        concurrent readers together.

        Raises:
            pydantic_ai.UsageLimitExceeded: If the run budget or request limit is spent.
            pydantic_ai.UnexpectedModelBehavior: If the model keeps failing validation.
        """
        history = self.log.load_history(name)
        logger.info("{} {}", "resuming" if history else "starting", name)
        before = self.usage.cost or Decimal(0)
        async with agent.iter(
            None if history else prompt,
            message_history=history or None,
            deps=deps,
            model=model.model,
            model_settings=model.settings,
            usage=self.usage,
            usage_limits=UsageLimits(
                cost_limit=self.settings.cost_limit_usd - self.prior_cost_usd,
                request_limit=self.settings.request_limit,
            ),
            retries={"output": self.settings.output_retries, "tools": 3},
            capabilities=[
                ProcessHistory(partial(trim_images, max_images=model.max_images))
            ],
        ) as run:
            async for _ in run:
                self.log.save_history(name, run.all_messages())
            result = run.result
        if result is None:
            raise RuntimeError(f"{name} ended without a result")
        self.save()
        self.log.event(
            "agent_done", agent=name, cost_usd=(self.usage.cost or Decimal(0)) - before
        )
        return result.output
