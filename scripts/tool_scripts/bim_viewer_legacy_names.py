"""Recover presentation names from older display geometry, without creating source quantities."""
from collections import defaultdict
import re

from shapely.geometry import Polygon
from shapely.ops import polygonize, unary_union

from scripts.tool_scripts.bim_display_language import room_role
from src.agent.geometry.source_naming import build_public_names, viewer_names
from src.agent.geometry.storey_codes import public_storey_codes


_FLOOR = re.compile(r"(?:[A-Z][A-Z0-9-]*|[0-9]+F)")
_SPACE = re.compile(r"Z[0-9]+_[^_]+_[A-Za-z0-9_-]+_(?:NE|NW|SE|SW|N|S|E|W|C)[0-9]*")
_OLD_DRAFT_SPACE = re.compile(r"Z\d+_F\d+_[A-Za-z0-9_-]+_(?:NE|NW|SE|SW|N|S|E|W|C)\d*")
_FACE = re.compile(r"Z\d+_(?:W\d+|Floor\d*|Roof\d*|Ceiling\d*)")
_OPENING = re.compile(_FACE.pattern + r"_(?:Door|Opening|Win)\d+")


def _saved(pattern, *values):
    return next((v for v in values if isinstance(v, str) and pattern.fullmatch(v)), None)


def _available_handles(generated, saved, prefix):
    """Partial old maps may reserve numbers belonging to newly recovered entities."""
    result, used = {}, set()
    for key in sorted(saved):
        if saved[key] not in used:
            result[key] = saved[key]
            used.add(saved[key])
    occupied = set(generated.values()) | used
    next_number = 1
    for key, preferred in generated.items():
        if key in result:
            continue
        if preferred in used:
            width = len(preferred) - len(prefix)
            while (candidate := f"{prefix}{next_number:0{width}d}") in occupied:
                next_number += 1
            preferred = candidate
            occupied.add(preferred)
        result[key] = preferred
        used.add(preferred)
    return result


def _child_names(candidates):
    groups = defaultdict(dict)
    for key, (original, saved) in candidates.items():
        reference = original or saved
        if reference:
            groups[reference.rstrip("0123456789")][key] = original or saved
    resolved = {}
    for prefix, generated in groups.items():
        saved = {key: candidates[key][1] for key in generated
                 if candidates[key][1] and candidates[key][1].rstrip("0123456789") == prefix}
        resolved.update(_available_handles(generated, saved, prefix))
    return resolved


def _zone_outline(faces):
    """Use a horizontal face or a closed circuit of walls, never a guessed hull."""
    for kind in ("floor", "ceiling", "roof"):
        caps = [f for f in faces if f.get("type", "").lower() == kind]
        if caps:
            base = min(min(v[2] for v in f["verts"]) for f in caps)
            polygons = [Polygon([v[:2] for v in f["verts"]]) for f in caps
                        if all(abs(v[2] - base) < 1e-6 for v in f["verts"])]
            outline = unary_union([p for p in polygons if p.is_valid and p.area > 0])
            if outline.geom_type == "Polygon":
                return list(outline.exterior.coords)[:-1]
    segments = set()
    for face in faces:
        xy = sorted({tuple(v[:2]) for v in face["verts"]})
        if face.get("type", "").lower() == "wall" and len(xy) >= 2:
            segments.add((xy[0], xy[-1]))
    outline = unary_union(list(polygonize(sorted(segments))))
    return list(outline.exterior.coords)[:-1] if outline.geom_type == "Polygon" else []


