"""Bind deliberately offset storey walls to a frozen Edit and the actual wall pair."""
from collections import Counter
import json
import math
import re

from shapely.geometry import Polygon

from src.agent.geometry.building_precision import _axis_line, NUMERICAL_M
from src.agent.geometry.proposal_edits import _translated_partition
from src.agent.geometry.source_model import _digest
from src.agent.model_revision import current_revision_evidence, north_axis_degrees


_MOVE = r"(?:平移|移动|偏移|挪动?|[东西南北]移|shift|move|translate|offset)"
_DISTANCE = re.compile(_MOVE + r"[^。；;，,\n]{0,75}?(?<![\d.])([+-]?\d*\.?\d+)\s*"
                       r"(毫米|厘米|公分|米|millimet(?:er|re)s?|centimet(?:er|re)s?|met(?:er|re)s?|mm|cm|m)"
                       r"(?![a-zA-Z²³\d])", re.IGNORECASE)
_CN_DIRECTION = r"(东北|东南|西南|西北|东|西|南|北)"
_DIRECTION = re.compile(r"(?:向|往|朝)\s*" + _CN_DIRECTION + "|" + _CN_DIRECTION + r"(?=移|挪)"
                        r"|\b(?:towards?|to the|due|move|shift|translate|offset)\s+(?:the\s+)?"
                        r"(northeast|southeast|southwest|northwest|east|west|south|north)(?:wards?)?\b", re.IGNORECASE)
_NEGATED_MOVE = re.compile(r"(?:不得|不要|禁止|不可|勿|避免|\bnever\b|\bnot\b|n['’]t\b)"
                           r"[^。；;.!?\n]{0,90}?" + _MOVE, re.IGNORECASE)
_COMPASS = ({"北", "north"}, {"东北", "northeast"}, {"东", "east"}, {"东南", "southeast"},
            {"南", "south"}, {"西南", "southwest"}, {"西", "west"}, {"西北", "northwest"})


def _wall_geometry(source, lines):
    ids = {identity for line in lines for identity in line["boundary_ids"]}
    return _digest({row["id"]: {key: row.get(key) for key in ("space_id", "geometry_type", "vertices",
                                                              "holes", "counterpart_ids")}
                    for row in source["boundaries"] if row["id"] in ids})


def _confirmed_reference(manifest, parent, operation, line, delta):
    revision = manifest["model_revision"]
    current = current_revision_evidence(revision)
    rooms = [operation["space_id"], operation["neighbor_space_id"]]
    public = parent.get("public_names", {}).get("spaces", {})
    for reference in operation.get("source_refs", []):
        key = reference.split("#", 1)[0].removeprefix("revision:")
        text = current.get(key)
        if (not reference.startswith("revision:") or not text or manifest["revisionEvidence"].get(key) != text
                or _NEGATED_MOVE.search(text)):
            continue
        targets = set()
        if "/notes/" in key:
            index = int(key.rsplit("/", 1)[1]) - 1
            targets = {t["id"] for t in revision["draft"]["notes"][index]["targets"]}
        if not targets.intersection(line["boundary_ids"]):
            if not re.search(r"墙|隔断|\b(?:wall|partition)\b", text, re.IGNORECASE):
                continue
            def named(room):
                return room in targets or any(
                    re.search(r"(?<![A-Za-z0-9_])" + re.escape(label) + r"(?![A-Za-z0-9_])", text)
                    for label in (room, public.get(room)) if label)
            if not all(named(room) for room in rooms):
                continue
        distances = []
        for amount, unit in _DISTANCE.findall(text):
            unit = unit.lower()
            factor = .001 if unit.startswith(("mm", "milli", "毫米")) else (
                .01 if unit.startswith(("cm", "centi", "厘米", "公分")) else 1)
            distances.append(math.isclose(float(amount) * factor, delta if amount.startswith(("+", "-"))
                                          else abs(delta), rel_tol=0, abs_tol=NUMERICAL_M))
        if not any(distances):
            continue
        directions = {(prefix or suffix or english).lower() for prefix, suffix, english in _DIRECTION.findall(text)}
        # The saved north axis is the clockwise bearing of model +Y from true north.
        model_bearing = (90 if line["axis"] == "x" else 0) + (180 if delta < 0 else 0)
        true_bearing = model_bearing + (north_axis_degrees(parent) or 0)
        sector = math.floor((true_bearing % 360 + 22.5) / 45) % 8
        if directions and not directions <= _COMPASS[sector]:
            continue
        return reference
    return None


