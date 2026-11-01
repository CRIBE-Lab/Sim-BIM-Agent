import pytest

from sim_bim.bim.build import build_from_drawings
from sim_bim.drawing.elevation import compile_elevation
from sim_bim.drawing.plan import compile_plan
from sim_bim.drawing.readings import (
    ElevationOpeningSchema,
    ElevationReadingSchema,
    LevelMarkSchema,
)
from tests.drawing.test_plan import two_room_plan


def south_elevation() -> ElevationReadingSchema:
    return ElevationReadingSchema(
        view_id="elev-south",
        facing="south",
        levels=[
            LevelMarkSchema(name="GRADE", role="grade", y_px=1000.0, label="0"),
            LevelMarkSchema(
                name="FIRST FLOOR",
                role="floor",
                storey="first floor",
                y_px=950.0,
                label="500",
            ),
            LevelMarkSchema(name="EAVE", role="eave", y_px=650.0, label="3500"),
        ],
        facade_left_px=100.0,
        facade_right_px=900.0,
        openings=[
            ElevationOpeningSchema(
                id="w-south",
                kind="window",
                storey="first floor",
                left_px=240.0,
                right_px=360.0,
                bottom_px=860.0,
                top_px=740.0,
            ),
            ElevationOpeningSchema(
                id="w-extra",
                kind="window",
                storey="first floor",
                left_px=700.0,
                right_px=800.0,
                bottom_px=860.0,
                top_px=740.0,
            ),
        ],
    )


def test_build_takes_heights_from_matched_elevation_openings() -> None:
    building = build_from_drawings(
        "two rooms",
        [compile_plan(two_room_plan())],
        [compile_elevation(south_elevation())],
        sources=["synthetic"],
    )

    assert [(s.name, s.elevation_m, s.height_m) for s in building.storeys] == [
        ("first floor", pytest.approx(0.5), pytest.approx(3.0))
    ]
    window = next(o for o in building.openings if o.id == "L1:w1")
    assert (window.sill_m, window.head_m) == pytest.approx((0.9, 2.1))
    assert window.height_source == "elev-south:w-south"
    door = next(o for o in building.openings if o.id == "L1:d1")
    assert door.height_source == "default"
    assert any("w-extra has no match" in item for item in building.unresolved)
    assert sorted(s.area_m2 for s in building.spaces) == pytest.approx(
        [20.0, 20.0], abs=0.3
    )
