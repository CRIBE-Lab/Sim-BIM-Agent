"""Preserve declared plan geometry and attachments during explicit alignment."""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any
from shapely.geometry import LineString, Point, Polygon
from src.agent.geometry_model.config import load_geometry_tolerances

MIN_JUNCTION_TOLERANCE_M = 0.05
MAX_JUNCTION_TOLERANCE_M = 0.30


def _finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _axis_scale(plan: Mapping[str, Any], axis: str) -> float:
    anchors = plan.get(f"{axis}_anchors")
    if (not isinstance(anchors, list) or len(anchors) != 2
            or any(not isinstance(row, list) or len(row) != 2 for row in anchors)):
        raise ValueError(f"numeric plan.{axis}_anchors are required for ink alignment")
    (p0, w0), (p1, w1) = anchors
    if not all(_finite(value) for value in (p0, w0, p1, w1)) or p0 == p1 or w0 == w1:
        raise ValueError(f"distinct numeric plan.{axis}_anchors are required for ink alignment")
    return abs((float(w1) - float(w0)) / (float(p1) - float(p0)))


def _junction_tolerance_pixels(metres_per_pixel: float) -> float:
    """Match the precompile join tolerance without applying its edits early."""

    return (min(MAX_JUNCTION_TOLERANCE_M,
                max(MIN_JUNCTION_TOLERANCE_M, 3.0 * metres_per_pixel))
            / metres_per_pixel)


def _segments(points: list[list[float]], *, closed: bool = False):
    pairs = list(zip(points, points[1:]))
    if closed and len(points) > 2:
        pairs.append((points[-1], points[0]))
    for index, (first, second) in enumerate(pairs):
        if first[0] == second[0]:
            yield index, "y", "x", float(first[0]), sorted((float(first[1]), float(second[1])))
        elif first[1] == second[1]:
            yield index, "x", "y", float(first[1]), sorted((float(first[0]), float(second[0])))


def _same_line(point: list[float], *, cross_index: int, cross_at: float, tolerance: float = 1e-6) -> bool:
    return len(point) == 2 and _finite(point[cross_index]) and abs(float(point[cross_index]) - cross_at) <= tolerance


