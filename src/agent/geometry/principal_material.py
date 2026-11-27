"""Bilingual estimates of a component's main construction when no material was declared."""
import re

from .sim_component_topology import Plane


def _inference(en, zh):
    return {"en": en, "zh": zh, "source": "inference", "source_refs": []}


def boundary_material(kind, shared, roles):
    if kind == "roof":
        return _inference("A concrete slab or metal roof deck is plausible.", "屋面可能采用混凝土板或金属承板。")
    if kind == "horizontal":
        return _inference("A concrete floor slab is plausible.", "楼板可能采用混凝土。")
    if roles & {"shaft", "stairwell"}:
        return _inference("A concrete or masonry enclosure is plausible here.",
                          "此处围护墙可能采用混凝土或砌体。")
    if shared:
        return _inference("Masonry or a framed partition is plausible.", "隔墙可能采用砌体或骨架隔墙。")
    return _inference("The wall may be masonry or concrete.", "墙体可能采用砌体或混凝土。")


def contact_materials(hosts, declarations, kind, roles):
    """Use direct observations ahead of estimates within each actual contact patch."""
    stated = {bid: declarations[bid]["material"] for bid in hosts if "material" in declarations.get(bid, {})}
    direct = {bid: m for bid, m in stated.items() if m["source"] != "inference"}
    if direct:
        observed = next(iter(direct.values()))
        return {bid: direct.get(bid, observed) for bid in hosts}
    fallback = next(iter(stated.values()), None) or boundary_material(kind, len(hosts) > 1, roles)
    return {bid: stated.get(bid, fallback) for bid in hosts}


def material_across_patches(materials):
    """A description on a contact supports the opposite face only over that contact."""
    first = materials[0]
    if all(m == first for m in materials):
        return first
    if all((m["en"], m["zh"]) == (first["en"], first["zh"]) for m in materials):
        origins = {m["source"] for m in materials}
        return {**first, "source": first["source"] if len(origins) == 1 else "inference",
                "source_refs": sorted({ref for m in materials for ref in m["source_refs"]})}
    return _inference("A single primary material has not been identified across this surface.",
                      "各区域的主材尚未统一判明。")


_LIFT_LABEL = re.compile(
    r"(?<![a-z])(?:lifts?|elevators?|hoistways?)(?=$|[^a-z]|shaft|well)|电梯|電梯|升降机|升降機", re.I)


def _lift_shaft(space):
    if space.get("role") != "shaft":
        return False
    label = space["id"] + " " + (space.get("name") or "")
    label = re.sub(r"([a-z])([A-Z])", r"\1 \2", label)
    return bool(_LIFT_LABEL.search(label))


def _adjacent_glazing(opening, windows, host):
    """Limit a storefront inference to a nearby aperture with overlapping height."""
    if not windows:
        return False
    plane = Plane(host)
    door = plane.shape(opening)
    left, sill, right, head = door.bounds
    # These are true local metres. Allow at most a door width of solid wall in between.
    max_gap = min(right - left, 2.0)
    for window in windows:
        glass = plane.shape(window)
        _, bottom, _, top = glass.bounds
        if min(head, top) > max(sill, bottom) and glass.distance(door) <= max_gap:
            return True
    return False


def opening_material(opening, spaces, windows_by_host, boundaries):
    if opening["kind"] == "window":
        return _inference("Likely glass of undetermined type.", "可能为玻璃，具体类型未明。")
    connected = [spaces[sid] for sid in opening.get("space_ids", ()) if sid in spaces]
    if any(_lift_shaft(s) for s in connected):
        return _inference("Likely a metal lift landing door.", "电梯层门可能采用金属门扇。")
    roles = {s.get("role") for s in connected}
    if opening.get("exterior"):
        host = opening["host_boundary_id"]
        if roles & {"retail", "lobby"} and _adjacent_glazing(opening, windows_by_host[host], boundaries[host]):
            return _inference("A glazed entrance with metal framing is plausible.",
                              "结合入口旁的窗，可能采用金属框玻璃门。")
        return _inference("Exterior door of undetermined material.", "外门门扇材质未明。")
    if roles & {"electrical/mechanical", "stairwell"}:
        return _inference("A metal leaf is plausible at this access door.", "这处通行门可能采用金属门扇。")
    return _inference("Interior door of undetermined material.", "室内门扇材质未明。")
