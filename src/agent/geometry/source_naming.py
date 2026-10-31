"""Stable public BIM names, separate from persistent source identities."""
from __future__ import annotations

from collections import Counter, defaultdict
import re

from shapely.geometry import LineString, Point, Polygon

from src.agent.roles import ROOM_TYPES, normalize


# This is naming comparison precision, not a geometry snapping tolerance.
ROW_TOLERANCE_M = 1e-6
SCHEME_VERSION = "bim_names_v3"


_NAME_QUANT = 6


def _q(value: float) -> float:
    return round(float(value), _NAME_QUANT)


def _qpt(pt) -> tuple[float, float]:
    return (_q(pt[0]), _q(pt[1]))


def _is_ccw(coords: list) -> bool:
    s = 0.0
    for i in range(len(coords) - 1):
        x1, y1 = coords[i][0], coords[i][1]
        x2, y2 = coords[i + 1][0], coords[i + 1][1]
        s += (x2 - x1) * (y2 + y1)
    return s < 0  # shoelace: negative sum => CCW


def _canonical_ring(poly: Polygon) -> tuple[tuple[float, float], ...]:
    coords = [_qpt(p) for p in list(poly.exterior.coords)[:-1]]
    if len(coords) < 3:
        return tuple(coords)
    if not _is_ccw(coords + [coords[0]]):
        coords = list(reversed(coords))
    start = min(range(len(coords)), key=lambda i: (coords[i][1], coords[i][0]))
    return tuple(coords[start:] + coords[:start])


def _geometry_fingerprint(poly: Polygon) -> tuple:
    minx, miny, maxx, maxy = poly.bounds
    return (
        (_q(minx), _q(miny), _q(maxx), _q(maxy)),
        _q(poly.area),
        _canonical_ring(poly),
    )


def _zone_centroid_key(poly: Polygon) -> tuple[float, float]:
    c = poly.centroid
    return (_q(-c.y), _q(c.x))


def _zone_quadrant(poly: Polygon, footprint_x: list[float], footprint_y: list[float]) -> str:
    cx = (float(footprint_x[0]) + float(footprint_x[1])) / 2.0
    cy = (float(footprint_y[0]) + float(footprint_y[1])) / 2.0
    half_w = max(abs(float(footprint_x[1]) - float(footprint_x[0])) / 2.0, 1e-9)
    half_h = max(abs(float(footprint_y[1]) - float(footprint_y[0])) / 2.0, 1e-9)
    p = poly.centroid
    dx, dy = p.x - cx, p.y - cy
    tol_x = min(half_w * 0.05, 0.5) + 1e-6
    tol_y = min(half_h * 0.05, 0.5) + 1e-6
    near_x = abs(dx) <= tol_x
    near_y = abs(dy) <= tol_y
    if near_x and near_y:
        return "C"
    ns = "" if near_y else ("N" if dy > 0 else "S")
    ew = "" if near_x else ("E" if dx > 0 else "W")
    return ns + ew


def _ordered_spaces(spaces, polys, floor_order):
    """Group north-to-south rows before sorting west-to-east within each row.

    Anchor each row to its northernmost centroid to avoid transitive chains
    spanning more than the tolerance. Sorting first makes input order irrelevant
    and avoids rounded-bin boundaries splitting numerically equal values.
    """
    by_floor = defaultdict(list)
    for space in spaces:
        by_floor[space['floor_id']].append(space)
    ordered = []
    for fid in sorted(by_floor, key=floor_order.__getitem__):
        north_to_south = sorted(by_floor[fid], key=lambda s: (
            -polys[s['id']].centroid.y, polys[s['id']].centroid.x,
            _geometry_fingerprint(polys[s['id']]), s['id']))
        rows = []
        for space in north_to_south:
            y = polys[space['id']].centroid.y
            if not rows or rows[-1][0] - y > ROW_TOLERANCE_M:
                rows.append((y, []))
            rows[-1][1].append(space)
        for _, row in rows:
            ordered.extend(sorted(row, key=lambda s: (
                round(polys[s['id']].centroid.x, 6),
                _geometry_fingerprint(polys[s['id']]), s['id'])))
    return ordered


def geometry_key(vertices):
    """Winding/start-independent ordering for fragments and edge names."""
    return tuple(sorted(tuple(round(float(c), 6) for c in v) for v in vertices))


