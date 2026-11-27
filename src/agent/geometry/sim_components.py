"""Surface materials and shared nominal component thicknesses. Geometry stays unchanged."""
from collections import defaultdict
from math import isfinite

from shapely.ops import unary_union

from .principal_material import contact_materials, material_across_patches, opening_material
from .sim_component_topology import Plane, component_id, physical_families
from .source_bim import EPS, _parts
from .thickness_options import PRESETS_MM, normalize_candidates


SOURCES = ("inference", "from material", "from user")
MATERIAL_SCOPE = "principal_construction"
_DOOR_MM = (8, 10, 12, 35, 36, 40, 45, 50, 55, 60, 70, 80)
_TARGET_MM = {"wall": 150, "horizontal": 150, "roof": 150, "glazing": 24, "door": 45}


def _provenance(row, required):
    if not isinstance(row, dict) or set(row) - {"source", "source_refs", *required}:
        raise ValueError("invalid Sim-BIM property fields")
    source = row.get("source")
    if source not in SOURCES:
        raise ValueError("property source must be from material, inference, or from user")
    refs = row.get("source_refs", [])
    if not isinstance(refs, list) or any(not isinstance(s, str) or not s.strip() for s in refs):
        raise ValueError("property source_refs must be a list of nonempty references")
    if source != "inference" and not refs:
        raise ValueError("direct material and user properties require a source reference")
    return {"source": source, "source_refs": list(dict.fromkeys(refs))}


def _thickness(row):
    record = _provenance(row, {"value_mm"})
    value = row.get("value_mm")
    if type(value) not in {float, int} or not isfinite(value) or value <= 0:
        raise ValueError("component thickness must be positive finite millimetres")
    return {"value_mm": value, **record}


def _material(row):
    record = _provenance(row, {"en", "zh"})
    for key in ("en", "zh"):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise ValueError("material description requires English and Chinese display text")
        record[key] = row[key].strip()
    return record


def _adopt(kind, candidates, target=None):
    choices = _DOOR_MM if kind == "door" else candidates.get(kind, PRESETS_MM[kind])
    target = _TARGET_MM[kind] if target is None else target
    return {"value_mm": min(choices, key=lambda v: (abs(v - target), v)),
            "source": "inference", "source_refs": []}


def _preferred(records):
    """Only a documented value supersedes an inference. Contradictory documents stay visible."""
    explicit = [r for r in records if r["source"] != "inference"]
    return explicit or records


def _agree(records, scope):
    preferred = _preferred(records)
    values = {r["value_mm"] for r in preferred}
    if len(values) > 1:
        raise ValueError(f"conflicting thicknesses {sorted(values)} mm on {scope}; "
                         "reconcile the sources or declare evidence-backed, disjoint regions")
    if not preferred:
        return None
    rank = max(SOURCES.index(r["source"]) for r in preferred)
    refs = sorted({ref for r in preferred if r["source"] == SOURCES[rank] for ref in r["source_refs"]})
    return {"value_mm": preferred[0]["value_mm"], "source": SOURCES[rank], "source_refs": refs}


