import pytest

from sim_bim.drawing.elevation import (
    FacadeOpeningSchema,
    PlanFacadeOpening,
    align_openings,
    compile_elevation,
    facade_openings,
)
from sim_bim.drawing.issues import CompileError
from sim_bim.drawing.plan import compile_plan
from sim_bim.drawing.readings import (
    ElevationOpeningSchema,
    ElevationReadingSchema,
    LevelMarkSchema,
    RoomSchema,
    WallSchema,
)
from tests.drawing.test_plan import px, two_room_plan


def facade(u: float) -> FacadeOpeningSchema:
    return FacadeOpeningSchema(
        id=f"e{u:g}",
        kind="window",
        storey="l1",
        u_m=u,
        width_m=1.0,
        sill_m=0.9,
        head_m=2.1,
    )


def test_alignment_survives_a_shifted_sequence_of_equal_length() -> None:
    plan = [PlanFacadeOpening(f"p{u}", "s", u, 1.0) for u in (1.0, 4.0, 7.0)]
    elevation = [facade(u) for u in (4.0, 7.0, 10.0)]

    matches, plan_only, elevation_only = align_openings(plan, elevation)

    assert [(m.plan_opening_id, m.elevation_opening_id) for m in matches] == [
        ("p4.0", "e4"),
        ("p7.0", "e7"),
    ]
    assert plan_only == ["p1.0"]
    assert elevation_only == ["e10"]


def test_compile_elevation_measures_from_grade_and_left_face() -> None:
    reading = ElevationReadingSchema(
        view_id="elev-south",
        facing="south",
        levels=[
            LevelMarkSchema(name="GRADE", role="grade", y_px=1000.0, label="0'-0\""),
            LevelMarkSchema(
                name="SECOND FLOOR",
                role="floor",
                storey="Second Floor",
                y_px=600.0,
                label="4000",
            ),
        ],
        facade_left_px=100.0,
        facade_right_px=900.0,
        openings=[
            ElevationOpeningSchema(
                id="w1",
                kind="window",
                storey="First Floor",
                left_px=150.0,
                right_px=250.0,
                bottom_px=910.0,
                top_px=790.0,
            )
        ],
    )

    compiled = compile_elevation(reading, None)

    assert compiled.px_per_m == pytest.approx(100.0)
    assert compiled.facade_width_m == pytest.approx(8.0)
    opening = compiled.openings[0]
    assert (opening.u_m, opening.width_m) == pytest.approx((1.0, 1.0))
    assert (opening.sill_m, opening.head_m) == pytest.approx((0.9, 2.1))
    assert opening.storey == "first floor"


def test_walls_behind_a_porch_face_outside() -> None:
    reading = two_room_plan()
    reading.walls += [
        WallSchema(
            id="pw", start_px=px(0.0, 0.0), end_px=px(0.0, -2.0), kind="open_edge"
        ),
        WallSchema(
            id="ps", start_px=px(0.0, -2.0), end_px=px(8.0, -2.0), kind="open_edge"
        ),
        WallSchema(
            id="pe", start_px=px(8.0, -2.0), end_px=px(8.0, 0.0), kind="open_edge"
        ),
    ]
    reading.rooms.append(RoomSchema(id="r3", name="porch", seed_px=px(4.0, -1.0)))

    south = facade_openings(compile_plan(reading), "south")

    assert [row.opening_id for row in south] == ["w1"]


def test_unlabelled_levels_use_the_sheet_scale() -> None:
    reading = ElevationReadingSchema(
        view_id="elev-west",
        facing="west",
        levels=[
            LevelMarkSchema(name="ground", role="grade", y_px=900.0),
            LevelMarkSchema(
                name="upper floor", role="floor", storey="second floor", y_px=500.0
            ),
        ],
        facade_left_px=100.0,
        facade_right_px=900.0,
        openings=[],
    )

    compiled = compile_elevation(reading, sheet_px_per_m=100.0)

    upper = next(level for level in compiled.levels if level.role == "floor")
    assert upper.height_m == pytest.approx(4.0)
    with pytest.raises(CompileError, match="printed heights"):
        compile_elevation(reading, None)
