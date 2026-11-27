"""Public inspection data for the offline viewer, bound to a single source BIM."""
from __future__ import annotations

import math


_TAGS = {"from material", "inference", "from user"}


def _labels(value):
    if isinstance(value, dict) and all(isinstance(value.get(k), str) and value[k].strip() for k in ("en", "zh")):
        return {k: value[k] for k in ("en", "zh")}
    return None


def _thickness(value):
    number = value.get("value_mm") if isinstance(value, dict) else None
    if type(number) in (int, float) and math.isfinite(number) and number > 0:
        return {"value_mm": number, "source": value.get("source") if value.get("source") in _TAGS else None}
    return None


def _material(value):
    labels = _labels(value)
    if labels:
        labels["source"] = value.get("source") if value.get("source") in _TAGS else None
    return labels


def _ring(vertices):
    return isinstance(vertices, list) and len(vertices) >= 3 and all(
        isinstance(p, (tuple, list)) and len(p) == 3 and all(
            type(v) in (int, float) and math.isfinite(v) for v in p) for p in vertices)


def public_inspection(source, inspection):
    bound = bool(source.get("source_model_sha256") and isinstance(inspection, dict)
                 and inspection.get("schema_version") == "sim_inspection_v1"
                 and inspection.get("source_model_sha256") == source["source_model_sha256"])
    from src.agent.geometry.sim_components import MATERIAL_SCOPE, legacy_surface_properties

    result = {"spatial": {}, "properties": {"boundaries": {}, "openings": {}}}
    if not bound:
        inspection = {}
    props = inspection.get("properties")
    if props is None:
        props = legacy_surface_properties(source)
    has_main_material = props.get("material_scope") == MATERIAL_SCOPE
    components = props.get("components", {})
    for group in ("spaces", "boundaries", "openings"):
        ratings = result["spatial"][group] = {}
        for obj in source.get(group, ()):
            identity = obj["id"]
            evidence = inspection.get("spatial", {}).get(group, {}).get(identity) or {}
            grade, reason = evidence.get("grade"), _labels(evidence.get("reason"))
            if type(grade) is int and 1 <= grade <= 5 and reason:
                ratings[identity] = {"grade": grade, "reason": reason}
            if group == "spaces":
                continue
            face = props.get(group, {}).get(identity)
            if not isinstance(face, dict):
                continue
            material = face.get("material")
            thicknesses, indexes = [], {}
            component_ids = face.get("component_ids", [])
            for cid in component_ids:
                component = components.get(cid, {})
                thickness = _thickness(component.get("thickness"))
                if thickness:
                    if thickness not in thicknesses:
                        thicknesses.append(thickness)
                    indexes[cid] = thicknesses.index(thickness)
            item = {"material": _material(material) if has_main_material else None, "thicknesses": thicknesses,
                    "applicable": bool(component_ids or material)}
            if len(thicknesses) > 1:
                item["regions"] = [{"index": indexes[r["component_id"]], "vertices": r["vertices"],
                                    "holes": r.get("holes", [])} for r in face.get("regions", ())
                                   if r.get("component_id") in indexes and _ring(r.get("vertices"))
                                   and all(_ring(hole) for hole in r.get("holes", ()))]
            result["properties"][group][identity] = item
    return result
