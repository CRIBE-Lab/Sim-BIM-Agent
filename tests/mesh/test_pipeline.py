from pathlib import Path

import pytest

from sim_bim.bim.geometry import export_glb
from sim_bim.demo import demo_building
from sim_bim.drawing.readings import Facing
from sim_bim.mesh.model import load_glb
from sim_bim.mesh.pipeline import (
    FacadeOpeningSchema,
    FacadeReadingSchema,
    MeshScaleSchema,
    align_to_axes,
    build_from_facades,
)
from sim_bim.mesh.raster import FacadeProjection, render_facade


def test_facades_of_the_demo_house_rebuild_its_footprint_and_window(
    tmp_path: Path,
) -> None:
    path = tmp_path / "demo.glb"
    path.write_bytes(export_glb(demo_building()))
    model = align_to_axes(load_glb(path))
    readings: dict[Facing, tuple[FacadeReadingSchema, FacadeProjection]] = {}
    facings: tuple[Facing, ...] = ("south", "east")
    for facing in facings:
        image, projection = render_facade(model, facing, 100.0)

        def x_px(u: float, projection=projection) -> float:
            return (u - projection.u_min) * projection.px_per_m

        def y_px(z: float, projection=projection) -> float:
            return (projection.z_max - z) * projection.px_per_m

        span = (-0.075, 8.075) if facing == "south" else (-0.075, 5.075)
        openings = (
            [
                FacadeOpeningSchema(
                    id="w1",
                    kind="window",
                    storey=1,
                    left_px=x_px(1.25),
                    right_px=x_px(2.75),
                    top_px=y_px(2.4),
                    bottom_px=y_px(1.2),
                )
            ]
            if facing == "south"
            else []
        )
        readings[facing] = (
            FacadeReadingSchema(
                building_left_px=x_px(span[0]),
                building_right_px=x_px(span[1]),
                ground_y_px=y_px(0.3),
                eave_y_px=y_px(3.3),
                floor_lines_y_px=[y_px(0.3)],
                openings=openings,
            ),
            projection,
        )
        assert image.width > 400

    building = build_from_facades(
        model, readings, MeshScaleSchema(metres_per_unit=1.0), "demo.glb", ""
    )

    xs = [x for x, _ in building.spaces[0].polygon]
    ys = [y for _, y in building.spaces[0].polygon]
    assert (max(xs) - min(xs), max(ys) - min(ys)) == pytest.approx(
        (8.15, 5.15), abs=0.25
    )
    assert building.storeys[0].height_m == pytest.approx(3.0, abs=0.05)
    window = building.openings[0]
    assert (window.width_m, window.sill_m, window.head_m) == pytest.approx(
        (1.5, 0.9, 2.1), abs=0.05
    )
    assert window.offset_m == pytest.approx(2.0, abs=0.25)