def _new_confirmation(manifest, parent, source, operations, item):
    old_faces = {row["id"]: row for row in parent["boundaries"]}
    faces = {row["id"]: row for row in source["boundaries"]}
    old_rooms = {row["id"]: row for row in parent["spaces"]}
    rooms = {row["id"]: row for row in source["spaces"]}
    for changed, fixed in (item["lines"], reversed(item["lines"])):
        identifiers = changed["boundary_ids"]
        if len(identifiers) != 2 or not all(bid in old_faces for bid in identifiers):
            continue
        a, b = identifiers
        if any(b not in inventory[a]["counterpart_ids"] or a not in inventory[b]["counterpart_ids"]
               for inventory in (old_faces, faces)):
            continue
        old_line = (changed["axis"], fixed["coordinate_m"], *changed["span_m"])
        if not all(_axis_line(old_faces[bid]["vertices"]) == old_line for bid in identifiers):
            continue
        if any(old_faces.get(bid) != faces[bid] for bid in fixed["boundary_ids"]):
            continue
        owners = {faces[bid]["space_id"] for bid in identifiers}
        delta = changed["coordinate_m"] - fixed["coordinate_m"]
        for op in operations:
            if op.get("op") != "replace_space_region" or {op["space_id"], op["neighbor_space_id"]} != owners:
                continue
            ids = (op["space_id"], op["neighbor_space_id"])
            old = [Polygon(old_rooms[i]["polygon"]) for i in ids]
            new = [Polygon(rooms[i]["polygon"]) for i in ids]
            if not new[0].equals(Polygon(op["polygon"])) or _translated_partition(old, new) is None:
                continue
            ref = _confirmed_reference(manifest, parent, op, changed, delta)
            if ref:
                return {"lines": item["lines"], "floor_id": changed["floor_id"], "space_ids": list(ids),
                        "axis": changed["axis"], "delta_m": round(delta, 9), "source_ref": ref,
                        "wall_geometry_sha256": _wall_geometry(source, item["lines"]),
                        "parent_source_model_sha256": parent["source_model_sha256"]}
    return None


def apply_confirmed_offsets(report, records):
    """Only a numerically and topologically identical pair can inherit a prior confirmation."""
    skipped, remaining = [], []
    for item in report["items"]:
        proof = next((row for row in records if item["type"] == "storey_wall_offset"
                      and item.get("lines") == row["lines"]), None)
        if proof:
            skipped.append({**item, "disposition": "confirmed_partition_translation", "confirmation": proof})
        else:
            remaining.append(item)
    if not skipped:
        return report
    return {**report, "items": remaining, "skipped_items": [*report.get("skipped_items", []), *skipped],
            "total": len(remaining), "counts": dict(Counter(row["type"] for row in remaining)),
            "status": "rejected" if remaining else "pass"}


def bind_partition_offsets(toolkit, parent, source, operations, report):
    offsets = [row for row in report["items"] if row.get("type") == "storey_wall_offset"]
    if not parent or not offsets or not toolkit.manifest.get("model_revision"):
        return []
    previous = json.loads((toolkit.candidate_path(parent) / "source_model.json").read_bytes())
    existing = previous["generation"]["provenance"].get("confirmed_partition_offsets", [])
    confirmed = []
    for item in offsets:
        receipt = next((row for row in existing if row["lines"] == item["lines"]
                        and row.get("source_ref", "").partition("#")[0].removeprefix("revision:")
                        in toolkit.manifest["revisionEvidence"]
                        and row.get("wall_geometry_sha256") == _wall_geometry(source, item["lines"])), None)
        if receipt is None and operations:
            receipt = _new_confirmation(toolkit.manifest, previous, source, operations, item)
        if receipt:
            confirmed.append(receipt)
    return confirmed
