"""Mesh mode: align and render a textured mesh, read its facades, build the BIM."""

import asyncio
import itertools
import math
import statistics
from pathlib import Path
from typing import Final, Literal, Self, get_args

import numpy as np
import shapely
import trimesh
from numpy.typing import NDArray
from pydantic import Field, model_validator
from pydantic_ai import Agent, BinaryContent, ModelRetry, RunContext
from shapely.geometry import LineString, Polygon, box

from sim_bim._share import BaseSchema, Point2
from sim_bim.agents.reader import ElevationDeps, view_region
from sim_bim.agents.session import Session
from sim_bim.bim.schema import (
    BuildingSchema,
    OpeningSchema,
    ReferenceMeshSchema,
    SpaceSchema,
    StoreySchema,
    WallSchema,
)
from sim_bim.drawing.images import png_bytes, render_region
from sim_bim.drawing.readings import Facing
from sim_bim.mesh.model import Y_UP_TO_Z_UP, MeshModel, load_glb
from sim_bim.mesh.raster import FacadeProjection, render_facade

FACINGS: Final[tuple[Facing, ...]] = get_args(Facing.__value__)
IMAGE_SIDE_PX: Final = 1600
SECTION_HEIGHT_M: Final = 1.2
WALL_THICKNESS_M: Final = 0.3
CLOSE_GAP_M: Final = 0.8
REFERENCE_FILE: Final = "reference.glb"

FACADE_INSTRUCTIONS = """\
You read one orthographic facade image rendered from a textured 3D building mesh. The first
image has a grid labelled in image pixels; view_region renders any part of it larger.

Report the horizontal extent of the main building (ignore trees, cars, fences and terrain),
the ground line and the eave line of the main walls, the floor line of every storey from the
lowest up (the ground floor's line is usually just above the ground line), and every door and
window of the main building with its storey number (1 = lowest) and its frame edges in pixels.
Report only what is visible; leave out openings hidden by trees or porches."""


class MeshScaleSchema(BaseSchema):
    eave_height_m: float | None = Field(default=None, gt=0)
    metres_per_unit: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.eave_height_m is None) == (self.metres_per_unit is None):
            raise ValueError("give exactly one of eave_height_m or metres_per_unit")
        return self


class FacadeOpeningSchema(BaseSchema):
    id: str
    kind: Literal["door", "window"]
    storey: int = Field(ge=1)
    left_px: float
    right_px: float
    top_px: float
    bottom_px: float


class FacadeReadingSchema(BaseSchema):
    building_left_px: float
    building_right_px: float
    ground_y_px: float
    eave_y_px: float
    floor_lines_y_px: list[float] = Field(
        min_length=1, description="Lowest storey first."
    )
    openings: list[FacadeOpeningSchema]
    unresolved: list[str] = Field(default_factory=list)


facade_agent = Agent(
    deps_type=ElevationDeps,
    output_type=FacadeReadingSchema,
    instructions=FACADE_INSTRUCTIONS,
    name="facade_reader",
    tools=[view_region],
)


@facade_agent.output_validator
def _check_facade(
    ctx: RunContext[ElevationDeps], reading: FacadeReadingSchema
) -> FacadeReadingSchema:
    problems = []
    if reading.building_right_px <= reading.building_left_px:
        problems.append("building_right_px must be right of building_left_px")
    if reading.eave_y_px >= reading.ground_y_px:
        problems.append("the eave line must be above the ground line (smaller y)")
    floors = reading.floor_lines_y_px
    if any(upper >= lower for lower, upper in itertools.pairwise(floors)):
        problems.append(
            "floor lines must go upwards: each y smaller than the one before"
        )
    for opening in reading.openings:
        if opening.right_px <= opening.left_px or opening.bottom_px <= opening.top_px:
            problems.append(f"{opening.id}: frame edges are swapped")
        if opening.storey > len(floors):
            problems.append(f"{opening.id}: storey {opening.storey} has no floor line")
    if problems:
        raise ModelRetry("\n".join(problems))
    return reading


def align_to_axes(model: MeshModel) -> tuple[MeshModel, NDArray[np.float64]]:
    """Rotate about z so the dominant wall directions lie along x and y.

    Returns the rotated model and the 4x4 rotation applied.
    """
    triangles = model.vertices[model.faces]
    normals = np.cross(
        triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
    )
    areas = np.linalg.norm(normals, axis=1)
    keep = areas > 0
    unit = normals[keep] / areas[keep, None]
    vertical = np.abs(unit[:, 2]) < 0.3
    angles = np.mod(np.degrees(np.arctan2(unit[vertical, 1], unit[vertical, 0])), 90.0)
    weights = areas[keep][vertical]
    histogram, _ = np.histogram(angles, bins=90, range=(0, 90), weights=weights)
    peak = float(np.argmax(histogram)) + 0.5
    offset = (angles - peak + 45.0) % 90.0 - 45.0
    near = np.abs(offset) <= 1.5
    peak += float(np.average(offset[near], weights=weights[near]))
    rotation = trimesh.transformations.rotation_matrix(math.radians(-peak), (0, 0, 1))
    return model.transformed(rotation), rotation


