"""Candidate-bound spatial assessments and simulation properties for the offline handoff."""
from __future__ import annotations

from collections import Counter
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

from scripts.tool_scripts.bim_agent_inference import _source
from src.agent.execution.handoff_transaction import handoff_writer
from src.agent.geometry.sim_components import MATERIAL_SCOPE
from src.agent.geometry.sim_component_topology import Plane, physical_families
from src.agent.geometry.source_bim import EPS, _parts


POLICY = "object_evidence_v1"
SCHEMA = "sim_inspection_v1"
KINDS = {"space": "spaces", "boundary": "boundaries", "opening": "openings"}
_FILTERS = {"ids", "space_ids", "floor_ids", "type", "physical_only", "missing_spatial"}
_REVISION_SPATIAL_HINT = (
    'Add missing_spatial:true to a spatial rule in finish_bim or record_sim_inspection. '
    'It selects only objects still without a spatial grade. Combine it with floor_ids, space_ids, ids or type '
    'when evidence differs. Use a shared grade and reason only if the same evidence applies to every match.')
_IDENTITIES = {
    "spaces": ("id", "floor_id", "z_floor", "height", "polygon", "holes", "vertical_sections"),
    "boundaries": ("id", "space_id", "geometry_type", "kind", "enclosure", "enclosure_regions",
                   "vertices", "holes", "counterpart_ids", "adjacent_space_ids"),
    "openings": ("id", "kind", "space_ids", "vertices", "host_boundary_id"),
}


def _empty():
    return {kind: {} for kind in KINDS.values()}


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write(path, raw):
    """Stage each artifact on its destination volume before replacing it."""
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(raw)
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _write_json(path, value):
    _write(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode())


def _inventory(source):
    return {key: {row["id"]: row for row in source[key]} for key in KINDS.values()}


def _evidence_resolver(toolkit):
    manifest = toolkit.manifest

    def resolve(ref):
        label, _, location = ref.partition("#")
        # Original filenames may themselves contain a hash.
        if ref.startswith("image:") and "#" in ref:
            for name in sorted(manifest.get("images", {}), key=len, reverse=True):
                if ref == f"image:{name}" or ref.startswith(f"image:{name}#"):
                    label = f"image:{name}"
                    location = ref[len(label) + 1:]
                    break
        if len(ref) > len(label) and not location.strip():
            raise ValueError("An evidence locator after # must not be blank")
        return resolve_label(label)

    @lru_cache(maxsize=None)
    def resolve_label(label):
        if label == "brief" and str(manifest.get("scope", "")).strip():
            return "user"
        prefix, sep, key = label.partition(":")
        if not sep or not key:
            raise ValueError(f"Unknown evidence reference {label!r}")
        if prefix == "block":
            if any(block.get("id") == key and str(block.get("caption", "")).strip()
                   for block in manifest.get("blocks", [])):
                return "user"
        elif prefix == "revision":
            evidence = manifest.get("revisionEvidence", {})
            note = evidence.get(key) if isinstance(evidence, dict) else None
            if isinstance(note, str) and note.strip():
                return "user"
        elif prefix == "input":
            if key.startswith("/") and isinstance(manifest.get("building_input"), dict):
                value = manifest["building_input"].get("declaration")
                for token in key[1:].split("/"):
                    part = token.replace("~1", "/").replace("~0", "~")
                    if isinstance(value, list) and re.fullmatch(r"0|[1-9]\d*", part) and int(part) < len(value):
                        value = value[int(part)]
                    elif isinstance(value, dict) and part in value:
                        value = value[part]
                    else:
                        raise ValueError(f"Unresolved building input pointer {key}")
                if value is not None:
                    return "user"
        elif prefix == "image":
            if key in manifest.get("images", {}):
                toolkit.image_path(key)  # verifies the frozen image's actual digest
                return "material"
        elif prefix == "view" and re.fullmatch(r"view_\d{4,}", key):
            if toolkit.read_image_view(key)["record"].get("view_id") == key:
                return "material"
        elif prefix in {"mesh", "section"}:
            mesh = manifest.get("mesh_input") or {}
            expected = mesh.get("sha256")
            if expected and mesh_digest() == expected:
                if prefix == "mesh" and key == "original":
                    return "material"
                folder = "mesh_observations" if prefix == "mesh" else "mesh_sections"
                if re.fullmatch(rf"{prefix}_\d+", key):
                    path = toolkit.run / folder / f"{key}.json"
                    if path.is_file() and json.loads(path.read_bytes()).get("mesh_sha256") == expected:
                        return "material"
        raise ValueError(f"Unresolved evidence reference {label!r}; cite an admitted input or its saved view")

    @lru_cache(maxsize=1)
    def mesh_digest():
        return _digest(toolkit.run / "assets" / "input.glb")

    return resolve


