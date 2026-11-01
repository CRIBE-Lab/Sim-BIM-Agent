"""Write a run's deliverables: building JSON, GLB and the HTML viewer with its assets."""

import json
import math
import shutil
from importlib import resources
from pathlib import Path
from typing import Any, Final

import numpy as np
from shapely.geometry import Polygon

from sim_bim._share import atomic_write
from sim_bim.bim.geometry import COLOURS, element_meshes, export_glb
from sim_bim.bim.schema import BuildingSchema

ASSETS: Final = (
    "three.min.js",
    "OrbitControls.js",
    "GLTFLoader.js",
    "app.js",
    "app.css",
)
Z_UP_TO_Y_UP: Final = np.array(
    [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, -1.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
)
_KIND_NAMES: Final = {"door": "门", "window": "窗", "opening": "洞口"}


def _m(value: float) -> str:
    return f"{value:.2f} m"


def _info(building: BuildingSchema) -> dict[str, dict[str, Any]]:
    """Inspector fields per element id, in display order."""
    storeys = {s.id: s for s in building.storeys}
    walls = {w.id: w for w in building.walls}
    hosted: dict[str, int] = {}
    for opening in building.openings:
        hosted[opening.wall_id] = hosted.get(opening.wall_id, 0) + 1
    info: dict[str, dict[str, Any]] = {}
    for wall in building.walls:
        info[wall.id] = {
            "title": f"墙 {wall.id.split(':', 1)[-1]}",
            "fields": [
                ["楼层", storeys[wall.storey_id].name],
                ["长度", _m(math.dist(wall.start, wall.end))],
                ["厚度", _m(wall.thickness_m)],
                ["高度", _m(wall.height_m)],
                ["门窗数", str(hosted.get(wall.id, 0))],
            ],
            "trace": [
                ["编号", wall.id],
                ["起点", str(wall.start)],
                ["终点", str(wall.end)],
            ],
        }
    for opening in building.openings:
        info[opening.id] = {
            "title": f"{_KIND_NAMES[opening.kind]} {opening.id.split(':', 1)[-1]}",
            "fields": [
                ["楼层", storeys[opening.storey_id].name],
                ["宽度", _m(opening.width_m)],
                ["窗台高", _m(opening.sill_m)],
                ["顶高", _m(opening.head_m)],
                [
                    "高度来源",
                    "默认值" if opening.height_source == "default" else "立面",
                ],
                ["宿主墙", opening.wall_id.split(":", 1)[-1]],
            ],
            "trace": [
                ["编号", opening.id],
                ["高度记录", opening.height_source],
                ["沿墙位置", _m(opening.offset_m)],
                ["宿主墙编号", walls[opening.wall_id].id],
            ],
        }
    for space in building.spaces:
        shape = Polygon(space.polygon)
        info[space.id] = {
            "title": space.name,
            "fields": [
                ["楼层", storeys[space.storey_id].name],
                ["面积", f"{space.area_m2:.1f} m²"],
                ["周长", _m(shape.length)],
                ["楼面标高", _m(storeys[space.storey_id].elevation_m)],
            ],
            "trace": [["编号", space.id], ["顶点数", str(len(space.polygon))]],
        }
    return info


def viewer_payload(building: BuildingSchema) -> dict[str, Any]:
    """Data the viewer script renders: element meshes in y-up metres plus inspector text."""
    info = _info(building)
    space_ids = {s.id for s in building.spaces}
    elements = []
    for element in element_meshes(building):
        v = element.mesh.vertices
        y_up = np.column_stack([v[:, 0], v[:, 2], -v[:, 1]])
        elements.append(
            {
                "id": element.element_id,
                "category": element.category,
                "storey": element.storey_id,
                "positions": np.round(y_up, 3).ravel().tolist(),
                "indices": element.mesh.faces.ravel().tolist(),
                "room": element.element_id in space_ids,
                **info[element.element_id],
            }
        )
    reference = None
    if building.reference_mesh is not None:
        matrix = Z_UP_TO_Y_UP @ np.array(building.reference_mesh.to_building)
        reference = {
            "file": building.reference_mesh.file,
            "matrix": matrix.ravel().tolist(),
        }
    return {
        "name": building.name,
        "storeys": [s.model_dump(mode="json") for s in building.storeys],
        "colours": COLOURS,
        "elements": elements,
        "unresolved": building.unresolved,
        "assumptions": building.assumptions,
        "sources": building.sources,
        "reference": reference,
    }


def viewer_html(building: BuildingSchema) -> str:
    """Viewer page; its script and styles load from ``assets/`` beside it."""
    embedded = json.dumps(
        viewer_payload(building), ensure_ascii=False, separators=(",", ":")
    ).replace("<", "\\u003c")
    template = (
        resources.files("sim_bim.viewer")
        .joinpath("template.html")
        .read_text(encoding="utf-8")
    )
    return template.replace("__BUILDING_JSON__", embedded, 1)


def write_outputs(building: BuildingSchema, out_dir: Path) -> dict[str, Path]:
    """Write building.json, building.glb, viewer.html and the viewer assets."""
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    static = resources.files("sim_bim.viewer").joinpath("static")
    for name in ASSETS:
        with resources.as_file(static.joinpath(name)) as source:
            shutil.copyfile(source, assets / name)
    paths = {
        "building": out_dir / "building.json",
        "glb": out_dir / "building.glb",
        "viewer": out_dir / "viewer.html",
    }
    atomic_write(paths["building"], building.model_dump_json(indent=1).encode())
    atomic_write(paths["glb"], export_glb(building))
    atomic_write(paths["viewer"], viewer_html(building).encode())
    return paths
