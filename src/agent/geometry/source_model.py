"""Build source spaces and full boundaries from validated geometry inputs."""
from __future__ import annotations

import hashlib
import json
from urllib.parse import quote
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.polygon import orient
from src.agent.geometry_model.schema import GeometryInput, SourceBoundary, SourceSpace
from src.agent.geometry_model.cells import cell_polygon


def _ring(polygon: Polygon) -> list[list[float]]:
    # Remove only exactly collinear display vertices; do not snap source geometry.
    points = list(orient(polygon.simplify(0), sign=1).exterior.coords)[:-1]
    start = min(range(len(points)), key=points.__getitem__)
    return [list(p) for p in points[start:] + points[:start]]


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode()).hexdigest()


def source_primitives(geom: GeometryInput, *, polygon_builder=cell_polygon, wall_breakpoints=None):
    """Build source identities and full room boundaries, without derived faces."""
    spaces: list[SourceSpace] = []
    boundaries: dict[str, SourceBoundary] = {}
    polygons: dict[str, Polygon] = {}
    by_space: dict[str, list[str]] = {}
    for floor in geom.floors:
        floor_id = str(getattr(floor, "id", None) or floor.name)
        for cell in floor.cells:
            if cell.id in polygons:
                raise ValueError(f"duplicate source space id: {cell.id}")
            poly = polygon_builder(cell)
            polygons[cell.id] = poly
            ring = _ring(poly)
            holes = []
            for interior in orient(poly.simplify(0), sign=1).interiors:
                points = list(interior.coords)[:-1]
                start = min(range(len(points)), key=points.__getitem__)
                holes.append([list(p) for p in points[start:] + points[:start]])
            breaks = (wall_breakpoints or {}).get(floor_id, [])
            def subdivide(points):
                split_ring = []
                for a, b in zip(points, points[1:] + points[:1]):
                    line = LineString([a, b])
                    interior = [p for p in breaks if line.distance(Point(p)) < 1e-7
                                and 1e-7 < line.project(Point(p)) < line.length - 1e-7]
                    split_ring.extend([a, *[list(p) for p in sorted(
                        interior, key=lambda p: line.project(Point(p)))]])
                return split_ring
            if breaks:
                ring = subdivide(ring)
                holes = [subdivide(points) for points in holes]
            z0 = float(floor.z_floor)
            z1 = z0 + float(floor.ceiling_height)
            ref = f"geometry:floor/{quote(floor_id, safe='')}/cell/{quote(cell.id, safe='')}"
            space = SourceSpace(
                id=cell.id, floor_id=floor_id, polygon=ring, holes=holes, z_floor=z0,
                height=z1-z0, role=cell.role,
                vertical_sections=getattr(cell, "vertical_sections", []),
                source_refs=getattr(cell, "source_refs", []),
                assumptions=getattr(cell, "assumptions", []),
                role_evidence=getattr(cell, "role_evidence", None),
            )
            # The geometry pointer is traceability, not a replacement for the
            # input's actual drawing observations and explicit hypotheses.
            space.source_refs = list(dict.fromkeys([ref, *space.source_refs]))
            spaces.append(space)
            if space.vertical_sections:
                from src.agent.geometry.source_volume import section_shell, space_sections
                rows = section_shell(space_sections(space), wall_breakpoints=breaks)
                by_space[cell.id] = []
                counters = {"wall": 0, "floor": 0, "ceiling": 0}
                for row in rows:
                    kind = row["geometry_type"]
                    suffix = f"{kind}/section/{counters[kind]}"
                    counters[kind] += 1
                    bid = f"space/{quote(cell.id, safe='')}/{suffix}"
                    boundaries[bid] = SourceBoundary(
                        id=bid, space_id=cell.id, kind="physical", source_refs=[ref], **row)
                    by_space[cell.id].append(bid)
                continue
            rows = []
            for index, (a, b) in enumerate(zip(ring, ring[1:] + ring[:1])):
                rows.append((f"wall/{index}", "wall", [
                    [*a, z0], [*b, z0], [*b, z1], [*a, z1],
                ]))
            for hole_index, points in enumerate(holes):
                for index, (a, b) in enumerate(zip(points, points[1:] + points[:1])):
                    rows.append((f"wall/hole/{hole_index}/{index}", "wall", [
                        [*a, z0], [*b, z0], [*b, z1], [*a, z1],
                    ]))
            rows.extend([
                ("floor", "floor", [[*p, z0] for p in ring]),
                ("ceiling", "ceiling", [[*p, z1] for p in ring]),
            ])
            by_space[cell.id] = []
            for suffix, kind, vertices in rows:
                bid = f"space/{quote(cell.id, safe='')}/{suffix}"
                boundaries[bid] = SourceBoundary(
                    id=bid, space_id=cell.id, kind="physical", geometry_type=kind,
                    vertices=vertices, source_refs=[ref],
                    holes=[[[*p, z0 if kind == 'floor' else z1] for p in h] for h in holes] if kind != 'wall' else [],
                )
                by_space[cell.id].append(bid)

    return spaces, boundaries, polygons, by_space
