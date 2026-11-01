"""Elevation compilation and facade-ordered matching of openings to plan walls."""

from dataclasses import dataclass
from typing import Final, Literal

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from sim_bim._share import BaseSchema
from sim_bim.drawing.issues import CompileError, Issue
from sim_bim.drawing.plan import CompiledPlanSchema
from sim_bim.drawing.readings import ElevationReadingSchema, Facing
from sim_bim.drawing.units import fit_levels, height_m

LEVEL_RESIDUAL_M: Final = 0.08
GAP_COST_M: Final = 1.0
MATCH_LIMIT_M: Final = 0.6
SIDE_PROBE_M: Final = 0.3


class LevelSchema(BaseSchema):
    name: str
    role: Literal["grade", "floor", "ceiling", "eave", "ridge", "other"]
    storey: str | None
    height_m: float


class FacadeOpeningSchema(BaseSchema):
    id: str
    kind: Literal["door", "window", "opening"]
    storey: str
    u_m: float
    width_m: float
    sill_m: float
    head_m: float


class CompiledElevationSchema(BaseSchema):
    view_id: str
    facing: Facing
    px_per_m: float
    facade_width_m: float
    levels: list[LevelSchema]
    openings: list[FacadeOpeningSchema]


def compile_elevation(reading: ElevationReadingSchema) -> CompiledElevationSchema:
    """Convert an elevation reading to heights above grade and facade positions.

    Horizontal positions use the vertical scale, since measured drawings are
    drawn at one scale in both directions.

    Raises:
        CompileError: If the level marks do not fit one scale or an
            opening is inverted.
    """
    issues: list[Issue] = []
    try:
        scale = fit_levels(
            [m.y_px for m in reading.levels], [m.label for m in reading.levels]
        )
    except ValueError as error:
        raise CompileError([Issue("levels", str(error))]) from error
    if scale.residual_m > LEVEL_RESIDUAL_M:
        issues.append(
            Issue(
                "levels",
                f"level marks disagree with their labels by up to {scale.residual_m:.3f} m; "
                "re-measure y_px or re-read the labels",
            )
        )
    left = min(reading.facade_left_px, reading.facade_right_px)
    openings: list[FacadeOpeningSchema] = []
    for opening in reading.openings:
        if opening.right_px <= opening.left_px or opening.bottom_px <= opening.top_px:
            issues.append(Issue(opening.id, "left/right or top/bottom are swapped"))
            continue
        openings.append(
            FacadeOpeningSchema(
                id=opening.id,
                kind=opening.kind,
                storey=opening.storey.casefold(),
                u_m=((opening.left_px + opening.right_px) / 2 - left) / scale.px_per_m,
                width_m=(opening.right_px - opening.left_px) / scale.px_per_m,
                sill_m=height_m(scale, opening.bottom_px),
                head_m=height_m(scale, opening.top_px),
            )
        )
    if issues:
        raise CompileError(issues)
    return CompiledElevationSchema(
        view_id=reading.view_id,
        facing=reading.facing,
        px_per_m=scale.px_per_m,
        facade_width_m=abs(reading.facade_right_px - reading.facade_left_px)
        / scale.px_per_m,
        levels=[
            LevelSchema(
                name=mark.name,
                role=mark.role,
                storey=mark.storey.casefold() if mark.storey else None,
                height_m=height_m(scale, mark.y_px),
            )
            for mark in reading.levels
        ],
        openings=openings,
    )


@dataclass(frozen=True)
class PlanFacadeOpening:
    opening_id: str
    wall_id: str
    u_m: float
    width_m: float


_OUTWARD: Final[dict[Facing, tuple[float, float]]] = {
    "south": (0.0, -1.0),
    "north": (0.0, 1.0),
    "east": (1.0, 0.0),
    "west": (-1.0, 0.0),
}


def facade_openings(
    plan: CompiledPlanSchema, facing: Facing
) -> list[PlanFacadeOpening]:
    """Plan openings on exterior walls that face ``facing``, in viewer left-to-right order.

    The facade coordinate ``u`` is measured from the building's leftmost extent
    as seen by a viewer standing outside that side.
    """
    footprint = unary_union([Polygon(space.polygon) for space in plan.spaces])
    dx, dy = _OUTWARD[facing]
    min_x, min_y, max_x, max_y = footprint.bounds
    walls = {wall.id: wall for wall in plan.walls}
    rows: list[PlanFacadeOpening] = []
    for opening in plan.openings:
        wall = walls[opening.wall_id]
        line = LineString([wall.start, wall.end])
        centre = line.interpolate(opening.offset_m)
        outside = Point(centre.x + dx * SIDE_PROBE_M, centre.y + dy * SIDE_PROBE_M)
        inside = Point(centre.x - dx * SIDE_PROBE_M, centre.y - dy * SIDE_PROBE_M)
        if footprint.contains(outside) or not footprint.contains(inside):
            continue
        u = {
            "south": centre.x - min_x,
            "north": max_x - centre.x,
            "east": centre.y - min_y,
            "west": max_y - centre.y,
        }[facing]
        rows.append(PlanFacadeOpening(opening.id, wall.id, u, opening.width_m))
    return sorted(rows, key=lambda row: row.u_m)


@dataclass(frozen=True)
class FacadeMatch:
    plan_opening_id: str
    elevation_opening_id: str
    offset_m: float


def align_openings(
    plan_side: list[PlanFacadeOpening], elevation_side: list[FacadeOpeningSchema]
) -> tuple[list[FacadeMatch], list[str], list[str]]:
    """Order-preserving alignment of plan and elevation openings on one facade.

    Returns matches, unmatched plan opening ids and unmatched elevation opening ids.
    Pairs further apart than ``MATCH_LIMIT_M`` along the facade are never matched.
    """
    elevation_side = sorted(elevation_side, key=lambda row: row.u_m)
    n, m = len(plan_side), len(elevation_side)
    cost = [[0.0] * (m + 1) for _ in range(n + 1)]
    step = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        cost[i][0], step[i][0] = i * GAP_COST_M, "plan"
    for j in range(1, m + 1):
        cost[0][j], step[0][j] = j * GAP_COST_M, "elev"
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            a, b = plan_side[i - 1], elevation_side[j - 1]
            distance = abs(a.u_m - b.u_m) + abs(a.width_m - b.width_m)
            options = [
                (cost[i - 1][j] + GAP_COST_M, "plan"),
                (cost[i][j - 1] + GAP_COST_M, "elev"),
            ]
            if abs(a.u_m - b.u_m) <= MATCH_LIMIT_M:
                options.append((cost[i - 1][j - 1] + distance, "match"))
            cost[i][j], step[i][j] = min(options)
    matches: list[FacadeMatch] = []
    plan_only: list[str] = []
    elevation_only: list[str] = []
    i, j = n, m
    while i or j:
        move = step[i][j]
        if move == "match":
            a, b = plan_side[i - 1], elevation_side[j - 1]
            matches.append(FacadeMatch(a.opening_id, b.id, b.u_m - a.u_m))
            i, j = i - 1, j - 1
        elif move == "plan":
            plan_only.append(plan_side[i - 1].opening_id)
            i -= 1
        else:
            elevation_only.append(elevation_side[j - 1].id)
            j -= 1
    return matches[::-1], plan_only[::-1], elevation_only[::-1]
