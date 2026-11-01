"""Triangle meshes for a building: wall pieces around openings, floor slabs and opening panels."""

import math
from dataclasses import dataclass
from typing import Final, Literal, cast

import numpy as np
import shapely
import trimesh
from shapely.geometry import Polygon
from trimesh.visual import ColorVisuals

from sim_bim.bim.schema import BuildingSchema, OpeningSchema, WallSchema

type Category = Literal["wall", "floor", "window", "door"]

PANEL_M: Final = 0.04
COLOURS: Final[dict[Category, tuple[int, int, int, int]]] = {
    "wall": (232, 226, 214, 255),
    "floor": (170, 160, 150, 255),
    "window": (110, 170, 220, 160),
    "door": (140, 95, 60, 255),
}


def _box(
    start: np.ndarray, end: np.ndarray, width: float, z0: float, z1: float
) -> trimesh.Trimesh:
    length = float(np.linalg.norm(end - start))
    box = trimesh.creation.box(extents=(length, width, z1 - z0))
    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    centre = (start + end) / 2
    transform = trimesh.transformations.rotation_matrix(angle, (0, 0, 1))
    transform[:3, 3] = (centre[0], centre[1], (z0 + z1) / 2)
    box.apply_transform(transform)
    return box


def _wall_pieces(
    wall: WallSchema, openings: list[OpeningSchema], base: float
) -> list[trimesh.Trimesh]:
    start, end = np.array(wall.start), np.array(wall.end)
    length = float(np.linalg.norm(end - start))
    if length == 0:
        return []
    unit = (end - start) / length
    pieces = []
    cursor = 0.0
    for opening in sorted(openings, key=lambda o: o.offset_m):
        low = max(0.0, opening.offset_m - opening.width_m / 2)
        high = min(length, opening.offset_m + opening.width_m / 2)
        if low > cursor:
            pieces.append(
                _box(
                    start + unit * cursor,
                    start + unit * low,
                    wall.thickness_m,
                    base,
                    base + wall.height_m,
                )
            )
        a, b = start + unit * low, start + unit * high
        if opening.sill_m > 0:
            pieces.append(_box(a, b, wall.thickness_m, base, base + opening.sill_m))
        if opening.head_m < wall.height_m:
            pieces.append(
                _box(
                    a, b, wall.thickness_m, base + opening.head_m, base + wall.height_m
                )
            )
        cursor = max(cursor, high)
    if cursor < length:
        pieces.append(
            _box(
                start + unit * cursor, end, wall.thickness_m, base, base + wall.height_m
            )
        )
    return pieces


def _floor(polygon: list[tuple[float, float]], z: float) -> trimesh.Trimesh | None:
    shape = Polygon(polygon)
    if not shape.is_valid or shape.area == 0:
        return None
    triangles = shapely.get_parts(shapely.constrained_delaunay_triangles(shape))
    vertices = [(x, y, z) for t in triangles for x, y in list(t.exterior.coords)[:3]]
    faces = np.arange(len(vertices)).reshape(-1, 3)
    return trimesh.Trimesh(vertices=vertices, faces=faces)


@dataclass(frozen=True)
class ElementMesh:
    """Triangles of one building element, in metres with z up."""

    element_id: str
    category: Category
    storey_id: str
    mesh: trimesh.Trimesh


def element_meshes(building: BuildingSchema) -> list[ElementMesh]:
    """One mesh per wall (cut around its openings), floor space and opening panel."""
    storeys = {storey.id: storey for storey in building.storeys}
    by_wall: dict[str, list[OpeningSchema]] = {}
    for opening in building.openings:
        by_wall.setdefault(opening.wall_id, []).append(opening)
    elements: list[ElementMesh] = []
    for wall in building.walls:
        if wall.kind != "wall":
            continue
        pieces = _wall_pieces(
            wall, by_wall.get(wall.id, []), storeys[wall.storey_id].elevation_m
        )
        if pieces:
            mesh = cast(trimesh.Trimesh, trimesh.util.concatenate(pieces))
            elements.append(ElementMesh(wall.id, "wall", wall.storey_id, mesh))
    walls = {wall.id: wall for wall in building.walls}
    for opening in building.openings:
        wall = walls[opening.wall_id]
        start, end = np.array(wall.start), np.array(wall.end)
        unit = (end - start) / np.linalg.norm(end - start)
        base = storeys[opening.storey_id].elevation_m
        a = start + unit * (opening.offset_m - opening.width_m / 2)
        b = start + unit * (opening.offset_m + opening.width_m / 2)
        category: Category = "window" if opening.kind == "window" else "door"
        panel = _box(a, b, PANEL_M, base + opening.sill_m, base + opening.head_m)
        elements.append(ElementMesh(opening.id, category, opening.storey_id, panel))
    for space in building.spaces:
        floor = _floor(space.polygon, storeys[space.storey_id].elevation_m)
        if floor is not None:
            elements.append(ElementMesh(space.id, "floor", space.storey_id, floor))
    return elements


def building_meshes(building: BuildingSchema) -> dict[Category, trimesh.Trimesh]:
    """One merged, coloured mesh per category, in metres with z up."""
    parts: dict[Category, list[trimesh.Trimesh]] = {}
    for element in element_meshes(building):
        parts.setdefault(element.category, []).append(element.mesh)
    meshes: dict[Category, trimesh.Trimesh] = {}
    for category, items in parts.items():
        mesh = cast(trimesh.Trimesh, trimesh.util.concatenate(items))
        mesh.visual = ColorVisuals(mesh, face_colors=COLOURS[category])
        meshes[category] = mesh
    return meshes


def export_glb(building: BuildingSchema) -> bytes:
    """Coloured GLB of the building, y up as glTF requires."""
    scene = trimesh.Scene()
    to_y_up = trimesh.transformations.rotation_matrix(-math.pi / 2, (1, 0, 0))
    for category, mesh in building_meshes(building).items():
        scene.add_geometry(mesh.copy().apply_transform(to_y_up), node_name=category)
    return scene.export(file_type="glb")
