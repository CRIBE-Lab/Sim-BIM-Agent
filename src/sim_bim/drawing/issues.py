from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Issue:
    """A problem with one object of a reading, phrased for the reader to fix."""

    object_id: str
    message: str
    severity: Literal["error", "warning"] = "error"

    def __str__(self) -> str:
        return f"[{self.object_id}] {self.message}"


class CompileError(ValueError):
    """A reading that cannot be compiled; ``issues`` name the objects to change."""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        super().__init__("\n".join(str(issue) for issue in issues))