def build_public_names(source: dict) -> dict:
    floors = sorted(source["floors"], key=lambda f: (f["z_floor"], f["id"]))
    # F# is an ordinal, including annex/vertical groups; source labels stay
    # separate and do not imply geographic/architectural storey identity.
    floor_names = {f["id"]: f"F{i}" for i, f in enumerate(floors, 1)}
    floor_order = {f["id"]: i for i, f in enumerate(floors)}
    polys = {s["id"]: Polygon(s["polygon"], s.get('holes', [])) for s in source["spaces"]}
    points = [p for f in floors for p in f["footprint"]]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    ordered = _ordered_spaces(source["spaces"], polys, floor_order)
    width = max(2, len(str(len(ordered))))
    handles, spaces, boundaries, openings, sides = {}, {}, {}, {}, {}
    room_groups = {}
    for space in ordered:
        role = ROOM_TYPES.get(normalize(space.get("role")), ROOM_TYPES["unknown"])
        quadrant = _zone_quadrant(polys[space["id"]], [min(xs), max(xs)], [min(ys), max(ys)])
        room_groups[space["id"]] = (space["floor_id"], role["name_token"], quadrant)
    group_sizes = Counter(room_groups.values())
    group_ordinals = Counter()
    for i, space in enumerate(ordered, 1):
        sid = space["id"]
        handle = handles[sid] = f"Z{i:0{width}d}"
        group = room_groups[sid]
        fid, token, quadrant = group
        group_ordinals[group] += 1
        suffix = str(group_ordinals[group]) if group_sizes[group] > 1 else ""
        spaces[sid] = f"{handle}_{floor_names[fid]}_{token}_{quadrant}{suffix}"

    walls = {}
    by_space = defaultdict(list)
    for b in source["boundaries"]:
        by_space[b["space_id"]].append(b)
    for sid, rows in by_space.items():
        ring = list(_canonical_ring(polys[sid]))
        path = LineString(ring + [ring[0]])
        def wall_key(b):
            xy = list({tuple(v[:2]) for v in b["vertices"]})
            mid = Point(sum(p[0] for p in xy)/len(xy), sum(p[1] for p in xy)/len(xy))
            return round(path.project(mid), 6), geometry_key(b["vertices"]), b["id"]
        for i, b in enumerate(sorted((b for b in rows if b["geometry_type"] == "wall"), key=wall_key), 1):
            bid = b["id"]
            boundaries[bid] = f"{handles[sid]}_W{i}"
            # Find the canonical ring segment containing this wall's midpoint.
            xy = list({tuple(v[:2]) for v in b["vertices"]})
            mid = Point(sum(p[0] for p in xy)/len(xy), sum(p[1] for p in xy)/len(xy))
            walls[bid] = min(zip(ring, ring[1:]+ring[:1]), key=lambda ab: LineString(ab).distance(mid))
        for kind, token in (("floor", "Floor"), ("ceiling", "Ceiling"), ("roof", "Roof")):
            rows_kind = sorted((b for b in rows if b["geometry_type"] == kind), key=lambda b: (
                _zone_centroid_key(Polygon([v[:2] for v in b["vertices"]])), geometry_key(b["vertices"]), b["id"]))
            for i, b in enumerate(rows_kind, 1):
                boundaries[b["id"]] = f"{handles[sid]}_{token}" + (str(i) if len(rows_kind) > 1 else "")

    groups = defaultdict(list)
    for opening in source["openings"]:
        for bid in source["opening_hosts"][opening["id"]]:
            groups[(bid, opening["kind"])].append(opening)
    for (bid, kind), rows in groups.items():
        if bid not in boundaries:
            raise ValueError(f"opening host boundary is unknown or unsupported: {bid}")
        if bid in walls:
            a, b = walls[bid]
            dx, dy = b[0]-a[0], b[1]-a[1]
            def opening_key(o):
                vs = o["vertices"]
                along = min((v[0]-a[0])*dx + (v[1]-a[1])*dy for v in vs)
                return round(along, 6), min(v[2] for v in vs), geometry_key(vs), o["id"]
        else:
            # Horizontal apertures have no perimeter-wall direction. Only their
            # display ordering changes; hosts, vertices and source IDs stay intact.
            def opening_key(o):
                return geometry_key(o["vertices"]), o["id"]
        token = {"window": "Win", "door": "Door", "open": "Opening"}[kind]
        for i, o in enumerate(sorted(rows, key=opening_key), 1):
            sides.setdefault(o["id"], {})[bid] = f"{boundaries[bid]}_{token}{i}"
    for oid, hosts in sides.items():
        # One shared opening with two room-side aliases, not two doors.
        openings[oid] = min(hosts.values())
    return {"scheme_version": SCHEME_VERSION, "row_tolerance_m": ROW_TOLERANCE_M,
            "direction_frame": "model XY: +X=E, +Y=N; not a geographic north assertion",
            "source_floor_names": {f['id']: f.get('name') or f['id'] for f in floors},
            "floors": floor_names, "spaces": spaces, "boundaries": boundaries,
            "openings": openings, "opening_sides": sides}


