"""Public model quantities in metres, using the represented space volumes."""
from __future__ import annotations

from collections import defaultdict
import math

from shapely.errors import ShapelyError
from shapely.ops import unary_union

from .source_volume import section_polygon, space_sections


def _interval(row):
    bottom, height = row.get("z_floor"), row.get("height")
    if (type(bottom) in (int, float) and type(height) in (int, float)
            and height > 0 and math.isfinite(bottom) and math.isfinite(bottom + height)):
        return bottom, bottom + height
    return None


def _area_shape(row):
    if not row.get("polygon"):
        return None
    try:
        shape = section_polygon(row)
        if shape.is_valid and math.isfinite(shape.area) and shape.area > 0:
            return shape
    except (ShapelyError, TypeError, ValueError):
        pass
    return None


def _space_layers(space, projection):
    """A source space is a prism or an ordered sequence of horizontal prisms."""
    interval = _interval(space)
    if not interval or projection is None:
        return []
    if not space.get("vertical_sections"):
        return [(*interval, projection)]
    layers = []
    previous = interval[0]
    for section in space_sections(space):
        bottom, top = section["z"]
        shape = _area_shape(section)
        if bottom != previous or top <= bottom or not math.isfinite(top) or shape is None:
            return []
        layers.append((bottom, top, shape))
        previous = top
    return layers if previous == interval[1] else []


def _floor_quantities(layers):
    """Integrate the union at every change in section, including overlapping XY projections."""
    levels = sorted({z for bottom, top, _ in layers for z in (bottom, top)})
    if len(levels) == 2:
        area = unary_union([shape for _, _, shape in layers]).area
        return {"area_m2": area, "volume_m3": area * (levels[1] - levels[0])}
    projections = []
    volume = 0.0
    for low, high in zip(levels, levels[1:]):
        active = [shape for bottom, top, shape in layers if bottom <= low and top >= high]
        if active:
            section = unary_union(active)
            projections.append(section)
            volume += section.area * (high - low)
    return {"area_m2": unary_union(projections).area, "volume_m3": volume}


def viewer_metrics(source: dict | None) -> dict:
    """Return projected area and modeled volume keyed by source space and floor IDs.

    Space polygons are the full XY projections of their sections, including holes.
    A floor covers its local spaces and explicitly referenced spanning spaces within
    its vertical interval. A full-height owning group is also independently selectable.
    Floor groups can therefore overlap and should not be summed into building totals.
    Wall apertures describe boundaries and do not subtract from the space volume.
    """
    result = {"spaces": {}, "floors": {}}
    if not source or (source.get("coordinate_system") or {}).get("units", "m") != "m":
        return result
    members = defaultdict(set)
    layers = {}
    for space in source.get("spaces", []):
        sid = space["id"]
        members[space.get("floor_id")].add(sid)
        projection = _area_shape(space)
        layers[sid] = _space_layers(space, projection)
        metrics = {}
        if projection is not None:
            metrics["area_m2"] = projection.area
        if layers[sid]:
            metrics["volume_m3"] = sum(shape.area * (top - bottom) for bottom, top, shape in layers[sid])
        result["spaces"][sid] = metrics

    for floor in source.get("floors", []):
        fid = floor["id"]
        selected = members[fid].union(floor.get("spanning_space_ids", []))
        metrics = {}
        result["floors"][fid] = metrics
        if "spaces" not in source or not selected.issubset(layers):
            continue
        metrics["space_count"] = len(selected)
        interval = _interval(floor)
        if not interval or not selected or not all(layers[sid] for sid in selected):
            continue
        low, high = interval
        clipped = [(max(low, bottom), min(high, top), shape)
                   for sid in sorted(selected) for bottom, top, shape in layers[sid]
                   if bottom < high and top > low]
        if clipped:
            metrics.update(_floor_quantities(clipped))
    return result
