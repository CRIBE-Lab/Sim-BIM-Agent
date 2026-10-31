"""Validate input room rings and their explicit bounding-box projections."""
from __future__ import annotations

import math
from typing import Any, Iterable
from shapely.geometry import Polygon
from src.agent.geometry_model.orthogonality import ORTHOGONALITY_EPSILON as _EPS, edge_is_axis_aligned

_BBOX_TOL = 1e-6

def cell_has_polygon(cell: Any) -> bool:
    return _raw_polygon(cell) is not None


def _raw_polygon(cell: Any):
    poly = getattr(cell, "polygon", None)
    if poly is None:
        poly = (getattr(cell, "__pydantic_extra__", None) or {}).get("polygon")
    return poly


def _as_pair(raw: Any, cell_id: str, idx: int) -> tuple[float, float]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError(f"cell {cell_id}: polygon vertex {idx} is not [x, y]")
    try:
        x = float(raw[0])
        y = float(raw[1])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"cell {cell_id}: polygon vertex {idx} is not numeric") from exc
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError(f"cell {cell_id}: polygon vertex {idx} is not finite")
    return x, y


def _cell_id(cell: Any) -> str:
    return str(getattr(cell, "id", "<unknown>"))


def _is_ccw(coords: Iterable[tuple[float, float]]) -> bool:
    pts = list(coords)
    s = 0.0
    for a, b in zip(pts, pts[1:]):
        s += (b[0] - a[0]) * (b[1] + a[1])
    return s < 0


def cell_polygon_vertices(
    cell: Any, *, allow_closed: bool = False, minimum_vertices: int = 4
) -> list[tuple[float, float]] | None:
    raw = _raw_polygon(cell)
    if raw is None:
        return None
    cid = _cell_id(cell)
    if not isinstance(raw, list):
        raise ValueError(f"cell {cid}: polygon must be a list of [x, y] vertices")
    if len(raw) < minimum_vertices:
        raise ValueError(f"cell {cid}: polygon must contain at least {minimum_vertices} vertices")
    pts = [_as_pair(p, cid, i) for i, p in enumerate(raw)]
    if pts[0] == pts[-1]:
        if not allow_closed:
            raise ValueError(
                f"cell {cid}: polygon must not repeat the first vertex at the end"
            )
        pts = pts[:-1]
        if len(pts) < minimum_vertices:
            raise ValueError(f"cell {cid}: polygon must contain at least {minimum_vertices} vertices")
    return pts


def _rect_vertices(cell: Any) -> list[tuple[float, float]]:
    cid = _cell_id(cell)
    if len(cell.x) != 2 or len(cell.y) != 2:
        raise ValueError(f"cell {cid}: x and y must be [min, max] pairs")
    x0, x1 = float(cell.x[0]), float(cell.x[1])
    y0, y1 = float(cell.y[0]), float(cell.y[1])
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def cell_polygon(
    cell: Any,
    *,
    min_edge_length_m: float | None = None,
    require_bbox_match: bool = True,
    allow_cw: bool = False,
    allow_closed: bool = False,
    require_orthogonal: bool = True,
) -> Polygon:
    pts = cell_polygon_vertices(cell, allow_closed=allow_closed, minimum_vertices=4 if require_orthogonal else 3)
    if pts is None:
        pts = _rect_vertices(cell)
    cid = _cell_id(cell)
    closed = pts + [pts[0]]

    for i, (a, b) in enumerate(zip(closed, closed[1:])):
        dx = abs(b[0] - a[0])
        dy = abs(b[1] - a[1])
        length = math.hypot(dx, dy)
        if length <= _EPS:
            raise ValueError(f"cell {cid}: polygon edge {i} is degenerate")
        if require_orthogonal and not edge_is_axis_aligned(dx, dy):
            raise ValueError(f"cell {cid}: polygon edge {i} is not orthogonal")
        if min_edge_length_m is not None and length < min_edge_length_m - _EPS:
            raise ValueError(
                f"cell {cid}: polygon edge {i} length {length:.6f} m is below "
                f"min_edge_length_m {min_edge_length_m:.6f} m"
            )

    if not allow_cw and not _is_ccw(closed):
        raise ValueError(f"cell {cid}: polygon exterior ring must be CCW")

    raw_holes = getattr(cell, "holes", []) or []
    holes = []
    for index, raw in enumerate(raw_holes):
        if not isinstance(raw, (list, tuple)) or len(raw) < (4 if require_orthogonal else 3):
            raise ValueError(f"cell {cid}: hole {index} requires a valid ring")
        hole = [_as_pair(p, cid, i) for i, p in enumerate(raw)]
        if hole[0] == hole[-1]:
            hole.pop()
        for a, b in zip(hole, hole[1:] + hole[:1]):
            length = math.dist(a, b)
            if length <= _EPS or (min_edge_length_m is not None and length < min_edge_length_m - _EPS):
                raise ValueError(f"cell {cid}: hole {index} has a degenerate or short edge")
            if require_orthogonal and not edge_is_axis_aligned(abs(a[0]-b[0]), abs(a[1]-b[1])):
                raise ValueError(f"cell {cid}: hole {index} is not orthogonal")
        holes.append(hole)
    poly = Polygon(pts, holes)
    if poly.is_empty or poly.area <= _EPS:
        raise ValueError(f"cell {cid}: polygon is degenerate")
    if not poly.is_valid:
        raise ValueError(f"cell {cid}: polygon is invalid/self-intersecting")
    exterior = Polygon(pts)
    if any(not exterior.contains(Polygon(h)) or exterior.boundary.intersects(Polygon(h).boundary) for h in holes):
        raise ValueError(f"cell {cid}: holes must lie strictly inside the exterior")

    if require_bbox_match and cell_has_polygon(cell):
        minx, miny, maxx, maxy = poly.bounds
        expected_x = [float(min(cell.x)), float(max(cell.x))]
        expected_y = [float(min(cell.y)), float(max(cell.y))]
        actual_x = [float(minx), float(maxx)]
        actual_y = [float(miny), float(maxy)]
        if (
            abs(expected_x[0] - actual_x[0]) > _BBOX_TOL
            or abs(expected_x[1] - actual_x[1]) > _BBOX_TOL
            or abs(expected_y[0] - actual_y[0]) > _BBOX_TOL
            or abs(expected_y[1] - actual_y[1]) > _BBOX_TOL
        ):
            raise ValueError(
                f"cell {cid}: x/y bbox {expected_x}/{expected_y} does not match "
                f"polygon bbox {actual_x}/{actual_y}"
            )
    return poly
