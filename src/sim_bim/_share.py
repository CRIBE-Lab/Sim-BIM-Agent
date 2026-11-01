import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict

type Point2 = tuple[float, float]


class BaseSchema(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        validate_assignment=True,
        str_strip_whitespace=True,
        populate_by_name=True,
        extra="forbid",
    )


def atomic_write(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
