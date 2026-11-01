"""Deterministic compilation of a plan reading into metric walls, spaces and openings."""

import math
from dataclasses import dataclass, field
from typing import Final, Literal

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

from sim_bim._share import BaseSchema, Point2
from sim_bim.drawing.issues import CompileError, Issue
from sim_bim.drawing.readings import (
    PlanPatchSchema,
    PlanReadingSchema,
    RemoveSchema,
    UpsertOpeningSchema,
    UpsertRoomSchema,
    UpsertWallSchema,
    WallMoveSchema,
)
from sim_bim.drawing.units import AxisScale, fit_chain

AXIS_SNAP_DEG: Final = 3.0
LINE_MERGE_M: Final = 0.10
JOIN_M: Final = 0.30
CHAIN_RESIDUAL_M: Final = 0.05
SCALE_MISMATCH: Final = 0.02
MIN_FACE_M2: Final = 0.5


class PlanWallSchema(BaseSchema):
    id: str
    start: Point2
    end: Point2
    thickness_m: float
    kind: Literal["wall", "open_edge"]

    @property
    def length_m(self) -> float:
        return math.dist(self.start, self.end)


class PlanSpaceSchema(BaseSchema):
    id: str
    name: str
    polygon: list[Point2]


class PlanOpeningSchema(BaseSchema):
    id: str
    kind: Literal["door", "window", "opening"]
    wall_id: str
    offset_m: float
    width_m: float


class CompiledPlanSchema(BaseSchema):
    view_id: str
    storey: str
    px_per_m: float
    walls: list[PlanWallSchema]
    spaces: list[PlanSpaceSchema]
    openings: list[PlanOpeningSchema]
    warnings: list[str]


@dataclass
class _Calibration:
    x: AxisScale
    y: AxisScale
    datum: Point2
    issues: list[Issue] = field(default_factory=list)

    def to_m(self, point: Point2) -> Point2:
        return (
            (point[0] - self.datum[0]) / self.x.px_per_m,
            (self.datum[1] - point[1]) / self.y.px_per_m,
        )


def calibrate(reading: PlanReadingSchema) -> _Calibration:
    """Fit pixel scales from the reading's dimension chains.

    Raises:
        CompileError: If no chain can be fitted.
    """
    fits: dict[str, list[tuple[int, AxisScale]]] = {"x": [], "y": []}
    issues: list[Issue] = []
    for index, chain in enumerate(reading.chains):
        chain_id = f"chain{index}-{chain.axis}"
        try:
            scale = fit_chain(chain.ticks_px, chain.labels)
        except ValueError as error:
            issues.append(Issue(chain_id, str(error)))
            continue
        if scale.residual_m > CHAIN_RESIDUAL_M:
            issues.append(
                Issue(
                    chain_id,
                    f"tick positions disagree with the labels by up to {scale.residual_m:.3f} m; "
                    "re-measure the ticks or re-read the labels",
                )
            )
            continue
        fits[chain.axis].append((len(chain.ticks_px), scale))
    if not fits["x"] and not fits["y"]:
        raise CompileError(issues or [Issue("chains", "no usable dimension chain")])
    best = {
        axis: max(rows, key=lambda row: row[0])[1]
        for axis, rows in fits.items()
        if rows
    }
    x = best.get("x") or best["y"]
    y = best.get("y") or best["x"]
    mismatch = abs(x.px_per_m - y.px_per_m) / max(x.px_per_m, y.px_per_m)
    if mismatch > SCALE_MISMATCH:
        issues.append(
            Issue(
                "chains",
                f"horizontal and vertical scales differ by {mismatch:.1%} "
                f"({x.px_per_m:.1f} vs {y.px_per_m:.1f} px/m); one chain is misread",
            )
        )
    return _Calibration(x=x, y=y, datum=reading.datum_px, issues=issues)


def _orthogonalise(start: Point2, end: Point2) -> tuple[Point2, Point2]:
    dx, dy = end[0] - start[0], end[1] - start[1]
    angle = math.degrees(math.atan2(abs(dy), abs(dx)))
    if angle < AXIS_SNAP_DEG:
        y = (start[1] + end[1]) / 2
        return (start[0], y), (end[0], y)
    if angle > 90 - AXIS_SNAP_DEG:
        x = (start[0] + end[0]) / 2
        return (x, start[1]), (x, end[1])
    return start, end


def _merge_lines(walls: dict[str, list[list[float]]]) -> None:
    """Move nearly coincident parallel axis walls onto one shared line."""
    for axis in (0, 1):
        other = 1 - axis
        rows = [
            (points[0][other], math.dist(*points), wall_id)
            for wall_id, points in walls.items()
            if points[0][other] == points[1][other]
            and points[0][axis] != points[1][axis]
        ]
        rows.sort()
        clusters: list[list[tuple[float, float, str]]] = []
        for row in rows:
            if clusters and row[0] - clusters[-1][-1][0] <= LINE_MERGE_M:
                clusters[-1].append(row)
            else:
                clusters.append([row])
        for cluster in clusters:
            total = sum(length for _, length, _ in cluster)
            value = sum(coord * length for coord, length, _ in cluster) / total
            for _, _, wall_id in cluster:
                for point in walls[wall_id]:
                    point[other] = value