def _refs(row, resolve, required=False):
    refs = row.get("source_refs", [])
    if not isinstance(refs, list) or any(not isinstance(r, str) or not r.strip() for r in refs):
        raise ValueError("source_refs must contain evidence reference strings")
    if required and not refs:
        raise ValueError("This assessment needs evidence from an admitted input or its saved view")
    return {resolve(ref) for ref in refs}


def _check_attribute_evidence(name, attribute, resolve):
    keys = {"en", "zh", "source"} if name == "material" else {"value_mm", "source"}
    shape = ", ".join(sorted(keys))
    if not isinstance(attribute, dict):
        raise ValueError(f"{name} must be an object with {shape} and optional source_refs. "
                         "Remove this field if it does not apply. Use physical_only:true on the "
                         "construction rule instead of trying to cancel it with null.")
    missing, unknown = keys - attribute.keys(), attribute.keys() - (keys | {"source_refs"})
    if missing or unknown:
        raise ValueError(f"{name} expects {shape} and optional source_refs; "
                         f"missing {sorted(missing)}, unexpected {sorted(unknown)}")
    origin = attribute.get("source")
    kinds = _refs(attribute, resolve, required=origin in {"from user", "from material"})
    if origin == "from user" and "user" not in kinds:
        raise ValueError("from user requires a brief, block caption, building declaration or confirmed revision reference")
    if origin == "from material" and "material" not in kinds:
        raise ValueError("from material requires an admitted drawing, mesh or material view")


def _spatial(value, resolve):
    if not isinstance(value, dict) or set(value) - {"grade", "reason", "source_refs"}:
        raise ValueError("spatial requires grade, bilingual reason and source_refs")
    grade, reason = value.get("grade"), value.get("reason")
    if type(grade) is not int or not 1 <= grade <= 5:
        raise ValueError("Spatial grade must be an integer from 1 to 5")
    if not isinstance(reason, dict) or set(reason) != {"en", "zh"} or any(
            not isinstance(v, str) or not v.strip() or len(v.strip()) > 400 for v in reason.values()):
        raise ValueError("Supply a short, nonempty public spatial reason in en and zh (up to 400 characters each)")
    _refs(value, resolve, required=grade > 1)
    return {"grade": grade, "reason": {lang: message.strip() for lang, message in reason.items()},
            "source_refs": value.get("source_refs", [])}


def _attribute_sources(fields, resolve):
    for key in ("material", "thickness"):
        if key in fields:
            _check_attribute_evidence(key, fields[key], resolve)
    for region in fields.get("thickness_regions", ()):
        _check_attribute_evidence("thickness", region["thickness"], resolve)


def _selected(rule, group, inventory, source, physical_ids, declarations):
    selectors = _FILTERS.intersection(rule)
    if "all" in rule:
        if selectors:
            raise ValueError(f"Remove 'all' to keep the filters {sorted(selectors)}. "
                             "Use all:true alone only when the rule applies to every object of its kind.")
        if rule["all"] is not True:
            raise ValueError("The unfiltered selector must be all:true.")
        return list(inventory[group])
    if not selectors:
        raise ValueError("Every rule needs an explicit object selector")
    if "physical_only" in selectors and (rule["physical_only"] is not True or group == "spaces"):
        raise ValueError("physical_only:true selects boundaries with solid area or filled openings")
    filters = {}
    for field, valid in (("ids", inventory[group]), ("space_ids", inventory["spaces"]),
                         ("floor_ids", {f["id"] for f in source["floors"]})):
        if field in rule:
            values = rule[field]
            if (not isinstance(values, list) or not values or
                    any(not isinstance(v, str) or v not in valid for v in values)):
                raise ValueError(f"{field} must contain existing source IDs")
            filters[field] = set(values)
    selected = []
    for identity, row in inventory[group].items():
        if "missing_spatial" in selectors and "spatial" in declarations.get(identity, {}):
            continue
        if "physical_only" in selectors and identity not in physical_ids(group):
            continue
        if "ids" in filters and identity not in filters["ids"]:
            continue
        owners = ({identity} if group == "spaces" else {row["space_id"]} if group == "boundaries"
                  else set(row.get("space_ids", ())))
        if "space_ids" in filters and owners.isdisjoint(filters["space_ids"]):
            continue
        if "floor_ids" in filters and not any(inventory["spaces"][s]["floor_id"] in filters["floor_ids"]
                                               for s in owners):
            continue
        if "type" in rule and rule["type"] != row.get({"boundaries": "geometry_type",
                                                      "spaces": "role", "openings": "kind"}[group]):
            continue
        selected.append(identity)
    if not selected and "missing_spatial" not in selectors:
        raise ValueError(f"No {group} match the supplied selector")
    return selected


