"""Building geometry input records and materialized source BIM objects."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import AllowInfNan, BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator
from src.agent.geometry_model.constants import SCHEMA_VERSION_V2

FiniteFloat = Annotated[float, AllowInfNan(False)]
Point2 = tuple[FiniteFloat, FiniteFloat]

_FACADE_ALIASES = {
    "north": "North", "n": "North", "south": "South", "s": "South",
    "east": "East", "e": "East", "west": "West", "w": "West",
}

class Cell(BaseModel):
    """One source room footprint (world meters), never a display/thermal slice.

    ``id`` is the persistent source-space identity. Public Z handles are derived
    and can change when geometry ordering changes. Preserve this id on edits.
    """

    model_config = ConfigDict(extra="allow")
    id: str
    role: str = "office"
    x: list[float]  # [min, max]
    y: list[float]  # [min, max]
    polygon: list[list[float]] | None = None  # exterior ring, CCW, not closed


class RoomRoleEvidence(BaseModel):
    """Current function assignment basis, separate from room geometry evidence."""

    model_config = ConfigDict(extra="forbid")
    role: str
    basis: Literal["observed", "inferred", "unknown"]
    source_refs: list[str] = Field(min_length=1)
    assumptions: list[str]

    @model_validator(mode="after")
    def validate_assignment(self):
        from src.agent.roles import require_role
        self.role = require_role(self.role)
        if any(not ref.strip() for ref in self.source_refs):
            raise ValueError("room role evidence requires nonblank source_refs")
        if (self.role == "unknown") != (self.basis == "unknown"):
            raise ValueError("unknown role and unknown evidence basis must be used together")
        if self.basis == "inferred" and not any(note.strip() for note in self.assumptions):
            raise ValueError("inferred room role requires an explicit assumption")
        return self


class SourceVerticalSection(BaseModel):
    """One horizontal slab of a single source space, using absolute world Z."""

    model_config = ConfigDict(extra="forbid")
    z: tuple[Annotated[float, AllowInfNan(False)], Annotated[float, AllowInfNan(False)]]
    polygon: list[list[Annotated[float, AllowInfNan(False)]]] = Field(min_length=3)
    holes: list[list[list[Annotated[float, AllowInfNan(False)]]]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_section(self):
        from src.agent.geometry.source_volume import validate_section_polygon
        if self.z[1] <= self.z[0]:
            raise ValueError("vertical section height must be positive")
        validate_section_polygon(self)
        return self


class SourceSpace(BaseModel):
    """Materialized source identity; independent of downstream zone numbering."""

    model_config = ConfigDict(extra="forbid")
    id: str
    floor_id: str
    polygon: list[list[float]]
    holes: list[list[list[float]]] = Field(default_factory=list, exclude_if=lambda value: not value)
    vertical_sections: list[SourceVerticalSection] = Field(default_factory=list, exclude_if=lambda value: not value)
    z_floor: float
    height: float
    role: str
    source_refs: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    role_evidence: RoomRoleEvidence | None = None

    @model_validator(mode="after")
    def role_matches_evidence(self):
        if self.vertical_sections:
            from src.agent.geometry.source_volume import validate_vertical_sections
            rows = validate_vertical_sections(self.vertical_sections, self.polygon, self.holes, self.z_floor, self.height)
            self.vertical_sections = [SourceVerticalSection.model_validate(row) for row in rows]
        if self.role_evidence is not None:
            from src.agent.roles import require_role
            if require_role(self.role) != self.role_evidence.role:
                raise ValueError("source room role does not match its saved role evidence")
        return self


class SourceBoundary(BaseModel):
    """A source room's oriented boundary, possibly split into several faces.

    Adjacent spaces and counterpart boundaries are derived relations, not a
    claim that adjacency is traversable. A long wall can border several rooms.
    """

    model_config = ConfigDict(extra="forbid")
    id: str
    space_id: str
    kind: Literal["physical", "virtual"]
    geometry_type: Literal["wall", "floor", "ceiling"]
    vertices: list[list[float]]
    holes: list[list[list[float]]] = Field(default_factory=list, exclude_if=lambda value: not value)
    adjacent_space_ids: list[str] = Field(default_factory=list)
    counterpart_ids: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)


class SourceOpening(BaseModel):
    """An identified opening and its source host, without door-leaf solids."""

    model_config = ConfigDict(extra="forbid")
    id: str
    kind: Literal["window", "door", "open"]
    host_boundary_id: str
    space_ids: list[str]
    exterior: bool
    vertices: list[list[float]]
    connectivity: Literal["unknown", "open", "closed"] = "unknown"
    source_refs: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class Window(BaseModel):
    """One window, positioned on a facade in world coordinates."""

    model_config = ConfigDict(extra="allow")
    id: str
    floor: str
    facade: Literal["North", "South", "East", "West"]
    span: list[float]  # along-facade world range [min, max] (x for N/S, y for E/W)
    z: list[float]  # [sill, head] world
    room: str | None = None  # cell id this window belongs to

    @field_validator("facade", mode="before")
    @classmethod
    def _normalize_facade(cls, v):
        if isinstance(v, str):
            return _FACADE_ALIASES.get(v.strip().lower(), v)
        return v


class Floor(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    z_floor: float
    ceiling_height: float
    cells: list[Cell]
    # Storey membership for existing continuous source spaces; no copies or slabs.
    spanning_space_ids: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize_floor(self, handler):
        # Omit empty membership lists from saved proposals.
        result = handler(self)
        if not self.spanning_space_ids:
            result.pop("spanning_space_ids", None)
        return result


class WallOpening(BaseModel):
    """An explicit door or empty aperture on a source room's wall (metres).

    ``other_space_id=None`` means outdoors, never an unspecified neighbour.
    The plan endpoints and world z interval define a rectangle, with no door-leaf
    solids. Door operating state may be unknown; an unfilled passage is open.
    """

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    kind: Literal["door", "open"]
    space_id: str
    other_space_id: str | None
    p1: tuple[Annotated[float, AllowInfNan(False)], Annotated[float, AllowInfNan(False)]]
    p2: tuple[Annotated[float, AllowInfNan(False)], Annotated[float, AllowInfNan(False)]]
    z: tuple[Annotated[float, AllowInfNan(False)], Annotated[float, AllowInfNan(False)]]
    state: Literal["unknown", "open", "closed"] | None = None
    source_refs: list[str] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _opening_contract(self):
        if self.space_id == self.other_space_id:
            raise ValueError("wall opening must connect distinct spaces or outdoors")
        if self.p1 == self.p2 or not self.z[0] < self.z[1]:
            raise ValueError("wall opening requires positive width and height")
        if self.p1[0] != self.p2[0] and self.p1[1] != self.p2[1]:
            raise ValueError("only orthogonal wall openings are currently supported")
        if self.state is None:
            self.state = "open" if self.kind == "open" else "unknown"
        if self.kind == "open" and self.state != "open":
            raise ValueError("an unfilled opening cannot be declared closed or unknown")
        if not all(ref.strip() for ref in self.source_refs):
            raise ValueError("opening source references must not be blank")
        return self


class VerticalSeparation(BaseModel):
    """Explicit physical separation between selected adjacent source spaces."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    space_ids: list[str] = Field(min_length=2)
    reason: str = Field(min_length=1)
    source_refs: list[str] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _separation_contract(self):
        if (not self.id.strip() or not self.reason.strip()
                or any(not value.strip() for value in self.space_ids + self.source_refs)):
            raise ValueError("vertical separation IDs, reason and source_refs must be nonblank")
        if len(set(self.space_ids)) != len(self.space_ids):
            raise ValueError("vertical separation space_ids must be unique")
        return self


class GeometryInput(BaseModel):
    """Orthogonal floor plans in world metres, with explicit openings and edit history."""

    model_config = ConfigDict(extra="allow")
    schema_version: Literal["2"] = SCHEMA_VERSION_V2
    footprint_x: list[float]  # [min, max]
    footprint_y: list[float]  # [min, max]
    floors: list[Floor]
    windows: list[Window] = Field(default_factory=list)
    # Empty optional relations are omitted from saved input records.
    openings: list[WallOpening] = Field(default_factory=list, exclude_if=lambda value: not value)
    vertical_separations: list[VerticalSeparation] = Field(default_factory=list, exclude_if=lambda value: not value)
    # Explicit proposal edits and unresolved observations; no automatic correction.
    corrections: list[dict] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    unsupported: list[dict] = Field(default_factory=list)
    notes: str | None = None


class FootprintRing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vertices: list[Point2] = Field(min_length=4)