def legacy_viewer_names(data, parts, roles, *, draft_anchors=False):
    source = data.get("source_model") or {}
    derived = {k: dict(v) for k, v in (source.get("derived") or {}).items()
               if k in ("zones", "surfaces", "windows", "openings") and isinstance(v, dict)}
    for group in ("zones", "surfaces", "windows", "openings"):
        derived.setdefault(group, {})
    saved_floors = (data.get("public_names") or {}).get("floors", [])
    floor_rows = source.get("floors") or data.get("floors") or (saved_floors if isinstance(saved_floors, list) else [])
    floors = {f["id"]: dict(f) for f in floor_rows if isinstance(f, dict) and f.get("id") and f.get("z_floor") is not None}
    spaces = {s["id"]: dict(s) for s in source.get("spaces", [])}
    faces_by_zone = defaultdict(list)
    for face in data.get("surfaces", []):
        if len(face.get("verts", [])) >= 3:
            zone = face.get("zone")
            faces_by_zone[derived["zones"].get(zone, zone)].append(face)
    metadata = {r["name"]: r for r in data.get("zone_meta", []) if r.get("name")}
    for zone in data.get("zones", []):
        sid = derived["zones"].get(zone, zone)
        space = spaces.get(sid, {})
        faces = faces_by_zone[sid]
        ring = space.get("polygon") or _zone_outline(faces)
        if not ring:
            continue
        base = min((v[2] for f in faces for v in f["verts"]), default=space.get("z_floor"))
        if base is None:
            continue
        # Preserve declared floor identity when two groups share an elevation.
        fid = space.get("floor_id") or metadata.get(zone, {}).get("floor_id")
        if not fid:
            fid = next((f for f in floors if abs(floors[f]["z_floor"] - base) < 1e-6), f"z={base:.6f}")
        floor = floors.setdefault(fid, {"id": fid, "z_floor": base})
        spaces[sid] = {**space, "id": sid, "floor_id": floor["id"], "polygon": ring,
                       "role": room_role(space.get("role") or roles.get(zone) or metadata.get(zone, {}).get("role"))}
    spaces = {sid: s for sid, s in spaces.items() if s.get("polygon") and s.get("floor_id") in floors}
    projected = defaultdict(list)
    for space in spaces.values():
        projected[space["floor_id"]].extend(space["polygon"])
    # Only the building XY extents enter the quadrant calculation in build_public_names.
    for fid, f in floors.items():
        if not f.get("footprint"):
            f["footprint"] = projected.get(fid, [])
    boundaries = {b["id"]: b for b in source.get("boundaries", [])
                  if b.get("space_id") in spaces and b.get("vertices") and b.get("geometry_type")}
    for sid, faces in faces_by_zone.items():
        if sid not in spaces:
            continue
        for f in faces:
            bid = derived["surfaces"].get(f["name"], f["name"])
            kind = f.get("type", "").lower()
            if kind in ("wall", "floor", "ceiling", "roof"):
                boundaries.setdefault(bid, {"id": bid, "space_id": sid, "geometry_type": kind, "vertices": f["verts"]})
    openings, hosts = {}, defaultdict(set)
    for group in ("windows", "openings"):
        for obj in data.get(group, []):
            oid = derived[group].get(obj["name"], obj.get("source_opening_id", obj["name"]))
            parent = obj.get("parent", "")
            bid = derived["surfaces"].get(parent, parent)
            if bid not in boundaries:
                continue
            kind = "window" if group == "windows" else obj.get("kind")
            if kind in ("window", "door", "open"):
                openings[oid] = {"id": oid, "kind": kind, "vertices": obj["verts"]}
                hosts[oid].add(bid)
    naming_source = {"floors": list(floors.values()), "spaces": list(spaces.values()),
                     "boundaries": list(boundaries.values()), "openings": list(openings.values()),
                     "opening_hosts": hosts, "derived": derived}
    names = build_public_names(naming_source)
    # Retain valid labels in partially archived files. Only bind an entry to
    # an actual display/source entity; never publish a supplied reference map.
    attached = {}
    for origin in (data.get("public_names"), source.get("public_names")):
        for group, values in (origin or {}).items():
            if isinstance(values, dict):
                attached.setdefault(group, {}).update(values)
            elif group == "floors" and isinstance(values, list):
                attached.setdefault(group, {}).update({r["id"]: r.get("name") for r in values if isinstance(r, dict) and r.get("id")})
    floor_aliases = {fid: name for fid, floor in floors.items() if
                    (name := _saved(_FLOOR, attached.get("floors", {}).get(fid), floor.get("public_name")))}
    saved_rooms = {}
    for zone in data.get("zones", []):
        sid = derived["zones"].get(zone, zone)
        label = _saved(_OLD_DRAFT_SPACE if draft_anchors else _SPACE,
                       attached.get("spaces", {}).get(sid), attached.get("spaces", {}).get(zone),
                       spaces.get(sid, {}).get("public_name"), metadata.get(zone, {}).get("public_name"), zone)
        if label:
            saved_rooms[sid] = label
    room_levels = defaultdict(set)
    for sid, label in saved_rooms.items():
        level = label.split("_", 2)[1]
        if sid in spaces and _FLOOR.fullmatch(level):
            room_levels[spaces[sid]["floor_id"]].add(level)
    for fid, levels in room_levels.items():
        if len(levels) == 1:
            floor_aliases.setdefault(fid, next(iter(levels)))
    floor_names = public_storey_codes([
        {**floor, "name": floor.get("source_name") or floor.get("name"),
         "archived_code": floor_aliases.get(fid) or _saved(_FLOOR, floor.get("name"))}
        for fid, floor in floors.items()
    ])
    names["floors"] = floor_names
    room_handles = {sid: label.split("_", 1)[0] for sid, label in names["spaces"].items()}
    assigned = _available_handles(room_handles, {sid: v.split("_", 1)[0] for sid, v in saved_rooms.items()}, "Z")
    old_spaces = {old: assigned[sid] for sid, old in room_handles.items()}
    for sid in assigned:
        original = saved_rooms.get(sid) or names["spaces"][sid]
        _, level, suffix = original.split("_", 2)
        floor = floor_names.get(spaces.get(sid, {}).get("floor_id")) or _saved(_FLOOR, level)
        names["spaces"][sid] = f"{assigned[sid]}_{floor}_{suffix}" if floor else assigned[sid]
    # A display archive may have only one wall of a room. Retain a unique English
    # handle even though its footprint and therefore its quadrant are unavailable.
    display_ids = {derived["zones"].get(z, z) for z in data.get("zones", [])}
    occupied = set(assigned.values())
    number = 1
    for sid in sorted(display_ids - names["spaces"].keys()):
        while (handle := f"Z{number:02d}") in occupied:
            number += 1
        names["spaces"][sid] = handle
        occupied.add(handle)
    faces = {}
    for obj in data.get("surfaces", []):
        key = obj["name"]
        bid = derived["surfaces"].get(key, key)
        original = names["boundaries"].get(bid, "")
        label = _saved(_FACE, attached.get("boundaries", {}).get(bid), attached.get("objects", {}).get(key),
                       obj.get("public_name"), key)
        if original:
            handle, suffix = original.split("_", 1)
            original = f"{old_spaces.get(handle, handle)}_{suffix}"
        faces[bid] = (original, label)
    names["boundaries"].update(_child_names(faces))
    aperture_sides = {}
    for group in ("windows", "openings"):
        for obj in data.get(group, []):
            key = obj["name"]
            oid = derived[group].get(key, obj.get("source_opening_id", key))
            bid = derived["surfaces"].get(obj["parent"], obj["parent"])
            host = names["boundaries"].get(bid)
            stored_primary = _saved(_OPENING, attached.get("openings", {}).get(oid))
            label = _saved(_OPENING, attached.get("opening_sides", {}).get(oid, {}).get(bid),
                           attached.get("objects", {}).get(key), obj.get("public_name"), key,
                           stored_primary if not host or (stored_primary or "").rsplit("_", 1)[0] == host else None)
            previous = names["opening_sides"].get(oid, {}).get(bid)
            original = host + "_" + previous.rsplit("_", 1)[-1] if host and previous else None
            aperture_sides[(oid, bid)] = (original, label)
    for (oid, bid), label in _child_names(aperture_sides).items():
        names["opening_sides"].setdefault(oid, {})[bid] = label
    result = viewer_names({**data, "source_model": naming_source}, parts, names=names, draft_anchors=draft_anchors)
    for row in result["floors"]:
        floor = floors[row["id"]]
        row["source_name"] = floor.get("source_name") or floor.get("name")
    result["primary_floors"] = {sid: space["floor_id"] for sid, space in spaces.items()}
    return result
