"""Lightweight BIM: storeys, spaces, walls and openings in metres, z up."""

from typing import Literal

from pydantic import Field

from sim_bim._share import BaseSchema, Point2


class StoreySchema(BaseSchema):
    id: str
    name: str
    elevation_m: float
    height_m: float = Field(gt=0)


class SpaceSchema(BaseSchema):
    id: str
    storey_id: str
    name: str
    polygon: list[Point2]
    area_m2: float


class WallSchema(BaseSchema):
    id: str
    storey_id: str
    start: Point2
    end: Point2
    thickness_m: float
    height_m: float
    kind: Literal["wall", "open_edge"] = "wall"


class OpeningSchema(BaseSchema):
    id: str
    storey_id: str
    wall_id: str
    kind: Literal["door", "window", "opening"]
    offset_m: float = Field(
        description="Centre position along the wall from its start."
    )
    width_m: float
    sill_m: float = Field(description="Bottom above the storey floor.")
    head_m: float = Field(description="Top above the storey floor.")
    height_source: str


class BuildingSchema(BaseSchema):
    schema_version: Literal[2] = 2
    name: str
    storeys: list[StoreySchema]
    spaces: list[SpaceSchema]
    walls: list[WallSchema]
    openings: list[OpeningSchema]
    assumptions: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
