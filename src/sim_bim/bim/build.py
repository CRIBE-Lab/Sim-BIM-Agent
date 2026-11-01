"""Assemble compiled plans and elevations into one building."""

import statistics
from typing import Final

from shapely.geometry import Polygon

from sim_bim.bim.schema import (
    BuildingSchema,
    OpeningSchema,
    SpaceSchema,
    StoreySchema,
    WallSchema,
)
from sim_bim.drawing.elevation import (
    CompiledElevationSchema,
    align_openings,
    facade_openings,
)
from sim_bim.drawing.plan import CompiledPlanSchema

DEFAULT_STOREY_HEIGHT_M: Final = 3.0
DEFAULT_HEIGHTS_M: Final = {
    "door": (0.0, 2.1),
    "window": (0.9, 2.1),
    "opening": (0.0, 2.1),
}


def _storey_levels(
    storeys: list[str],
    elevations: list[CompiledElevationSchema],
    assumptions: list[str],
) -> dict[str, tuple[float, float]]:
    floors: dict[str, list[float]] = {}
    tops: dict[str, list[float]] = {}
    eaves: list[float] = []
    for elevation in elevations:
        for level in elevation.levels:
            if level.role == "floor" and level.storey:
                floors.setdefault(level.storey, []).append(level.height_m)
            elif level.role == "ceiling" and level.storey:
                tops.setdefault(level.storey, []).append(level.height_m)
            elif level.role == "eave":
                eaves.append(level.height_m)
    known = {name: statistics.median(values) for name, values in floors.items()}
    elevations_m: dict[str, float] = {}
    for index, name in enumerate(storeys):
        if name in known:
            elevations_m[name] = known[name]
        else:
            previous = elevations_m[storeys[index - 1]] if index else 0.0
            elevations_m[name] = previous + (DEFAULT_STOREY_HEIGHT_M if index else 0.0)
            assumptions.append(
                f"storey '{name}' has no floor level on any elevation; placed at {elevations_m[name]:.2f} m"
            )
    order = sorted(storeys, key=lambda name: elevations_m[name])
    result: dict[str, tuple[float, float]] = {}
    for index, name in enumerate(order):
        base = elevations_m[name]
        if index + 1 < len(order):
            height = elevations_m[order[index + 1]] - base
        elif name in tops:
            height = statistics.median(tops[name]) - base
        elif eaves:
            height = statistics.median(eaves) - base
        else:
            height = DEFAULT_STOREY_HEIGHT_M
            assumptions.append(f"top storey '{name}' height defaults to {height:.1f} m")
        result[name] = (base, height)
    return result


def build_from_drawings(
    name: str,
    plans: list[CompiledPlanSchema],
    elevations: list[CompiledElevationSchema],
    sources: list[str],
) -> BuildingSchema:
    """Combine per-storey plans with elevation heights into a building.

    Plans must share one datum, so their coordinates already align.
    """
    assumptions: list[str] = []
    unresolved: list[str] = []
    levels = _storey_levels(
        [plan.storey.casefold() for plan in plans], elevations, assumptions
    )
    order = sorted(levels, key=lambda storey: levels[storey][0])
    storey_ids = {storey: f"L{index + 1}" for index, storey in enumerate(order)}

    storeys = [
        StoreySchema(
            id=storey_ids[storey],
            name=storey,
            elevation_m=round(levels[storey][0], 4),
            height_m=round(levels[storey][1], 4),
        )
        for storey in order
    ]
    spaces: list[SpaceSchema] = []
    walls: list[WallSchema] = []
    openings: list[OpeningSchema] = []
    for plan in plans:
        storey = plan.storey.casefold()
        sid = storey_ids[storey]
        base, height = levels[storey]
        heights = _opening_heights(plan, storey, base, elevations, unresolved)
        spaces += [
            SpaceSchema(
                id=f"{sid}:{space.id}",
                storey_id=sid,
                name=space.name,
                polygon=space.polygon,
                area_m2=round(Polygon(space.polygon).area, 3),
            )
            for space in plan.spaces
        ]
        walls += [
            WallSchema(
                id=f"{sid}:{wall.id}",
                storey_id=sid,
                start=wall.start,
                end=wall.end,
                thickness_m=wall.thickness_m,
                height_m=round(height, 4),
                kind=wall.kind,
            )
            for wall in plan.walls
        ]
        for opening in plan.openings:
            if opening.id in heights:
                sill, head, source = heights[opening.id]
            else:
                sill, head = DEFAULT_HEIGHTS_M[opening.kind]
                source = "default"
                assumptions.append(
                    f"{sid}:{opening.id} uses default {opening.kind} sill {sill} m and head {head} m"
                )
            openings.append(
                OpeningSchema(
                    id=f"{sid}:{opening.id}",
                    storey_id=sid,
                    wall_id=f"{sid}:{opening.wall_id}",
                    kind=opening.kind,
                    offset_m=opening.offset_m,
                    width_m=opening.width_m,
                    sill_m=round(sill, 4),
                    head_m=round(head, 4),
                    height_source=source,
                )
            )
    return BuildingSchema(
        name=name,
        storeys=storeys,
        spaces=spaces,
        walls=walls,
        openings=openings,
        assumptions=assumptions,
        unresolved=unresolved,
        sources=sources,
    )


def _opening_heights(
    plan: CompiledPlanSchema,
    storey: str,
    base: float,
    elevations: list[CompiledElevationSchema],
    unresolved: list[str],
) -> dict[str, tuple[float, float, str]]:
    heights: dict[str, tuple[float, float, str]] = {}
    for elevation in elevations:
        on_storey = [o for o in elevation.openings if o.storey == storey]
        plan_side = facade_openings(plan, elevation.facing)
        if not on_storey and not plan_side:
            continue
        matches, plan_only, elevation_only = align_openings(plan_side, on_storey)
        by_id = {o.id: o for o in on_storey}
        for match in matches:
            seen = by_id[match.elevation_opening_id]
            heights[match.plan_opening_id] = (
                seen.sill_m - base,
                seen.head_m - base,
                f"{elevation.view_id}:{seen.id}",
            )
        unresolved += [
            f"{storey} {elevation.facing} facade: plan opening {item} has no match on {elevation.view_id}"
            for item in plan_only
        ]
        unresolved += [
            f"{storey} {elevation.facing} facade: {elevation.view_id} opening {item} has no match on plan {plan.view_id}"
            for item in elevation_only
        ]
    return heights