def _orthogonal_ids(plan: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    partitions = set()
    openings = set()
    for partition in plan.get("partitions", []):
        points = partition.get("points", [])
        pairs = list(zip(points, points[1:])) if isinstance(points, list) else []
        if pairs and all(first[0] == second[0] or first[1] == second[1]
                         for first, second in pairs):
            partitions.add(str(partition.get("id", "?")))
    for opening in plan.get("openings", []):
        first, second = opening.get("p1"), opening.get("p2")
        if (isinstance(first, list) and isinstance(second, list)
                and len(first) == len(second) == 2
                and (first[0] == second[0] or first[1] == second[1])):
            openings.add(str(opening.get("id", "?")))
    return partitions, openings


def _footprint_edge_directions(plan: Mapping[str, Any]) -> dict[int, tuple[str, int, float]]:
    """Record every nonzero orthogonal perimeter edge's axis and direction."""

    ring = plan.get("footprint_pixels", [])
    if not isinstance(ring, list) or len(ring) < 3:
        return {}
    result: dict[int, tuple[str, int, float]] = {}
    for index, (first, second) in enumerate(zip(ring, [*ring[1:], ring[0]])):
        dx = float(second[0]) - float(first[0])
        dy = float(second[1]) - float(first[1])
        if abs(dx) <= 1e-9 and abs(dy) > 1e-9:
            result[index] = ("y", 1 if dy > 0 else -1, abs(dy))
        elif abs(dy) <= 1e-9 and abs(dx) > 1e-9:
            result[index] = ("x", 1 if dx > 0 else -1, abs(dx))
    return result


def _point_on_segment(point: list[float], first: list[float], second: list[float],
                      tolerance: float = 1e-5) -> bool:
    if first[0] == second[0]:
        return (abs(float(point[0]) - float(first[0])) <= tolerance
                and min(first[1], second[1]) - tolerance <= point[1]
                <= max(first[1], second[1]) + tolerance)
    if first[1] == second[1]:
        return (abs(float(point[1]) - float(first[1])) <= tolerance
                and min(first[0], second[0]) - tolerance <= point[0]
                <= max(first[0], second[0]) + tolerance)
    return False


def _wall_host_segments(plan: Mapping[str, Any]) -> dict[str, list[tuple[list[float], list[float]]]]:
    ring = plan.get("footprint_pixels", [])
    hosts = {"footprint": list(zip(ring, [*ring[1:], ring[0]])) if len(ring) >= 3 else []}
    for partition in plan.get("partitions", []):
        points = partition.get("points", [])
        hosts[f"partition:{partition.get('id', '?')}"] = list(zip(points, points[1:]))
    return hosts


def _attachment_requirements(plan: Mapping[str, Any]) -> tuple[
    dict[tuple[str, int], set[str]], dict[str, set[str]],
]:
    hosts = _wall_host_segments(plan)
    partition_ends: dict[tuple[str, int], set[str]] = {}
    for partition in plan.get("partitions", []):
        partition_id = str(partition.get("id", "?"))
        points = partition.get("points", [])
        for endpoint_index in (0, len(points) - 1):
            if len(points) < 2:
                continue
            endpoint = points[endpoint_index]
            attached = {
                host for host, segments in hosts.items()
                if host != f"partition:{partition_id}"
                and any(_point_on_segment(endpoint, first, second) for first, second in segments)
            }
            if attached:
                partition_ends[(partition_id, endpoint_index)] = attached
    opening_hosts: dict[str, set[str]] = {}
    for opening in plan.get("openings", []):
        first, second = opening.get("p1"), opening.get("p2")
        if not (isinstance(first, list) and isinstance(second, list)):
            continue
        attached = {
            host for host, segments in hosts.items()
            if any(_point_on_segment(first, start, end)
                   and _point_on_segment(second, start, end) for start, end in segments)
        }
        if attached:
            opening_hosts[str(opening.get("id", "?"))] = attached
    return partition_ends, opening_hosts


def _footprint_preserves_declarations(
    plan: Mapping[str, Any], *,
    orthogonal_partitions: set[str] | None = None,
    orthogonal_openings: set[str] | None = None,
    partition_attachments: dict[tuple[str, int], set[str]] | None = None,
    opening_hosts: dict[str, set[str]] | None = None,
    footprint_edge_directions: dict[int, tuple[str, int, float]] | None = None,
    footprint_axis_scales: Mapping[str, float] | None = None,
) -> tuple[bool, str | None]:
    ring = plan.get("footprint_pixels", [])
    if footprint_edge_directions:
        pairs = list(zip(ring, [*ring[1:], ring[0]])) if len(ring) >= 3 else []
        minimum_edge_m = load_geometry_tolerances().min_edge_length_m
        for edge_index, (axis, direction, original_length_px) in footprint_edge_directions.items():
            if edge_index >= len(pairs):
                return False, f"would remove footprint edge {edge_index}"
            first, second = pairs[edge_index]
            delta = (float(second[0]) - float(first[0]) if axis == "x"
                     else float(second[1]) - float(first[1]))
            perpendicular_delta = (float(second[1]) - float(first[1]) if axis == "x"
                                   else float(second[0]) - float(first[0]))
            if abs(perpendicular_delta) > 1e-9:
                return False, f"would make footprint edge {edge_index} non-orthogonal"
            if delta * direction <= 1e-9:
                return False, f"would collapse or reverse footprint edge {edge_index}"
            if footprint_axis_scales is not None:
                scale = float(footprint_axis_scales[axis])
                if (original_length_px * scale >= minimum_edge_m - 1e-9
                        and abs(delta) * scale < minimum_edge_m - 1e-9):
                    return False, (
                        f"would shrink footprint edge {edge_index} below "
                        f"min_edge_length_m {minimum_edge_m:.6f} m"
                    )
    try:
        footprint = Polygon(ring)
    except (TypeError, ValueError) as error:
        return False, f"moved footprint is invalid: {error}"
    if not footprint.is_valid or footprint.area <= 0 or footprint.interiors:
        return False, "moved footprint is not one valid simple outer ring"
    for partition in plan.get("partitions", []):
        partition_id = str(partition.get("id", "?"))
        points = partition.get("points", [])
        if (orthogonal_partitions is not None and partition_id in orthogonal_partitions
                and any(first[0] != second[0] and first[1] != second[1]
                        for first, second in zip(points, points[1:]))):
            return False, f"would make partition {partition_id} non-orthogonal"
        try:
            line = LineString(points)
        except (TypeError, ValueError):
            continue
        if not footprint.covers(line):
            return False, f"would put partition {partition.get('id', '?')} outside the footprint"
    for opening in plan.get("openings", []):
        opening_id = str(opening.get("id", "?"))
        first, second = opening.get("p1"), opening.get("p2")
        if (orthogonal_openings is not None and opening_id in orthogonal_openings
                and isinstance(first, list) and isinstance(second, list)
                and first[0] != second[0] and first[1] != second[1]):
            return False, f"would make opening {opening_id} non-orthogonal"
        try:
            line = LineString([first, second])
        except (TypeError, ValueError):
            continue
        if not footprint.covers(line):
            return False, f"would put opening {opening.get('id', '?')} outside the footprint"
    for seed in plan.get("space_seeds", []):
        try:
            point = Point(seed.get("point"))
        except (TypeError, ValueError):
            continue
        if not footprint.contains(point):
            return False, f"would put named room seed {seed.get('id', '?')} outside the footprint"
    hosts = _wall_host_segments(plan)
    partitions = {str(row.get("id", "?")): row for row in plan.get("partitions", [])}
    for (partition_id, endpoint_index), required_hosts in (partition_attachments or {}).items():
        partition = partitions.get(partition_id)
        if partition is None:
            return False, f"would remove attached partition {partition_id}"
        points = partition.get("points", [])
        if not points:
            return False, f"would remove the attached endpoint of partition {partition_id}"
        endpoint = points[endpoint_index]
        for host in required_hosts:
            if not any(_point_on_segment(endpoint, first, second)
                       for first, second in hosts.get(host, [])):
                return False, f"would detach partition {partition_id} from {host}"
    openings = {str(row.get("id", "?")): row for row in plan.get("openings", [])}
    for opening_id, required_hosts in (opening_hosts or {}).items():
        opening = openings.get(opening_id)
        if opening is None:
            return False, f"would remove hosted opening {opening_id}"
        first, second = opening.get("p1"), opening.get("p2")
        if not any(any(_point_on_segment(first, start, end)
                           and _point_on_segment(second, start, end)
                       for start, end in hosts.get(host, []))
                   for host in required_hosts):
            return False, f"would detach opening {opening_id} from its declared wall"
    return True, None


def move_straight_wall(plan: dict[str, Any], *, collection: str, identity: str,
                       along_axis: str, old_cross: float, new_cross: float,
                       span: list[float],
                       endpoint_tolerance_px: float = 1e-6) -> dict[str, list[str]]:
    """Move one wall line with objects attached or within junction tolerance."""

    # Reuse one float object for every moved coordinate.  Computing attached
    # coordinates as ``old + delta`` can round to a neighbouring binary float,
    # leaving a visually coincident opening or junction unequal to its host.
    new_cross = float(new_cross)
    cross_index = 1 if along_axis == "x" else 0
    along_index = 1 - cross_index
    moved_openings: list[str] = []
    moved_junctions: list[str] = []
    moved_walls: list[str] = []

    # A footprint edge can terminate at a collinear partition which continues
    # inward from the same corner.  Treat touching collinear declarations as
    # one connected wall line: moving only their shared endpoint would turn the
    # continuation diagonal.  Grow the affected span until the whole connected
    # collinear chain is known, then move every point on that chain together.
    effective_span = [float(span[0]), float(span[1])]
    footprint_point_indices: set[int] | None = None
    if collection == "footprint":
        ring = plan["footprint_pixels"]
        edge_index = int(identity)
        footprint_point_indices = {edge_index, (edge_index + 1) % len(ring)}
        # A perimeter side may be split by collinear vertices or repeated
        # points. Move that connected side atomically so changing one segment
        # cannot turn its neighbour diagonal.
        changed = True
        while changed:
            changed = False
            for first_index in range(len(ring)):
                second_index = (first_index + 1) % len(ring)
                if not ({first_index, second_index} & footprint_point_indices):
                    continue
                first, second = ring[first_index], ring[second_index]
                if (_same_line(first, cross_index=cross_index, cross_at=old_cross)
                        and _same_line(second, cross_index=cross_index, cross_at=old_cross)):
                    before = len(footprint_point_indices)
                    footprint_point_indices.update((first_index, second_index))
                    changed = changed or len(footprint_point_indices) != before
        along_values = [float(ring[index][along_index]) for index in footprint_point_indices]
        effective_span = [min(along_values), max(along_values)]
    connected: list[dict[str, Any]] = []
    pending = list(plan.get("partitions", []))
    while True:
        added = False
        for partition in pending:
            if partition in connected:
                continue
            if collection == "partitions" and str(partition.get("id")) == identity:
                continue
            points = partition.get("points", [])
            if (not isinstance(points, list) or len(points) < 2
                    or not all(isinstance(point, list) and len(point) == 2
                               and _finite(point[cross_index]) and _finite(point[along_index])
                               and abs(float(point[cross_index]) - old_cross) <= 1e-6
                               for point in points)):
                continue
            candidate_span = [min(float(point[along_index]) for point in points),
                              max(float(point[along_index]) for point in points)]
            if (candidate_span[1] < effective_span[0] - 1e-6
                    or candidate_span[0] > effective_span[1] + 1e-6):
                continue
            connected.append(partition)
            effective_span[0] = min(effective_span[0], candidate_span[0])
            effective_span[1] = max(effective_span[1], candidate_span[1])
            added = True
        if not added:
            break

    if collection == "partitions":
        row = next(item for item in plan.get("partitions", []) if item.get("id") == identity)
        for point in row["points"]:
            if _same_line(point, cross_index=cross_index, cross_at=old_cross):
                point[cross_index] = new_cross
    elif collection == "footprint":
        ring = plan["footprint_pixels"]
        assert footprint_point_indices is not None
        for point_index in footprint_point_indices:
            if _same_line(ring[point_index], cross_index=cross_index, cross_at=old_cross):
                ring[point_index][cross_index] = new_cross
    else:
        raise ValueError(f"unsupported wall collection {collection!r}")

    for partition in connected:
        for point in partition["points"]:
            point[cross_index] = new_cross
        moved_walls.append(str(partition.get("id", "?")))

    for opening in plan.get("openings", []):
        points = [opening.get("p1"), opening.get("p2")]
        if (all(isinstance(point, list) and _same_line(
                point, cross_index=cross_index, cross_at=old_cross) for point in points)
                and all(effective_span[0] - 1e-6 <= float(point[along_index])
                        <= effective_span[1] + 1e-6 for point in points)):
            opening["p1"][cross_index] = new_cross
            opening["p2"][cross_index] = new_cross
            moved_openings.append(str(opening.get("id", "?")))
        else:
            for field in ("p1", "p2"):
                point = opening.get(field)
                if (isinstance(point, list) and len(point) == 2
                        and _finite(point[cross_index]) and _finite(point[along_index])
                        and abs(float(point[cross_index]) - old_cross) <= 1e-6
                        and effective_span[0] - 1e-6 <= float(point[along_index])
                        <= effective_span[1] + 1e-6):
                    point[cross_index] = new_cross
                    moved_openings.append(f"{opening.get('id', '?')}:{field}")

    for partition in plan.get("partitions", []):
        if (collection == "partitions" and str(partition.get("id")) == identity
                or partition in connected):
            continue
        points = partition.get("points", [])
        for endpoint_index in (0, len(points) - 1):
            if len(points) < 2:
                continue
            point = points[endpoint_index]
            neighbor = points[1 if endpoint_index == 0 else len(points) - 2]
            if (isinstance(point, list) and len(point) == 2
                    and isinstance(neighbor, list) and len(neighbor) == 2
                    and _finite(point[cross_index]) and _finite(point[along_index])
                    and _finite(neighbor[cross_index]) and _finite(neighbor[along_index])
                    and abs(float(point[along_index]) - float(neighbor[along_index])) <= 1e-6
                    and abs(float(point[cross_index]) - float(neighbor[cross_index])) > 1e-6
                    and abs(float(point[cross_index]) - old_cross) <= 1e-6
                    and effective_span[0] - endpoint_tolerance_px <= float(point[along_index])
                    <= effective_span[1] + endpoint_tolerance_px):
                point[cross_index] = new_cross
                moved_junctions.append(f"{partition.get('id', '?')}:{endpoint_index}")
    return {"walls": sorted(set(moved_walls)), "openings": sorted(set(moved_openings)),
            "junctions": sorted(set(moved_junctions))}


def _append_line_reference(plan: dict[str, Any], *, partition_id: str,
                           basis: str, source_refs: list[str]) -> None:
    inputs = plan.setdefault("regularization_inputs", {})
    rows = inputs.setdefault("line_references", [])
    candidate = {"partition_id": partition_id, "basis": basis,
                 "source_refs": list(dict.fromkeys(source_refs))}
    previous = next((row for row in rows if row.get("partition_id") == partition_id), None)
    rank = {"inferred": 0, "measured": 1, "ink": 2, "dimension": 3, "exterior": 4}
    if previous is None:
        rows.append(candidate)
    elif rank.get(basis, -1) >= rank.get(previous.get("basis"), -1):
        previous.update(candidate)


def _set_reading_alignment(plan: dict[str, Any], key: str, report: Mapping[str, Any]) -> None:
    inputs = plan.setdefault("regularization_inputs", {})
    empty = {"summary": {"status": "not_supplied"}, "items": [], "rejections": [],
             "search_or_tolerance": "not applicable"}
    audit = inputs.setdefault("reading_alignment", {
        "schema_version": "plan_reading_alignment_v1",
        "ink": copy.deepcopy(empty),
        "dimensions": copy.deepcopy(empty),
    })
    items = [*report.get("items", []), *report.get("changes", [])]
    audit[key] = {
        "summary": {"status": report.get("status"), **copy.deepcopy(report.get("summary", {}))},
        "items": copy.deepcopy(items),
        "rejections": copy.deepcopy(report.get("rejections", [])),
        "search_or_tolerance": copy.deepcopy(
            report.get("search", report.get("tolerances", "not reported"))
        ),
    }