def apply_rules(toolkit, source, records, packet):
    """Apply ordered selector rules to a detached working copy. Later fields override earlier fields."""
    if not isinstance(packet, dict) or set(packet) != {"rules"} or not isinstance(packet["rules"], list):
        raise ValueError("inspection_json must be an object with a rules list; read get_bim_reference('sim_inspection')")
    inventory, resolve = _inventory(source), _evidence_resolver(toolkit)
    @lru_cache(maxsize=2)
    def physical_ids(group):
        if group == "openings":
            return {key for key, row in inventory[group].items() if row["kind"] != "open"}
        return {bid for family, _, _, _ in physical_families(source, inventory["boundaries"]) for bid in family}

    values = {group: {i: dict(fields) for i, fields in rows.items()} for group, rows in records.items()}
    for index, rule in enumerate(packet["rules"]):
        try:
            if not isinstance(rule, dict) or rule.get("kind") not in KINDS:
                raise ValueError("kind must be space, boundary or opening")
            group = KINDS[rule["kind"]]
            attrs = set(rule) - {"kind", "all"} - _FILTERS
            if "missing_spatial" in rule:
                if not toolkit.manifest.get("model_revision") or rule["missing_spatial"] is not True:
                    raise ValueError("missing_spatial:true is available only when editing a delivered model.")
                if attrs != {"spatial"}:
                    raise ValueError("A missing_spatial:true rule may set only spatial.")
            allowed = {"spatial"} if group == "spaces" else {"spatial", "material", "thickness"}
            if group == "boundaries":
                allowed.add("thickness_regions")
            if not attrs or attrs - allowed:
                raise ValueError(f"Unsupported assessment attributes for {group}: {attrs - allowed}")
            fields = {k: rule[k] for k in attrs}
            if "spatial" in fields:
                fields["spatial"] = _spatial(fields["spatial"], resolve)
            _attribute_sources(fields, resolve)
            selected = _selected(rule, group, inventory, source, physical_ids, values[group])
            if attrs - {"spatial"}:
                excluded = [key for key in selected if key not in physical_ids(group)]
                if excluded:
                    why = "boundaries with no physical area" if group == "boundaries" else "unfilled openings"
                    raise ValueError(f"Construction fields include {len(excluded)} {why}: {', '.join(excluded[:8])}. "
                                     "Assess their spatial evidence separately. For material/thickness on the "
                                     "remaining targets, remove 'all' and add physical_only:true to the same filters. "
                                     "Omit any null material/thickness overrides.")
            for identity in selected:
                values[group].setdefault(identity, {}).update(fields)
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise ValueError(f"inspection rule[{index}]: {exc}") from exc
    return values


def _load(source, directory, *, draft=False):
    paths = ([directory / "sim_inspection_draft.json"] if draft else []) + [directory / "sim_inspection.json"]
    for path in paths:
        if path.is_file():
            record = json.loads(path.read_bytes())
            if (record.get("schema_version") != SCHEMA or
                    record.get("source_model_sha256") != source["source_model_sha256"]):
                raise ValueError("Inspection is bound to a different source model")
            declarations = record["declarations"]
            # Full inspections declare material semantics in properties. Partial drafts
            # have no computed properties and carry the same marker directly.
            if record.get("properties", record).get("material_scope") != MATERIAL_SCOPE:
                for group in ("boundaries", "openings"):
                    for fields in declarations[group].values():
                        old = {k: fields.pop(k) for k in ("material", "materials_by_host") if k in fields}
                        if old:
                            fields["legacy_surface_material"] = old
            return declarations
    return _empty()


def _object_shapes(source):
    inventory = _inventory(source)
    shapes = {group: {k: {f: row[f] for f in _IDENTITIES[group] if f in row}
                       for k, row in rows.items()} for group, rows in inventory.items()}
    # Moving a hosted aperture changes the spatial evidence about the face's net extent.
    for opening in source["openings"]:
        hosts = source.get("opening_hosts", {}).get(opening["id"], ())
        shapes["openings"][opening["id"]]["hosts"] = hosts
        for bid in hosts:
            shapes["boundaries"][bid].setdefault("apertures", {})[opening["id"]] = opening["vertices"]
    for contact in source.get("boundary_relations", ()):
        left, right = contact["boundary_ids"]
        for bid, other in ((left, right), (right, left)):
            shapes["boundaries"][bid].setdefault("contacts", {})[other] = contact["regions"]
    # The compiler can hold coordinate tuples before JSON export; their saved form is a list.
    return json.loads(json.dumps(shapes))