def _to_world(facing: Facing, u: float) -> float:
    """Model x (south, north) or y (east, west) for a facade coordinate ``u``."""
    return u if facing in ("south", "east") else -u


def _footprint(
    model: MeshModel,
    z_cut: float,
    x_range: tuple[float, float],
    y_range: tuple[float, float],
    unit: float,
) -> Polygon | None:
    mesh = trimesh.Trimesh(model.vertices, model.faces, process=False)
    lines = trimesh.intersections.mesh_plane(mesh, (0, 0, 1), (0, 0, z_cut))
    if len(lines) == 0:
        return None
    segments = shapely.union_all([LineString(segment[:, :2]) for segment in lines])
    closed = segments.buffer(CLOSE_GAP_M / unit).buffer(-(CLOSE_GAP_M - 0.05) / unit)
    polygons = [
        Polygon(p.exterior) for p in shapely.get_parts(closed) if isinstance(p, Polygon)
    ]
    if not polygons:
        return None
    region = box(x_range[0], y_range[0], x_range[1], y_range[1]).buffer(1.0 / unit)
    building = max(polygons, key=lambda polygon: polygon.intersection(region).area)
    if building.intersection(region).is_empty:
        return None
    return building.simplify(0.15 / unit)


async def run_mesh(
    session: Session, glb: Path, scale: MeshScaleSchema
) -> BuildingSchema:
    """Reconstruct storeys, footprint walls and facade openings of a textured mesh.

    Raises:
        ValueError: If the mesh has no triangles or no facade could be read.
    """
    model, rotation = align_to_axes(await asyncio.to_thread(load_glb, glb))
    span = float((model.vertices.max(0) - model.vertices.min(0)).max())
    px_per_unit = IMAGE_SIDE_PX / span
    readings: dict[Facing, tuple[FacadeReadingSchema, FacadeProjection]] = {}
    gate = asyncio.Semaphore(session.settings.max_concurrent_readers)

    async def read(facing: Facing) -> None:
        async with gate:
            image, projection = await asyncio.to_thread(
                render_facade, model, facing, px_per_unit
            )
            shown = await asyncio.to_thread(
                render_region, image, (0, 0, image.width, image.height)
            )
            deps = ElevationDeps(view=image, view_id=f"facade-{facing}", storeys=[])
            reading = await session.run_agent(
                facade_agent,
                f"facade-{facing}",
                [
                    f"Facade seen from the {facing}. {session.brief}",
                    BinaryContent(png_bytes(shown), media_type="image/png"),
                ],
                deps,
                model=session.reader_model,
            )
            readings[facing] = (reading, projection)
            session.log.event(
                "facade_read", facing=facing, openings=len(reading.openings)
            )

    async with asyncio.TaskGroup() as group:
        for facing in FACINGS:
            group.create_task(read(facing))
    return build_from_facades(model, rotation, readings, scale, glb.name, session.brief)


