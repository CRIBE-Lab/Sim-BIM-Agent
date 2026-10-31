"""Project visible wall parts after subtracting explicit source apertures."""
from __future__ import annotations

from shapely.ops import unary_union
from src.agent.geometry.surfaces import OpeningBindingError, wall_frame, project_to_wall, lift_from_wall


def visible_wall_parts(data: dict) -> dict[str, list[dict]]:
    """Subtract explicit apertures for display, retaining authoritative parents.

    Door openings touching a floor become notches; raised openings become inner
    rings. Parts carry their original parent name so selecting a display piece
    still refers to the same source wall. Empty walls stay empty (full passage).
    """
    by_parent = {}
    for opening in data.get("openings", []):
        by_parent.setdefault(opening["parent"], []).append(opening)
    result = {}
    for face in data.get("surfaces", []):
        apertures = by_parent.get(face["name"])
        if not apertures:
            continue
        points = sorted(set(tuple(v[:2]) for v in face["verts"]))
        direction, _ = wall_frame(points[0], points[-1])
        parent = project_to_wall(face["verts"], points[0], direction)
        holes = [project_to_wall(o["verts"], points[0], direction) for o in apertures]
        if parent is None or any(h is None for h in holes):
            raise OpeningBindingError(f"opening display geometry is not on wall {face['name']}")
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
            "verts": lift_from_wall(list(part.exterior.coords)[:-1], points[0], direction),
            "holes": [lift_from_wall(list(ring.coords)[:-1], points[0], direction) for ring in part.interiors],
        } for part in parts]
    return result