def _carried_construction(group, before, after, fields):
    """Keep properties of a surviving construction, independently of spatial confidence."""
    carried = {k: v for k, v in fields.items() if k != "spatial"}
    if not carried or group == "spaces":
        return {}
    if group == "openings":
        # The same aperture can be resized. Changed kind, owners or hosts require new evidence.
        identity = ("kind", "space_ids", "host_boundary_id", "hosts")
        return carried if all(before.get(k) == after.get(k) for k in identity) else {}
    identity = ("geometry_type", "space_id", "kind", "enclosure")
    if any(before.get(k) != after.get(k) for k in identity):
        return {}
    old_plane, plane = Plane(before), Plane(after)
    dx = dy = 0.0
    if plane.direction is None:
        # Moving a room partition can change the adjacent room above/below a slab.
        # Its own space, face type, elevation and overlap still identify the same construction.
        if abs(old_plane.origin[2] - plane.origin[2]) > EPS:
            return {}
    else:
        if (math.dist(old_plane.direction, plane.direction) > EPS
                or set(before.get("adjacent_space_ids", [])) != set(after.get("adjacent_space_ids", []))
                or set(before.get("counterpart_ids", [])) != set(after.get("counterpart_ids", []))):
            return {}
        nx, ny = -plane.direction[1], plane.direction[0]
        distance = ((plane.origin[0] - old_plane.origin[0]) * nx
                    + (plane.origin[1] - old_plane.origin[1]) * ny)
        dx, dy = nx * distance, ny * distance

    def shifted(row):
        if abs(dx) <= EPS and abs(dy) <= EPS:
            return row
        def ring(points):
            return [[x + dx, y + dy, z] for x, y, z in points]
        return {**row, "vertices": ring(row["vertices"]), "holes": [ring(h) for h in row.get("holes", [])]}

    host = plane.shape(after)
    if host.intersection(plane.shape(shifted(before))).area <= EPS ** 2:
        return {}
    if "thickness_regions" in carried:
        regions = []
        for old_region in carried.pop("thickness_regions"):
            region = shifted(old_region)
            shape = plane.shape(region)
            if host.buffer(EPS).covers(shape):
                regions.append(region)
            else:
                for part in _parts(host.intersection(shape)):
                    regions.append({"vertices": plane.lift(part.exterior), "holes": [plane.lift(h) for h in part.interiors],
                                    "thickness": region["thickness"]})
        if regions:
            carried["thickness_regions"] = regions
    return carried


def inherited_records(toolkit, source, parent):
    records = _empty()
    if parent:
        directory = toolkit.candidate_path(parent)
        if (directory / "sim_inspection.json").exists() or (directory / "sim_inspection_draft.json").exists():
            old, _, _ = _source(toolkit, parent)
            for key in ("mesh_sha256", "translation_m", "yaw_degrees"):
                if (source.get("mesh_frame") or {}).get(key) != (old.get("mesh_frame") or {}).get(key):
                    return records
            old_shapes, current_shapes = _object_shapes(old), _object_shapes(source)
            for group, rows in _load(old, directory, draft=True).items():
                for identity, fields in rows.items():
                    before = old_shapes[group].get(identity)
                    after = current_shapes[group].get(identity)
                    if before is None or after is None:
                        continue
                    if before == after:
                        records[group][identity] = fields
                        continue
                    if toolkit.manifest.get("model_revision"):
                        retained = _carried_construction(group, before, after, fields)
                        if retained:
                            records[group][identity] = retained
    return records