def _legacy_readings(source, *, archive=False):
    observations = {}
    recorded = {}
    for ref in source.get("wall_references", []):
        status, metres = ref.get("evidence_status"), ref.get("thickness_m")
        if status not in {"observed", "inferred"} or metres is None:
            continue
        ids = ref.get("boundary_ids", [ref["boundary_id"]])
        # A dimensional reference may describe only a lining or an individual layer.
        # Only an explicit whole-wall reading can stand on its own as assembly thickness.
        scope = " ".join(str(ref.get("thickness_scope", "")).lower().split())
        if scope in {"complete wall", "whole wall", "full wall thickness", "total wall thickness",
                     "entire wall", "entire declared plan line at this floor", "墙体总厚度", "整墙厚度"}:
            record = _thickness({"value_mm": round(metres * 1000, 12),
                                 "source": "from material" if status == "observed" else "inference",
                                 "source_refs": ref["source_refs"]})
            for bid in ids:
                observations[bid, metres] = status
                recorded[bid] = record
    result = dict(recorded)
    for row in source.get("component_attributes", []):
        ids = row["boundary_ids"]
        status = next((observations[bid, row["thickness_m"]] for bid in ids
                       if (bid, row["thickness_m"]) in observations), None)
        tag = row.get("basis", "").strip().lower()
        uncertain = tag not in SOURCES and status is None
        if tag not in SOURCES:
            tag = "from material" if status == "observed" else "inference"
        record = _thickness({"value_mm": round(row["thickness_m"] * 1000, 12), "source": tag,
                             "source_refs": list(row.get("source_refs", []))})
        # A prior nominal value can support an inferred adoption for a new delivery.
        # An archive must not retrospectively classify a free-text, unclassified basis.
        if archive and uncertain:
            record["source"] = None
        for bid in ids:
            existing = recorded.get(bid)
            if existing and (record["source"] is None or existing["source"] != "inference"):
                result[bid] = existing if record["source"] is None else _agree([existing, record], bid)
            else:
                result[bid] = record
    return result


def _declarations(declarations, boundaries, openings):
    supplied = {} if declarations is None else declarations
    if not isinstance(supplied, dict) or set(supplied) - {"boundaries", "openings"}:
        raise ValueError("property declarations must be keyed by boundaries or openings")
    result = {}
    for group, inventory in (("boundaries", boundaries), ("openings", openings)):
        allowed = {"material", "thickness"}
        if group == "boundaries":
            allowed.add("thickness_regions")
        rows = supplied.get(group, {})
        if not isinstance(rows, dict) or set(rows) - inventory.keys():
            raise ValueError(f"property declarations reference unknown {group} IDs")
        validated = {}
        for identity, row in rows.items():
            if not isinstance(row, dict) or set(row) - allowed:
                raise ValueError(f"invalid {group} property declaration on {identity}")
            obj = {}
            if "material" in row:
                obj["material"] = _material(row["material"])
            if "thickness" in row:
                obj["thickness"] = _thickness(row["thickness"])
            if "thickness_regions" in row:
                regions = row["thickness_regions"]
                if not isinstance(regions, list) or not regions:
                    raise ValueError(f"expected nonempty thickness_regions on {identity}")
                obj["thickness_regions"] = []
                # Validate against the actual declaring side, independently of any contact projection.
                plane = Plane(inventory[identity])
                host = plane.shape(inventory[identity])
                for region in regions:
                    if (not isinstance(region, dict) or set(region) - {"vertices", "holes", "thickness"}
                            or not {"thickness", "vertices"} <= region.keys()):
                        raise ValueError(f"invalid local thickness region on {identity}")
                    holes = region.get("holes", [])
                    if not isinstance(holes, list) or any(
                        not isinstance(ring, list) or len(ring) < 3 or any(
                            not isinstance(p, (list, tuple)) or len(p) != 3 or any(
                                type(v) not in (int, float) or not isfinite(v) for v in p) for p in ring)
                        for ring in [region["vertices"], *holes]
                    ):
                        raise ValueError(f"thickness region needs finite 3D polygon rings on {identity}")
                    thickness = _thickness(region["thickness"])
                    if thickness["source"] == "inference":
                        raise ValueError(f"local thickness changes require a material or user source on {identity}")
                    if not host.buffer(EPS).covers(plane.shape(region)):
                        raise ValueError(f"thickness region extends outside {identity}")
                    obj["thickness_regions"].append({**region, "thickness": thickness})
            validated[identity] = obj
        result[group] = validated
    return result


def _regional_pieces(shape, masks):
    pieces = [(shape, [])]
    for mask, value in masks:
        next_pieces = []
        for piece, records in pieces:
            if not piece.intersects(mask):
                next_pieces.append((piece, records))
                continue
            next_pieces.extend((p, records) for p in _parts(piece.difference(mask)))
            next_pieces.extend((p, [*records, value]) for p in _parts(piece.intersection(mask)))
        pieces = next_pieces
    return pieces


