"""Project visible wall parts after subtracting explicit source apertures."""
from __future__ import annotations

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union
from src.agent.geometry.surfaces import OpeningBindingError, WALL_PLANE_EPS_M, polygon_normal


def visible_wall_parts(data: dict) -> dict[str, list[dict]]:
    """Subtract explicit apertures for display, retaining authoritative parents.

    Door openings touching a floor become notches; raised openings become inner
    rings. Parts carry their original parent name so selecting a display piece
    still refers to the same source wall. Empty walls stay empty (full passage).
    """
    by_parent = {}
    for group in ("openings", "windows"):
        for opening in data.get(group, []):
            by_parent.setdefault(opening["parent"], []).append((group, opening["verts"]))
    result = {}
    for face in data.get("surfaces", []):
        apertures = by_parent.get(face["name"])
        if not apertures:
            continue
        origin = np.asarray(face["verts"][0], dtype=float)
        normal = polygon_normal(np.asarray(face["verts"]) - origin)
        axis = int(np.argmax(np.abs(normal)))
        if abs(normal[axis]) < 1e-10:
            continue
        keep = [i for i in range(3) if i != axis]

        def project(ring):
            relative = np.asarray(ring, dtype=float) - origin
            if len(relative) < 3 or np.any(np.abs(relative @ normal) > WALL_PLANE_EPS_M):
                return None
            shape = Polygon(relative[:, keep])
            return shape if shape.is_valid and shape.area > 0 else None

        def lift(coords):
            local = np.empty((len(coords), 3))
            local[:, keep] = coords
            local[:, axis] = -(local[:, keep] @ normal[keep]) / normal[axis]
            return (local + origin).tolist()

        parent = project(face["verts"])
        holes = []
        for group, ring in apertures:
            hole = project(ring)
            if hole is not None and parent is not None:
                holes.append(hole)
            elif group == "openings":
                raise OpeningBindingError(f"opening display geometry is not on its host {face['name']}")
        if not holes:
            continue
        holes.extend(shape for ring in face.get("holes", []) if (shape := project(ring)) is not None)
        difference = parent.difference(unary_union(holes))
        if difference.is_empty:
            parts = []
        elif difference.geom_type == "Polygon":
            parts = [difference]
        elif difference.geom_type == "MultiPolygon":
            parts = list(difference.geoms)
        else:
            raise OpeningBindingError(f"unsupported cut wall {face['name']}")
        result[face["name"]] = [{
            "verts": lift(list(part.exterior.coords)[:-1]),
            "holes": [lift(list(ring.coords)[:-1]) for ring in part.interiors],
        } for part in parts]
    return result
