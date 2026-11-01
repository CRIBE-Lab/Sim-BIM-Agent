"""Offline example: a two-room house from hand-written readings, no model calls."""

from sim_bim.bim.build import build_from_drawings
from sim_bim.bim.schema import BuildingSchema
from sim_bim.drawing.elevation import compile_elevation
from sim_bim.drawing.plan import compile_plan
from sim_bim.drawing.readings import (
    DimensionChainSchema,
    ElevationOpeningSchema,
    ElevationReadingSchema,
    LevelMarkSchema,
    PlanOpeningSchema,
    PlanReadingSchema,
    RoomSchema,
    WallSchema,
)


def _px(x_m: float, y_m: float) -> tuple[float, float]:
    return (50.0 + x_m * 100.0, 550.0 - y_m * 100.0)


def demo_building() -> BuildingSchema:
    plan = PlanReadingSchema(
        view_id="plan-l1",
        storey="ground floor",
        chains=[
            DimensionChainSchema(
                axis="x", ticks_px=[50.0, 450.0, 850.0], labels=["4000", "4000"]
            ),
            DimensionChainSchema(axis="y", ticks_px=[550.0, 50.0], labels=["5000"]),
        ],
        datum_px=_px(0, 0),
        walls=[
            WallSchema(id="s", start_px=_px(0, 0), end_px=_px(8, 0)),
            WallSchema(id="e", start_px=_px(8, 0), end_px=_px(8, 5)),
            WallSchema(id="n", start_px=_px(8, 5), end_px=_px(0, 5)),
            WallSchema(id="w", start_px=_px(0, 5), end_px=_px(0, 0)),
            WallSchema(id="p", start_px=_px(4, 0), end_px=_px(4, 5), thickness_m=0.1),
        ],
        openings=[
            PlanOpeningSchema(
                id="d1", kind="door", wall_id="s", center_px=_px(6, 0), width_m=1.0
            ),
            PlanOpeningSchema(
                id="d2", kind="door", wall_id="p", center_px=_px(4, 2.5), width_m=0.9
            ),
            PlanOpeningSchema(
                id="w1", kind="window", wall_id="s", center_px=_px(2, 0), width_m=1.5
            ),
            PlanOpeningSchema(
                id="w2", kind="window", wall_id="n", center_px=_px(6, 5), width_m=1.2
            ),
        ],
        rooms=[
            RoomSchema(id="r1", name="living room", seed_px=_px(2, 2.5)),
            RoomSchema(id="r2", name="bedroom", seed_px=_px(6, 2.5)),
        ],
    )
    elevation = ElevationReadingSchema(
        view_id="elev-south",
        facing="south",
        levels=[
            LevelMarkSchema(name="GRADE", role="grade", y_px=1000.0, label="0"),
            LevelMarkSchema(
                name="GROUND FLOOR",
                role="floor",
                storey="ground floor",
                y_px=970.0,
                label="300",
            ),
            LevelMarkSchema(name="EAVE", role="eave", y_px=670.0, label="3300"),
        ],
        facade_left_px=100.0,
        facade_right_px=900.0,
        openings=[
            ElevationOpeningSchema(
                id="sw1",
                kind="window",
                storey="ground floor",
                left_px=225.0,
                right_px=375.0,
                bottom_px=880.0,
                top_px=760.0,
            ),
            ElevationOpeningSchema(
                id="sd1",
                kind="door",
                storey="ground floor",
                left_px=650.0,
                right_px=750.0,
                bottom_px=970.0,
                top_px=760.0,
            ),
        ],
    )
    return build_from_drawings(
        "Two-room demo house",
        [compile_plan(plan)],
        [compile_elevation(elevation, None)],
        sources=["demo"],
    )