def _resolve_family(family, kind, plane, patches, declarations, readings, fallback):
    whole = {bid: readings[bid] for bid in family if bid in readings}
    preferred = _preferred(list(whole.values()))
    values = {r["value_mm"] for r in preferred}
    scope = ", ".join(family)
    if len(values) > 1 and all(r["source"] == "inference" for r in preferred):
        _agree(preferred, scope)
    uniform = _agree(preferred, scope) if len(values) == 1 else None
    masks = {bid: [(plane.shape(r), (bid, r["thickness"])) for r in declarations.get(bid, {}).get("thickness_regions", [])]
             for bid in family}
    groups = defaultdict(list)
    for hosts, shape in patches:
        local = [whole[bid] for bid in hosts if bid in whole]
        limits = [mask for bid in hosts for mask in masks[bid]]
        for piece, overrides in _regional_pieces(shape, limits):
            if overrides:
                # A local declaration may refine its own general reading, but an
                # independently documented measurement across the contact must agree.
                authors = {bid for bid, _ in overrides}
                counterpart = [whole[bid] for bid in hosts if bid not in authors and bid in whole]
                thickness = _agree([r for _, r in overrides] + counterpart, ", ".join(hosts))
            elif uniform:
                thickness = uniform
            else:
                thickness = _agree(local, ", ".join(hosts)) or fallback
            if thickness:
                groups[thickness["value_mm"]].append((hosts, piece, thickness))
    return groups