def snapshot(toolkit, source, records, *, complete=False, check_evidence=False):
    from src.agent.geometry.sim_components import build_surface_properties
    admitted = source.get("generation", {}).get("provenance", {}).get("input_manifest_sha256")
    if admitted and _digest(toolkit.run / "inputs.json") != admitted:
        raise ValueError("Candidate was built against a different inputs.json")
    spatial = {group: {k: values["spatial"] for k, values in rows.items() if "spatial" in values}
               for group, rows in records.items()}
    if complete:
        missing = {g: [r["id"] for r in source[g] if r["id"] not in spatial[g]] for g in KINDS.values()}
        if any(missing.values()):
            scope = "; ".join(f"{k}: {len(v)} missing (e.g. {', '.join(v[:2])})" for k, v in missing.items() if v)
            if toolkit.manifest.get("model_revision"):
                raise ValueError(f"Spatial coverage incomplete: {scope}. {_REVISION_SPATIAL_HINT}")
            raise ValueError(f"Spatial coverage incomplete: {scope}. Submit inspection_json rules naming actual "
                             "geometry evidence or explicitly reasoned assumptions. Use kind+all:true for a "
                             "shared basis and override exceptions with floor_ids/space_ids/ids/type. "
                             "See get_bim_reference('sim_inspection').")
    if check_evidence:
        resolve = _evidence_resolver(toolkit)
        for group, rows in records.items():
            if rows.keys() - {r["id"] for r in source[group]}:
                raise ValueError(f"Inspection contains stale {group} IDs")
            for fields in rows.values():
                if "spatial" in fields:
                    _spatial(fields["spatial"], resolve)
                _attribute_sources(fields, resolve)
    separate = {"spatial", "legacy_surface_material"}
    attrs = {group: {k: {f: v for f, v in fields.items() if f not in separate}
                     for k, fields in records[group].items() if fields.keys() - separate}
             for group in ("boundaries", "openings")}
    props = build_surface_properties(source, toolkit.manifest.get("thickness_candidates_mm"), attrs)
    return {"schema_version": SCHEMA, "source_model_sha256": source["source_model_sha256"],
            "declarations": records, "spatial": spatial, "properties": props}


def initial_snapshot(toolkit, source, parent):
    return snapshot(toolkit, source, inherited_records(toolkit, source, parent))


def _summary(source, assessment):
    return {k: {"assessed": len(rows), "total": len(source[k]),
                "grades": dict(Counter(v["grade"] for v in rows.values()))}
            for k, rows in assessment["spatial"].items()}


def revision_spatial_feedback(source, assessment):
    coverage = _summary(source, assessment)
    selectors = []
    for kind, group in KINDS.items():
        counts = coverage[group]
        counts["missing"] = counts["total"] - counts["assessed"]
        if counts["missing"]:
            selectors.append({"kind": kind, "missing_spatial": True})
    feedback = {"coverage": coverage}
    if selectors:
        feedback.update(spatial_selectors=selectors, spatial_rule_help=_REVISION_SPATIAL_HINT)
    return feedback


def record_inspection(toolkit, candidate, text):
    with handoff_writer(toolkit.run):
        source, path, _ = _source(toolkit, candidate)
        records = apply_rules(toolkit, source, _load(source, path.parent, draft=True), json.loads(text))
        assessment = snapshot(toolkit, source, records)
        _write_json(path.parent / "sim_inspection_draft.json", {
            "schema_version": SCHEMA, "material_scope": MATERIAL_SCOPE,
            "source_model_sha256": source["source_model_sha256"], "declarations": records})
        feedback = (revision_spatial_feedback(source, assessment) if toolkit.manifest.get("model_revision")
                    else {"coverage": _summary(source, assessment)})
        return {"candidate": candidate, "source_model_sha256": source["source_model_sha256"], **feedback}


def finish_inspection(toolkit, candidate, text):
    """Called under the finish lock; replace the final viewer before its digest is attested."""
    directory = toolkit.candidate_path(candidate)
    required = toolkit.manifest.get("spatial_assessment_policy") == POLICY
    if not (required or toolkit.manifest.get("model_revision") or text or (directory / "sim_inspection_draft.json").exists()
            or (directory / "sim_inspection.json").exists()):
        return False
    from scripts.tool_scripts.render_geometry_viewer import build_viewer_html
    from src.agent.geometry.source_bim import source_view_geometry

    source, _, _ = _source(toolkit, candidate)
    records = _load(source, directory, draft=True)
    if text:
        records = apply_rules(toolkit, source, records, json.loads(text))
    assessment = snapshot(toolkit, source, records, complete=required, check_evidence=True)
    if toolkit.manifest.get("model_revision"):
        from src.agent.model_revision import require_revision_change
        require_revision_change(toolkit, source, assessment)
    display = source_view_geometry(source)
    display["sim_inspection"] = assessment
    mesh_path = toolkit.run / "assets" / "input.glb" if source.get("mesh_frame") else None
    html = build_viewer_html(display, mesh_path=mesh_path, locale=toolkit.manifest.get("locale", "en"),
                             revision_mesh_cache=toolkit.revision_mesh_cache)
    _write_json(directory / "sim_inspection_draft.json", {
        "schema_version": SCHEMA, "material_scope": MATERIAL_SCOPE,
        "source_model_sha256": source["source_model_sha256"], "declarations": records})
    _write_json(directory / "sim_inspection.json", assessment)
    _write(directory / "viewer.html", html.encode())
    return True