def public_names_for_display(source: dict) -> dict:
    """Derive display names from the current complete source geometry."""
    return build_public_names(source)


def public_reference_map(names: dict) -> dict:
    """Map persistent source identities to their current public display names."""
    return {identity: name
            for kind in ("floors", "spaces", "boundaries", "openings")
            for identity, name in names.get(kind, {}).items()}


def public_reference_text(value: object, references: dict) -> str:
    """Replace complete object references in prose, leaving paths/URLs intact."""
    text = str(value)
    keys = sorted((key for key, name in references.items() if key != name), key=lambda key: (-len(key), key))
    if not keys:
        return text
    token_chars = r"A-Za-z0-9_./:%\\-"
    pattern = rf"(?<![{token_chars}])(?:{'|'.join(re.escape(key) for key in keys)})(?![A-Za-z0-9_/%\\-]|[.:][A-Za-z0-9_])"
    return re.sub(pattern, lambda match: references[match[0]], text)


def viewer_names(data: dict, parts: dict) -> dict:
    """Name disposable clickable fragments without rewriting IDs or geometry."""
    source = data.get("source_model") or {}
    names = public_names_for_display(source)
    derived = source.get("derived", {})
    surfaces = {s["name"]: names.get("boundaries", {}).get(derived.get("surfaces", {}).get(s["name"], s["name"]), s["name"])
                for s in data.get("surfaces", [])}
    objects = dict(surfaces)
    for kind in ("windows", "openings"):
        for o in data.get(kind, []):
            oid = derived.get(kind, {}).get(o["name"], o.get("source_opening_id", o["name"]))
            bid = derived.get("surfaces", {}).get(o["parent"], o["parent"])
            objects[o["name"]] = names.get("opening_sides", {}).get(oid, {}).get(bid, names.get("openings", {}).get(oid, o["name"]))
    fragment_names = {}
    for sid, rows in parts.items():
        ranks = {i: rank for rank, i in enumerate(sorted(range(len(rows)), key=lambda i: (
            geometry_key(rows[i]["verts"]), tuple(sorted(geometry_key(h) for h in rows[i].get("holes", []))))), 1)}
        fragment_names[sid] = [surfaces.get(sid, sid) + (f"_Part{ranks[i]}" if len(rows)>1 else "") for i in range(len(rows))]
    regions = data.get("enclosure_regions", [])
    region_names = [""] * len(regions)
    grouped = defaultdict(list)
    for i, r in enumerate(regions):
        grouped[(r["boundary_id"], r["condition"])].append(i)
    for (bid, condition), indices in grouped.items():
        for rank, i in enumerate(sorted(indices, key=lambda i: geometry_key(regions[i]["verts"])), 1):
            region_names[i] = names.get("boundaries", {}).get(bid, bid) + f"_{condition.title()}{rank}"
    edges = {}
    for obj in data.get("surfaces", []) + data.get("windows", []) + data.get("openings", []):
        vs = obj["verts"]
        order = sorted(range(len(vs)), key=lambda i: geometry_key([vs[i], vs[(i+1)%len(vs)]]))
        rank = {i:n for n,i in enumerate(order, 1)}
        edges[obj["name"]] = [f"{objects[obj['name']]}_Edge{rank[i]}" for i in range(len(vs))]
    floors = sorted(source.get("floors", []), key=lambda f: (f["z_floor"], f["id"]))
    return {"floors": [{"id": f["id"], "name": f"F{i}",
                       "source_name": f.get("name") or f["id"], "z_floor": f["z_floor"]}
                      for i, f in enumerate(floors, 1)],
            "spaces": names.get("spaces", {}), "objects": objects, "parts": fragment_names,
            "regions": region_names, "edges": edges,
            "references": public_reference_map(names)}