def build_surface_properties(source, thickness_candidates=None, declarations=None):
    """Resolve a current delivery from source-bound observations, selections and explicit inference."""
    candidates = normalize_candidates(thickness_candidates)
    boundaries = {b["id"]: b for b in source.get("boundaries", [])}
    openings = {o["id"]: o for o in source.get("openings", [])}
    spaces = {s["id"]: s for s in source.get("spaces", [])}
    declared = _declarations(declarations, boundaries, openings)
    sides, apertures = declared["boundaries"], declared["openings"]
    readings = _legacy_readings(source)
    for bid, row in sides.items():
        if "thickness" in row:
            previous = readings.get(bid)
            records = ([previous, row["thickness"]] if previous and previous["source"] != "inference"
                       else [row["thickness"]])
            readings[bid] = _agree(records, bid)
    components, result, open_results = {}, {}, {}
    patch_materials = defaultdict(list)
    for family, kind, plane, patches in physical_families(source, boundaries):
        # Exterior walls without a counterpart and walls dividing rooms have different nominal defaults.
        target = 240 if kind == "wall" and len(family) == 1 else _TARGET_MM[kind]
        fallback = _adopt(kind, candidates, target)
        applicable = {}
        for bid in family:
            estimate = sides.get(bid, {}).get("thickness")
            if estimate and estimate["source"] == "inference" and estimate["value_mm"] not in candidates.get(kind, PRESETS_MM[kind]):
                raise ValueError(f"inferred thickness on {bid} must use a {kind} candidate")
            if bid in readings:
                value = readings[bid]
                if not estimate and value["source"] == "inference" and kind in candidates:
                    value = {**value, "value_mm": _adopt(kind, candidates, value["value_mm"])["value_mm"]}
                applicable[bid] = value
        by_value = _resolve_family(family, kind, plane, patches, sides, applicable, fallback)
        materials_by_contact = {}
        for rows in by_value.values():
            owners = sorted({bid for hosts, _, _ in rows for bid in hosts})
            identity = [family]
            if len(by_value) > 1:
                identity.append(unary_union([shape for _, shape, _ in rows]).normalize().wkb_hex)
            cid = component_id(kind, identity)
            components[cid] = {"kind": kind, "boundary_ids": owners,
                               "thickness": _agree([r[2] for r in rows], ", ".join(owners))}
            for hosts, shape, _ in rows:
                if hosts not in materials_by_contact:
                    roles = {spaces.get(boundaries[bid]["space_id"], {}).get("role") for bid in hosts}
                    materials_by_contact[hosts] = contact_materials(hosts, sides, kind, roles)
                for bid in hosts:
                    patch_materials[bid].append(materials_by_contact[hosts][bid])
                    record = result.setdefault(bid, {"component_ids": [], "material": None})
                    if cid not in record["component_ids"]:
                        record["component_ids"].append(cid)
                    record.setdefault("_regions", defaultdict(list))[cid].append((plane, shape))
    for bid, boundary in boundaries.items():
        if bid not in result:
            if sides.get(bid):
                raise ValueError(f"properties supplied on a boundary with no physical area: {bid}")
            result[bid] = {"component_ids": [], "material": None}
            continue
        record = result[bid]
        masks = record.pop("_regions")
        if sides.get(bid, {}).get("thickness_regions"):
            covered = unary_union([shape for entries in masks.values() for _, shape in entries])
            plane = next(iter(masks.values()))[0][0]
            for region in sides[bid]["thickness_regions"]:
                if covered.intersection(plane.shape(region)).area <= EPS ** 2:
                    raise ValueError(f"thickness region has no physical area on {bid}")
        if len(masks) > 1:
            record["regions"] = [region for cid, shapes in sorted(masks.items())
                                 for region in shapes[0][0].regions(unary_union([s for _, s in shapes]), cid)]
        record["component_ids"].sort()
        record["material"] = material_across_patches(patch_materials[bid])
    windows_by_host = defaultdict(list)
    for opening in openings.values():
        if opening["kind"] == "window":
            windows_by_host[opening["host_boundary_id"]].append(opening)
    for oid, opening in openings.items():
        properties = apertures.get(oid, {})
        if opening["kind"] == "open":
            if properties:
                raise ValueError(f"an unfilled opening has no material or thickness: {oid}")
            open_results[oid] = {"component_ids": [], "material": None}
            continue
        kind = "glazing" if opening["kind"] == "window" else "door"
        cid = component_id(kind, oid)
        thickness = properties.get("thickness") or _adopt(kind, candidates)
        choices = _DOOR_MM if kind == "door" else candidates.get(kind, PRESETS_MM[kind])
        if thickness["source"] == "inference" and thickness["value_mm"] not in choices:
            raise ValueError(f"inferred thickness on {oid} must use a {kind} candidate")
        components[cid] = {"kind": kind, "opening_ids": [oid], "thickness": thickness}
        material = properties.get("material") or opening_material(opening, spaces, windows_by_host, boundaries)
        open_results[oid] = {"component_ids": [cid], "material": material}
    return {"material_scope": MATERIAL_SCOPE, "components": components, "boundaries": result, "openings": open_results}


def legacy_surface_properties(source):
    """Expose previously recorded thicknesses only. Do not fabricate annotations for archived runs."""
    source = source or {}
    readings = _legacy_readings(source, archive=True)
    components, boundaries = {}, {}
    scopes = [("wall" if row["kind"] == "wall" else "horizontal", row["boundary_ids"])
              for row in source.get("component_attributes", [])]
    scopes.extend(("wall", ref.get("boundary_ids", [ref["boundary_id"]]))
                  for ref in source.get("wall_references", []))
    for kind, scope in scopes:
        by_reading = defaultdict(list)
        for bid in sorted(scope):
            if bid in readings and bid not in boundaries:
                r = readings[bid]
                by_reading[r["value_mm"], r["source"], tuple(r["source_refs"])].append(bid)
        for owners in by_reading.values():
            cid = component_id(kind, [owners])
            components[cid] = {"kind": kind, "boundary_ids": owners, "thickness": readings[owners[0]]}
            for bid in owners:
                boundaries[bid] = {"component_ids": [cid], "material": None}
    return {"components": components, "boundaries": boundaries, "openings": {}}
