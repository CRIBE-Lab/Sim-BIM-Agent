import pytest

from sim_bim.drawing.issues import CompileError
from sim_bim.drawing.plan import apply_patch, compile_plan
from sim_bim.drawing.readings import (
    DimensionChainSchema,
    PlanOpeningSchema,
    PlanPatchSchema,
    PlanReadingSchema,
    RoomSchema,
    WallSchema,
)

PX_PER_M = 100.0
ORIGIN = (50.0, 550.0)


def px(x_m: float, y_m: float) -> tuple[float, float]:
    return (ORIGIN[0] + x_m * PX_PER_M, ORIGIN[1] - y_m * PX_PER_M)


def two_room_plan(**overrides: object) -> PlanReadingSchema:
    walls = [
        WallSchema(id="s", start_px=px(-0.1, 0.0), end_px=px(8.0, 0.0)),
        WallSchema(id="e", start_px=px(8.0, 0.0), end_px=px(8.04, 5.0)),
        WallSchema(id="n", start_px=px(8.0, 5.0), end_px=px(0.1, 5.03)),
        WallSchema(id="w", start_px=px(0.0, 5.0), end_px=px(0.0, 0.0)),
        WallSchema(id="p", start_px=px(4.0, 0.1), end_px=px(4.0, 4.9), thickness_m=0.1),
    ]
    data: dict[str, object] = {
        "view_id": "plan-l1",
        "storey": "first floor",
        "chains": [
            DimensionChainSchema(
                axis="x",
                ticks_px=[px(0, 0)[0], px(4, 0)[0], px(8, 0)[0]],
                labels=["4000", "4000"],
            ),
            DimensionChainSchema(
                axis="y", ticks_px=[px(0, 0)[1], px(0, 5)[1]], labels=["5000"]
            ),
        ],
        "datum_px": ORIGIN,
        "walls": walls,
        "openings": [
            PlanOpeningSchema(
                id="d1", kind="door", wall_id="p", center_px=px(4.0, 2.5), width_m=0.9
            ),
            PlanOpeningSchema(
                id="w1", kind="window", wall_id="s", center_px=px(2.0, 0.0), width_m=1.2
            ),
        ],
        "rooms": [
            RoomSchema(id="r1", name="living", seed_px=px(2.0, 2.5)),
            RoomSchema(id="r2", name="bedroom", seed_px=px(6.0, 2.5)),
        ],
    }
    data.update(overrides)
    return PlanReadingSchema.model_validate(data)


def test_compile_snaps_walls_and_finds_rooms() -> None:
    plan = compile_plan(two_room_plan())

    areas = {space.name: _area(space.polygon) for space in plan.spaces}
    assert areas == pytest.approx({"living": 20.0, "bedroom": 20.0}, abs=0.3)
    door = next(o for o in plan.openings if o.id == "d1")
    assert door.offset_m == pytest.approx(2.5, abs=0.15)
    assert plan.px_per_m == pytest.approx(PX_PER_M)


def test_missing_partition_names_both_rooms() -> None:
    reading = two_room_plan()
    reading = apply_patch(
        reading,
        PlanPatchSchema.model_validate(
            {"edits": [{"op": "remove", "id": "p"}, {"op": "remove", "id": "d1"}]}
        ),
    )

    with pytest.raises(CompileError) as caught:
        compile_plan(reading)

    assert [issue.object_id for issue in caught.value.issues] == ["r2"]
    assert "r1" in caught.value.issues[0].message


def test_gap_in_wall_reports_free_end() -> None:
    reading = two_room_plan()
    reading.walls[2] = WallSchema(id="n", start_px=px(8.0, 5.0), end_px=px(1.0, 5.0))

    with pytest.raises(CompileError) as caught:
        compile_plan(reading)

    message = str(caught.value)
    assert "[r1]" in message
    assert "free end" in message


def test_opening_off_its_wall_is_rejected() -> None:
    reading = two_room_plan()
    reading.openings[1] = PlanOpeningSchema(
        id="w1", kind="window", wall_id="s", center_px=px(2.0, 2.0), width_m=1.2
    )

    with pytest.raises(CompileError, match=r"\[w1\] centre is 2.00 m away"):
        compile_plan(reading)


def _area(polygon: list[tuple[float, float]]) -> float:
    total = 0.0
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2
