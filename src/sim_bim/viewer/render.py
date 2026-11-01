"""Write a run's deliverables: building JSON, GLB and the HTML viewer with its assets."""

import json
import shutil
from importlib import resources
from pathlib import Path

import numpy as np

from sim_bim._share import atomic_write
from sim_bim.bim.geometry import COLOURS, building_meshes, export_glb
from sim_bim.bim.schema import BuildingSchema

_ASSETS = ("three.min.js", "OrbitControls.js")


def viewer_html(building: BuildingSchema) -> str:
    """Self-describing viewer page; three.js is loaded from ``assets/`` beside it."""
    meshes = {}
    for category, mesh in building_meshes(building).items():
        y_up = np.column_stack(
            [mesh.vertices[:, 0], mesh.vertices[:, 2], -mesh.vertices[:, 1]]
        )
        meshes[category] = {
            "positions": np.round(y_up, 3).ravel().tolist(),
            "indices": mesh.faces.ravel().tolist(),
            "colour": COLOURS[category],
        }
    payload = building.model_dump(
        mode="json", include={"name", "storeys", "spaces", "unresolved"}
    )
    payload["meshes"] = meshes
    embedded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace(
        "<", "\\u003c"
    )
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
    for name in _ASSETS:
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
