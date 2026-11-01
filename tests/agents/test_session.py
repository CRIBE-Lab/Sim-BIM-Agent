import asyncio
from pathlib import Path

import pytest
from PIL import Image
from pydantic import SecretStr
from pydantic_ai import BinaryContent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from sim_bim.agents.reader import PlanDeps, plan_agent
from sim_bim.agents.runlog import RunLog
from sim_bim.agents.session import RoleModel, Session
from sim_bim.config import SimBimSettings
from sim_bim.drawing.readings import PlanReadingSchema
from tests.drawing.test_plan import two_room_plan


class CrashError(RuntimeError):
    pass


def settings() -> SimBimSettings:
    return SimBimSettings(openrouter_api_key=SecretStr("unused"))


def session(run_dir: Path, model: FunctionModel) -> Session:
    role = RoleModel(model=model, settings={})
    log = RunLog(run_dir)
    return Session(
        settings=settings(),
        log=log,
        state=log.load_state(),
        sheets={},
        brief="",
        coordinator_model=role,
        reader_model=role,
    )


def plan_deps(drafts: dict[str, PlanReadingSchema]) -> PlanDeps:
    return PlanDeps(
        view=Image.new("L", (900, 600), 255),
        view_id="plan-l1",
        storey="first floor",
        save_draft=lambda reading: drafts.__setitem__("plan-l1", reading),
        reading=drafts.get("plan-l1"),
    )


def tool_returns(messages: list[ModelMessage]) -> list[str]:
    return [
        part.tool_name
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


def finish(info: AgentInfo) -> ModelResponse:
    return ModelResponse(
        parts=[ToolCallPart(info.output_tools[0].name, {"notes": "done"})]
    )


def test_plan_reader_compiles_its_reading_and_saves_history(tmp_path: Path) -> None:
    reading = two_room_plan().model_dump(mode="json")

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not tool_returns(messages):
            return ModelResponse(
                parts=[ToolCallPart("set_reading", {"reading": reading})]
            )
        return finish(info)

    run = session(tmp_path, FunctionModel(model))
    deps = plan_deps({})
    done = asyncio.run(
        run.run_agent(
            plan_agent,
            "reader-plan-l1",
            ["Read it.", BinaryContent(b"x", media_type="image/png")],
            deps,
            model=run.reader_model,
        )
    )

    assert done.notes == "done"
    assert deps.compiled is not None
    assert {space.name for space in deps.compiled.spaces} == {"living", "bedroom"}
    history = run.log.load_history("reader-plan-l1")
    assert "set_reading" in tool_returns(history)


def test_resume_continues_from_the_saved_history_and_draft(tmp_path: Path) -> None:
    reading = two_room_plan().model_dump(mode="json")
    drafts: dict[str, PlanReadingSchema] = {}

    def crashing(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not tool_returns(messages):
            return ModelResponse(
                parts=[ToolCallPart("set_reading", {"reading": reading})]
            )
        raise CrashError("network lost")

    first = session(tmp_path, FunctionModel(crashing))
    with pytest.raises(CrashError):
        asyncio.run(
            first.run_agent(
                plan_agent,
                "reader-plan-l1",
                ["Read it."],
                plan_deps(drafts),
                model=first.reader_model,
            )
        )

    seen: list[list[str]] = []

    def resumed(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(tool_returns(messages))
        return finish(info)

    second = session(tmp_path, FunctionModel(resumed))
    deps = plan_deps(drafts)
    done = asyncio.run(
        second.run_agent(
            plan_agent, "reader-plan-l1", ["Read it."], deps, model=second.reader_model
        )
    )

    assert done.notes == "done"
    assert seen == [["set_reading"]]
    assert deps.compiled is not None