def _extended(points: list[list[float]], distance: float) -> LineString:
    start, end = np.asarray(points[0]), np.asarray(points[1])
    length = float(np.hypot(*(end - start)))
    if length == 0:
        return LineString([start, end])
    unit = (end - start) / length
    return LineString([start - unit * distance, end + unit * distance])


def _snap_endpoints(walls: dict[str, list[list[float]]]) -> None:
    """Extend or trim each wall end onto the nearest other wall within ``JOIN_M``."""
    ids = list(walls)
    lines = [_extended(walls[wall_id], JOIN_M) for wall_id in ids]
    tree = shapely.STRtree(lines)
    for index, wall_id in enumerate(ids):
        points = walls[wall_id]
        direction = np.subtract(points[1], points[0])
        length = float(np.hypot(*direction))
        if length == 0:
            continue
        unit = direction / length
        for end, sign in ((0, -1.0), (1, 1.0)):
            end_point = Point(points[end])
            best: tuple[float, Point2] | None = None
            for candidate in tree.query(end_point.buffer(JOIN_M)):
                if candidate == index:
                    continue
                target = lines[candidate]
                if target.distance(end_point) > JOIN_M:
                    continue
                ray = LineString(
                    [
                        tuple(np.asarray(points[end]) - sign * unit * JOIN_M),
                        tuple(np.asarray(points[end]) + sign * unit * JOIN_M),
                    ]
                )
                hit = ray.intersection(target)
                if hit.is_empty or hit.geom_type != "Point":
                    continue
                distance = hit.distance(end_point)
                if best is None or distance < best[0]:
                    best = (distance, (hit.x, hit.y))
            if best is not None:
                points[end] = list(best[1])


def compile_plan(reading: PlanReadingSchema) -> CompiledPlanSchema:
    """Compile a plan reading into metric geometry.

    Raises:
        CompileError: If calibration, enclosure or opening hosting fails;
            each issue names the object the reader must change.
    """
    calibration = calibrate(reading)
    issues = list(calibration.issues)
    warnings: list[str] = []

    raw: dict[str, list[list[float]]] = {}
    kinds: dict[str, Literal["wall", "open_edge"]] = {}
    thickness: dict[str, float] = {}
    for wall in reading.walls:
        start, end = _orthogonalise(
            calibration.to_m(wall.start_px), calibration.to_m(wall.end_px)
        )
        if math.dist(start, end) < 0.05:
            issues.append(
                Issue(wall.id, "wall is shorter than 5 cm; remove it or fix its ends")
            )
            continue
        raw[wall.id] = [list(start), list(end)]
        kinds[wall.id] = wall.kind
        thickness[wall.id] = wall.thickness_m
    _merge_lines(raw)
    _snap_endpoints(raw)
    walls = {
        wall_id: PlanWallSchema(
            id=wall_id,
            start=(round(points[0][0], 4), round(points[0][1], 4)),
            end=(round(points[1][0], 4), round(points[1][1], 4)),
            thickness_m=thickness[wall_id],
            kind=kinds[wall_id],
        )
        for wall_id, points in raw.items()
    }

    noded = unary_union([LineString([w.start, w.end]) for w in walls.values()])
    faces = [face for face in polygonize(noded) if face.area >= MIN_FACE_M2]
    dangling = _free_ends(noded, walls)

    spaces: list[PlanSpaceSchema] = []
    claimed: dict[int, str] = {}
    for room in reading.rooms:
        seed = Point(calibration.to_m(room.seed_px))
        hits = [index for index, face in enumerate(faces) if face.contains(seed)]
        if not hits:
            near = sorted(dangling, key=lambda item: item[1].distance(seed))[:2]
            hint = "; ".join(
                f"wall {wall_id} has a free end at ({p.x:.2f}, {p.y:.2f}) m"
                for wall_id, p in near
            )
            issues.append(
                Issue(
                    room.id,
                    f"room '{room.name}' is not enclosed by walls or open edges"
                    + (f" ({hint})" if hint else ""),
                )
            )
            continue
        face_index = min(hits, key=lambda index: faces[index].area)
        if face_index in claimed:
            issues.append(
                Issue(
                    room.id,
                    f"room '{room.name}' shares one enclosed region with room {claimed[face_index]}; "
                    "a separating wall or open edge is missing",
                )
            )
            continue
        claimed[face_index] = room.id
        spaces.append(
            PlanSpaceSchema(
                id=room.id, name=room.name, polygon=_ring(faces[face_index])
            )
        )
    for index, face in enumerate(faces):
        if index not in claimed:
            warnings.append(f"enclosed region of {face.area:.1f} m² has no room label")

    openings = _host_openings(reading, calibration, walls, issues)
    for wall_id, point in dangling:
        warnings.append(
            f"wall {wall_id} has a free end at ({point.x:.2f}, {point.y:.2f}) m"
        )

    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        raise CompileError(errors)
    return CompiledPlanSchema(
        view_id=reading.view_id,
        storey=reading.storey,
        px_per_m=(calibration.x.px_per_m + calibration.y.px_per_m) / 2,
        walls=list(walls.values()),
        spaces=spaces,
        openings=openings,
        warnings=warnings,
    )


