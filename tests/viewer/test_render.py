from pathlib import Path

from sim_bim.bim.build import build_from_drawings
from sim_bim.drawing.elevation import compile_elevation
from sim_bim.drawing.plan import compile_plan
from sim_bim.viewer.render import viewer_html, write_outputs
from tests.bim.test_build import south_elevation
from tests.drawing.test_plan import two_room_plan


def test_viewer_embeds_model_text_as_data(tmp_path: Path) -> None:
    building = build_from_drawings(
        "</script><script>alert(1)</script> __BUILDING_JSON__",
        [compile_plan(two_room_plan())],
        [compile_elevation(south_elevation(), None)],
        sources=[],
    )

    html = viewer_html(building)

    assert "<script>alert(1)" not in html
    assert html.count("<script") == 5
    paths = write_outputs(building, tmp_path)
    assert (tmp_path / "assets" / "three.min.js").stat().st_size > 100_000
    assert (tmp_path / "assets" / "app.js").exists()
    assert paths["glb"].read_bytes()[:4] == b"glTF"
