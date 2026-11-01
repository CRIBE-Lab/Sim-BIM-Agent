"""Run directory: append-only event log, atomic state checkpoints and agent histories."""

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from sim_bim._share import BaseSchema, atomic_write
from sim_bim.drawing.readings import (
    ElevationReadingSchema,
    PlanReadingSchema,
    SheetLayoutSchema,
)


class ReaderStatusSchema(BaseSchema):
    state: Literal["pending", "done", "failed"] = "pending"
    detail: str = ""


class RunStateSchema(BaseSchema):
    sheets: dict[str, SheetLayoutSchema] = Field(default_factory=dict)
    datum: str | None = None
    storeys: list[str] = Field(default_factory=list)
    drafts: dict[str, PlanReadingSchema] = Field(default_factory=dict)
    plans: dict[str, PlanReadingSchema] = Field(default_factory=dict)
    elevations: dict[str, ElevationReadingSchema] = Field(default_factory=dict)
    readers: dict[str, ReaderStatusSchema] = Field(default_factory=dict)
    rereads: list[str] = Field(default_factory=list)
    cost_usd: Decimal = Decimal(0)
    finished: bool = False


class RunLog:
    """Owns the files of one run directory."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        self._events = (run_dir / "events.jsonl").open("a", encoding="utf-8")

    def close(self) -> None:
        self._events.close()

    def event(self, kind: str, **fields: Any) -> None:
        record = {"time": round(time.time(), 3), "kind": kind, **fields}
        self._events.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._events.flush()

    @property
    def state_path(self) -> Path:
        return self.run_dir / "state.json"

    def load_state(self) -> RunStateSchema:
        if not self.state_path.exists():
            return RunStateSchema()
        return RunStateSchema.model_validate_json(self.state_path.read_bytes())

    def save_state(self, state: RunStateSchema) -> None:
        atomic_write(self.state_path, state.model_dump_json(indent=1).encode())

    def history_path(self, agent: str) -> Path:
        return self.run_dir / "histories" / f"{agent}.json"

    def load_history(self, agent: str) -> list[ModelMessage]:
        path = self.history_path(agent)
        if not path.exists():
            return []
        return ModelMessagesTypeAdapter.validate_json(path.read_bytes())

    def save_history(self, agent: str, messages: list[ModelMessage]) -> None:
        atomic_write(
            self.history_path(agent), ModelMessagesTypeAdapter.dump_json(messages)
        )