def _free_ends(
    noded: shapely.Geometry, walls: dict[str, PlanWallSchema]
) -> list[tuple[str, Point]]:
    """Wall ends that touch nothing else in the noded wall network."""
    degree: dict[tuple[float, float], int] = {}
    for segment in shapely.get_parts(noded):
        for coord in (segment.coords[0], segment.coords[-1]):
            key = (round(coord[0], 6), round(coord[1], 6))
            degree[key] = degree.get(key, 0) + 1
    ids = list(walls)
    tree = shapely.STRtree([LineString([walls[i].start, walls[i].end]) for i in ids])
    return [
        (ids[int(tree.query_nearest(point)[0])], point)
        for point in (Point(key) for key, count in degree.items() if count == 1)
    ]


def _ring(face: Polygon) -> list[Point2]:
    ring = shapely.remove_repeated_points(face.exterior).simplify(0.01)
    return [(round(x, 4), round(y, 4)) for x, y in list(ring.coords)[:-1]]


def _host_openings(
    reading: PlanReadingSchema,
    calibration: _Calibration,
    walls: dict[str, PlanWallSchema],
    issues: list[Issue],
) -> list[PlanOpeningSchema]:
    hosted: dict[str, list[tuple[float, float, str]]] = {}
    openings: list[PlanOpeningSchema] = []
    for opening in reading.openings:
        wall = walls.get(opening.wall_id)
        if wall is None:
            issues.append(
                Issue(opening.id, f"host wall {opening.wall_id} does not exist")
            )
            continue
        if wall.kind == "open_edge":
            issues.append(
                Issue(opening.id, f"host {wall.id} is an open edge, not a wall")
            )
            continue
        line = LineString([wall.start, wall.end])
        centre = Point(calibration.to_m(opening.center_px))
        off_wall = line.distance(centre)
        if off_wall > max(JOIN_M, wall.thickness_m):
            issues.append(
                Issue(
                    opening.id,
                    f"centre is {off_wall:.2f} m away from host wall {wall.id}; "
                    "check wall_id or center_px",
                )
            )
            continue
        offset = line.project(centre)
        low, high = offset - opening.width_m / 2, offset + opening.width_m / 2
        if low < -0.05 or high > wall.length_m + 0.05:
            issues.append(
                Issue(
                    opening.id,
                    f"{opening.width_m:.2f} m opening at {offset:.2f} m runs past the ends of "
                    f"wall {wall.id} (length {wall.length_m:.2f} m)",
                )
            )
            continue
        for other_low, other_high, other_id in hosted.get(wall.id, []):
            if low < other_high - 0.02 and high > other_low + 0.02:
                issues.append(
                    Issue(opening.id, f"overlaps opening {other_id} on wall {wall.id}")
                )
        hosted.setdefault(wall.id, []).append((low, high, opening.id))
        openings.append(
            PlanOpeningSchema(
                id=opening.id,
                kind=opening.kind,
                wall_id=wall.id,
                offset_m=round(offset, 4),
                width_m=opening.width_m,
            )
        )
    return openings


def apply_patch(
    reading: PlanReadingSchema, patch: PlanPatchSchema
) -> PlanReadingSchema:
    """Return a copy of ``reading`` with the patch edits applied in order.

    Raises:
        KeyError: If an edit names an object that does not exist.
    """
    data = reading.model_copy(deep=True)
    for edit in patch.edits:
        match edit:
            case WallMoveSchema(wall_id=wall_id, start_px=start, end_px=end):
                wall = next((w for w in data.walls if w.id == wall_id), None)
                if wall is None:
                    raise KeyError(f"wall {wall_id} does not exist")
                wall.start_px, wall.end_px = start, end
            case RemoveSchema(id=object_id):
                before = len(data.walls) + len(data.openings) + len(data.rooms)
                data.walls = [w for w in data.walls if w.id != object_id]
                data.openings = [o for o in data.openings if o.id != object_id]
                data.rooms = [r for r in data.rooms if r.id != object_id]
                if before == len(data.walls) + len(data.openings) + len(data.rooms):
                    raise KeyError(f"{object_id} does not exist")
            case UpsertWallSchema(wall=wall):
                data.walls = [w for w in data.walls if w.id != wall.id] + [wall]
            case UpsertOpeningSchema(opening=opening):
                data.openings = [o for o in data.openings if o.id != opening.id] + [
                    opening
                ]
            case UpsertRoomSchema(room=room):
                data.rooms = [r for r in data.rooms if r.id != room.id] + [room]
    return PlanReadingSchema.model_validate(data.model_dump())