def build_from_facades(
    model: MeshModel,
    rotation: NDArray[np.float64],
    readings: dict[Facing, tuple[FacadeReadingSchema, FacadeProjection]],
    scale: MeshScaleSchema,
    source: str,
    brief: str,
) -> BuildingSchema:
    """Combine facade readings with the aligned mesh into a building in metres.

    Raises:
        ValueError: If no facade reading is given.
    """
    if not readings:
        raise ValueError("no facade was read")
    assumptions: list[str] = []
    unresolved: list[str] = []
    ground = statistics.median(p.z_m(r.ground_y_px) for r, p in readings.values())
    eave = statistics.median(p.z_m(r.eave_y_px) for r, p in readings.values())
    unit = scale.metres_per_unit or (scale.eave_height_m or 0) / (eave - ground)
    storey_count = max(len(r.floor_lines_y_px) for r, _ in readings.values())
    floors = []
    for index in range(storey_count):
        heights = [
            p.z_m(r.floor_lines_y_px[index])
            for r, p in readings.values()
            if index < len(r.floor_lines_y_px)
        ]
        floors.append((statistics.median(heights) - ground) * unit)
    tops = [*floors[1:], (eave - ground) * unit]
    storeys = [
        StoreySchema(
            id=f"L{i + 1}",
            name=f"storey {i + 1}",
            elevation_m=round(f, 3),
            height_m=round(t - f, 3),
        )
        for i, (f, t) in enumerate(zip(floors, tops, strict=True))
    ]

    def extent(facing: Facing) -> tuple[float, float] | None:
        if facing not in readings:
            return None
        reading, projection = readings[facing]
        ends = sorted(
            _to_world(facing, projection.u_m(px))
            for px in (reading.building_left_px, reading.building_right_px)
        )
        return ends[0], ends[1]

    x_range = extent("south") or extent("north")
    y_range = extent("east") or extent("west")
    if x_range is None or y_range is None:
        raise ValueError("need one facade on each axis to locate the footprint")
    outlines: list[Polygon] = []
    for storey in storeys:
        z_cut = ground + (storey.elevation_m + SECTION_HEIGHT_M) / unit
        outline = _footprint(model, z_cut, x_range, y_range, unit)
        if outline is None:
            outline = (
                outlines[-1]
                if outlines
                else box(x_range[0], y_range[0], x_range[1], y_range[1])
            )
            assumptions.append(
                f"mesh section of {storey.name} failed; footprint taken from "
                + ("the storey below" if outlines else "the facade bounding rectangle")
            )
        outlines.append(outline)
    origin = (outlines[0].bounds[0], outlines[0].bounds[1])

    def ring_of(outline: Polygon) -> list[Point2]:
        ring = [
            (round((x - origin[0]) * unit, 3), round((y - origin[1]) * unit, 3))
            for x, y in list(outline.exterior.coords)[:-1]
        ]
        return ring if Polygon(ring).exterior.is_ccw else ring[::-1]

    walls: list[WallSchema] = []
    spaces: list[SpaceSchema] = []
    for storey, outline in zip(storeys, outlines, strict=True):
        ring = ring_of(outline)
        for index, (start, end) in enumerate(
            zip(ring, ring[1:] + ring[:1], strict=True)
        ):
            walls.append(
                WallSchema(
                    id=f"{storey.id}:w{index}",
                    storey_id=storey.id,
                    start=start,
                    end=end,
                    thickness_m=WALL_THICKNESS_M,
                    height_m=storey.height_m,
                )
            )
        spaces.append(
            SpaceSchema(
                id=f"{storey.id}:floor",
                storey_id=storey.id,
                name=f"{storey.name} (interior not observed)",
                polygon=ring,
                area_m2=round(Polygon(ring).area, 2),
            )
        )
    unresolved.append(
        "interior partitions cannot be observed in a mesh; each storey is one space"
    )

    openings: list[OpeningSchema] = []
    outward = {"south": (0, -1), "north": (0, 1), "east": (1, 0), "west": (-1, 0)}
    for facing, (reading, projection) in readings.items():
        dx, dy = outward[facing]
        for opening in reading.openings:
            storey = storeys[opening.storey - 1]
            centre_u = _to_world(
                facing, projection.u_m((opening.left_px + opening.right_px) / 2)
            )
            along = (centre_u - origin[0 if facing in ("south", "north") else 1]) * unit
            candidates = []
            for wall in walls:
                if wall.storey_id != storey.id:
                    continue
                (x1, y1), (x2, y2) = wall.start, wall.end
                length = math.dist(wall.start, wall.end)
                nx, ny = (y2 - y1) / length, -(x2 - x1) / length
                if nx * dx + ny * dy < 0.9:
                    continue
                axis = 0 if facing in ("south", "north") else 1
                low, high = sorted((wall.start[axis], wall.end[axis]))
                if low - 0.05 <= along <= high + 0.05:
                    depth = -(x1 * dx + y1 * dy)
                    candidates.append((depth, wall, abs(along - wall.start[axis])))
            if not candidates:
                unresolved.append(
                    f"{facing} opening {opening.id} lies on no {facing}-facing wall"
                )
                continue
            _, wall, offset = min(candidates, key=lambda item: item[0])
            width = (opening.right_px - opening.left_px) / projection.px_per_m * unit
            base = storey.elevation_m
            openings.append(
                OpeningSchema(
                    id=f"{storey.id}:{facing}-{opening.id}",
                    storey_id=storey.id,
                    wall_id=wall.id,
                    kind=opening.kind,
                    offset_m=round(offset, 3),
                    width_m=round(width, 3),
                    sill_m=round(
                        (projection.z_m(opening.bottom_px) - ground) * unit - base, 3
                    ),
                    head_m=round(
                        (projection.z_m(opening.top_px) - ground) * unit - base, 3
                    ),
                    height_source=f"facade-{facing}:{opening.id}",
                )
            )
        unresolved += [f"{facing}: {item}" for item in reading.unresolved]
    if scale.metres_per_unit is None:
        assumptions.append(
            f"scale {unit:.4f} m per mesh unit from eave height {scale.eave_height_m} m"
        )
    return BuildingSchema(
        name=(brief.splitlines()[0][:80] if brief else Path(source).stem),
        storeys=storeys,
        spaces=spaces,
        walls=walls,
        openings=openings,
        assumptions=assumptions,
        unresolved=unresolved,
        sources=[source],
        reference_mesh=ReferenceMeshSchema(
            file=REFERENCE_FILE,
            to_building=(
                np.diag([unit, unit, unit, 1.0])
                @ trimesh.transformations.translation_matrix(
                    (-origin[0], -origin[1], -ground)
                )
                @ rotation
                @ Y_UP_TO_Z_UP
            ).tolist(),
        ),
    )
