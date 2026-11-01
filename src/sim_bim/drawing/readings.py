"""Structured outputs that reader agents return for one drawing view.

All pixel coordinates are in the cropped view image the reader was given:
x grows to the right, y grows downwards, origin at the top-left corner.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from sim_bim._share import BaseSchema, Point2

type Facing = Literal["north", "south", "east", "west"]
type OpeningKind = Literal["door", "window", "opening"]


class LayoutViewSchema(BaseSchema):
    view_id: str = Field(description="Short unique id, e.g. 'plan-l1' or 'elev-south'.")
    kind: Literal["plan", "elevation"]
    title: str = Field(description="Title printed under the view, e.g. 'FIRST FLOOR'.")
    storey: str | None = Field(
        default=None, description="Plans only: storey name, e.g. 'first floor'."
    )
    facing: Facing | None = Field(
        default=None,
        description="Elevations only: the compass side of the building the view shows.",
    )
    regions: list[int] = Field(
        min_length=1,
        description="Numbers of all boxed regions that belong to this view, including its "
        "dimension chains and level marks.",
    )


class SheetLayoutSchema(BaseSchema):
    views: list[LayoutViewSchema]
    north_up_in_plans: bool = Field(
        description="True when plan views are drawn with north at the top of the sheet."
    )


class ViewBoxSchema(LayoutViewSchema):
    box_px: tuple[int, int, int, int]


class DimensionChainSchema(BaseSchema):
    axis: Literal["x", "y"] = Field(
        description="'x' for a horizontal chain, 'y' for a vertical chain."
    )
    ticks_px: list[float] = Field(
        description="Pixel positions of the chain's tick marks along the axis, in order."
    )
    labels: list[str] = Field(
        description='Segment labels between consecutive ticks exactly as printed, e.g. "7\'-9 1/2\\"".'
    )


class WallSchema(BaseSchema):
    id: str
    start_px: Point2
    end_px: Point2
    thickness_m: float = Field(default=0.15, gt=0, le=1.0)
    kind: Literal["wall", "open_edge"] = Field(
        default="wall",
        description="'open_edge' bounds a space without a wall, e.g. the open side of a porch.",
    )


class PlanOpeningSchema(BaseSchema):
    id: str
    kind: OpeningKind
    wall_id: str
    center_px: Point2 = Field(
        description="Centre of the opening on the wall centreline."
    )
    width_m: float = Field(gt=0, le=6.0)


class RoomSchema(BaseSchema):
    id: str
    name: str
    seed_px: Point2 = Field(description="A point clearly inside the room.")


class PlanReadingSchema(BaseSchema):
    view_id: str
    storey: str
    chains: list[DimensionChainSchema] = Field(
        description="At least one horizontal and one vertical chain."
    )
    datum_px: Point2 = Field(
        description="Pixel of the shared datum point named in the task; aligns storeys."
    )
    walls: list[WallSchema]
    openings: list[PlanOpeningSchema]
    rooms: list[RoomSchema]
    unresolved: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [
            item.id
            for group in (self.walls, self.openings, self.rooms)
            for item in group
        ]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(
                f"ids must be unique across walls, openings and rooms: {duplicates}"
            )
        return self


class LevelMarkSchema(BaseSchema):
    name: str = Field(
        description="Level name as printed, e.g. 'SECOND FLOOR' or 'GRADE BEAM'."
    )
    role: Literal["grade", "floor", "ceiling", "eave", "ridge", "other"]
    storey: str | None = Field(
        default=None,
        description="For 'floor' and 'ceiling' marks: the storey they belong to.",
    )
    y_px: float
    label: str | None = Field(
        default=None,
        description='Height label as printed, e.g. "12\'-7\\""; null when no height is printed.',
    )


class ElevationOpeningSchema(BaseSchema):
    id: str
    kind: OpeningKind
    storey: str
    left_px: float
    right_px: float
    bottom_px: float
    top_px: float


class ElevationReadingSchema(BaseSchema):
    view_id: str
    facing: Facing
    levels: list[LevelMarkSchema] = Field(min_length=2)
    facade_left_px: float = Field(description="Outer face of the leftmost main wall.")
    facade_right_px: float = Field(description="Outer face of the rightmost main wall.")
    openings: list[ElevationOpeningSchema]
    unresolved: list[str] = Field(default_factory=list)


class WallMoveSchema(BaseSchema):
    op: Literal["move_wall"]
    wall_id: str
    start_px: Point2
    end_px: Point2


class RemoveSchema(BaseSchema):
    op: Literal["remove"]
    id: str


class UpsertWallSchema(BaseSchema):
    op: Literal["upsert_wall"]
    wall: WallSchema


class UpsertOpeningSchema(BaseSchema):
    op: Literal["upsert_opening"]
    opening: PlanOpeningSchema


class UpsertRoomSchema(BaseSchema):
    op: Literal["upsert_room"]
    room: RoomSchema


type PlanEdit = Annotated[
    WallMoveSchema
    | RemoveSchema
    | UpsertWallSchema
    | UpsertOpeningSchema
    | UpsertRoomSchema,
    Field(discriminator="op"),
]


class PlanPatchSchema(BaseSchema):
    edits: list[PlanEdit]
