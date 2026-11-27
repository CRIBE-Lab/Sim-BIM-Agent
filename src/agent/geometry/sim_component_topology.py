"""Resolve physical boundary patches from the source BIM's positive-area contacts."""
from collections import defaultdict
from hashlib import sha256
import json

from shapely import STRtree, set_precision
from shapely.geometry import Polygon
from shapely.ops import unary_union

from .source_bim import EPS, _parts
from .surfaces import lift_from_wall, project_to_wall, wall_frame


class ContactGroups:
    def __init__(self, ids):
        self.roots = {bid: bid for bid in ids}

    def find(self, bid):
        while bid != self.roots[bid]:
            self.roots[bid] = self.roots[self.roots[bid]]
            bid = self.roots[bid]
        return bid

    def join(self, first, second):
        a, b = sorted([self.find(first), self.find(second)])
        self.roots[b] = a


def component_id(kind, identity):
    data = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"{kind}-{sha256(data).hexdigest()[:24]}"


class Plane:
    """A common projection for already related source faces, with a reversible world frame."""
    def __init__(self, boundary):
        vertices = boundary["vertices"]
        self.origin = vertices[0]
        self.direction = None
        if boundary["geometry_type"] == "wall":
            self.direction, _ = wall_frame(self.origin[:2], vertices[1][:2])

    def ring(self, vertices):
        if self.direction is not None:
            shape = project_to_wall(vertices, self.origin[:2], self.direction)
        elif all(abs(p[2] - self.origin[2]) <= EPS for p in vertices):
            shape = Polygon([p[:2] for p in vertices])
        else:
            shape = None
        if shape is None or not shape.is_valid or shape.area <= EPS ** 2:
            raise ValueError("invalid or non-coplanar physical component polygon")
        return set_precision(shape, EPS)

    def shape(self, row):
        outer = self.ring(row["vertices"])
        if row.get("holes"):
            return outer.difference(unary_union([self.ring(ring) for ring in row["holes"]]))
        return outer

    def lift(self, ring):
        coords = list(ring.coords)[:-1]
        if self.direction is not None:
            return lift_from_wall(coords, self.origin[:2], self.direction)
        return [[x, y, self.origin[2]] for x, y in coords]

    def regions(self, shape, cid):
        return [{"component_id": cid, "vertices": self.lift(p.exterior),
                 "holes": [self.lift(r) for r in p.interiors]} for p in _parts(shape)]


class FloorsAbove:
    def __init__(self, boundaries):
        self.heights, self.shapes = [], []
        for boundary in boundaries.values():
            if boundary["geometry_type"] == "floor":
                self.heights.append(boundary["vertices"][0][2])
                self.shapes.append(Plane(boundary).shape(boundary))
        self.index = STRtree(self.shapes)

    def split_ceiling(self, shape, z):
        # An upper volume separated by an unmodeled gap does not make a continuous slab.
        # It does, however, preclude inferring a sky-facing roof under its footprint.
        above = [self.shapes[i] for i in self.index.query(shape, predicate="intersects")
                 if self.heights[i] > z + EPS]
        covered = shape.intersection(unary_union(above)) if above else Polygon()
        return [("horizontal", covered), ("roof", shape.difference(covered))]


def _positive_parts(shape):
    # Ignore numerical line slivers generated at a shared ring, retaining real narrow faces.
    return [p for p in _parts(shape) if p.buffer(-EPS / 2).area > 0]


def _relation_sets(boundaries, relations):
    candidates = ContactGroups(boundaries)
    eligible = []
    for rel in relations:
        a, b = rel["boundary_ids"]
        if a in boundaries and b in boundaries and rel.get("regions"):
            eligible.append(rel)
            candidates.join(a, b)
    by_group, faces = defaultdict(list), defaultdict(list)
    for rel in eligible:
        by_group[candidates.find(rel["boundary_ids"][0])].append(rel)
    for bid in sorted(boundaries):
        faces[candidates.find(bid)].append(bid)
    return [(ids, by_group[key]) for key, ids in faces.items()]


def physical_families(source, boundaries):
    """Yield (connected hosts, kind, plane, [(owner IDs, polygon)]) for shared constructions.

    A shared patch is emitted once with both owners. Only *documented contacts with actual
    solid overlap* join assemblies. Floor names, equal elevations and footprints alone
    never establish continuity.
    """
    ceilings = FloorsAbove(boundaries)
    apertures = defaultdict(list)
    for opening in source.get("openings", []):
        for bid in source.get("opening_hosts", {}).get(opening["id"], [opening["host_boundary_id"]]):
            apertures[bid].append(opening)
    eligible = {bid: b for bid, b in boundaries.items()
                if b.get("kind") == "physical" and b.get("enclosure") not in {"open", "unknown"}}
    for ids, relations in _relation_sets(eligible, source.get("boundary_relations", [])):
        plane = Plane(eligible[ids[0]])
        solids = {}
        for bid in ids:
            boundary = eligible[bid]
            cuts = [plane.shape(o) for o in apertures[bid]]
            cuts.extend(plane.shape(r) for r in boundary.get("enclosure_regions", [])
                        if r["condition"] in {"open", "unknown"})
            base = plane.shape(boundary)
            solids[bid] = base.difference(unary_union(cuts)) if cuts else base
        contacts = defaultdict(list)
        families, patches = ContactGroups(ids), []
        for rel in relations:
            a, b = sorted(rel["boundary_ids"])
            shared = unary_union([plane.shape(r) for r in rel["regions"]])
            shape = shared.intersection(solids[a]).intersection(solids[b])
            for patch in _positive_parts(shape):
                contacts[a].append(patch)
                contacts[b].append(patch)
                families.join(a, b)
                kind = "wall" if eligible[a]["geometry_type"] == "wall" else "horizontal"
                patches.append((kind, (a, b), patch))
        for bid in ids:
            clips = contacts[bid]
            rest = solids[bid].difference(unary_union(clips)) if clips else solids[bid]
            surface_type = eligible[bid]["geometry_type"]
            for patch in _positive_parts(rest):
                if surface_type == "ceiling":
                    residuals = ceilings.split_ceiling(patch, plane.origin[2])
                else:
                    residuals = [({"floor": "horizontal", "wall": "wall", "roof": "roof"}[surface_type], patch)]
                for kind, residual in residuals:
                    patches.extend((kind, (bid,), part) for part in _positive_parts(residual))
        grouped = defaultdict(list)
        for kind, hosts, patch in patches:
            grouped[(families.find(hosts[0]), kind)].append((hosts, patch))
        for (_, kind), chunks in grouped.items():
            hosts = sorted({bid for owners, _ in chunks for bid in owners})
            yield hosts, kind, plane, chunks
