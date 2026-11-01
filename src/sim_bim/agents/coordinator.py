"""Coordinator agent and the drawing-mode pipeline."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from loguru import logger
from PIL import Image
from pydantic import Field
from pydantic_ai import (
    Agent,
    BinaryContent,
    ModelRetry,
    RunContext,
    UnexpectedModelBehavior,
)

from sim_bim._share import BaseSchema
from sim_bim.agents.reader import (
    ElevationDeps,
    PlanDeps,
    SheetDeps,
    elevation_agent,
    layout_agent,
    plan_agent,
)
from sim_bim.agents.runlog import ReaderStatusSchema
from sim_bim.agents.session import Session
from sim_bim.bim.build import build_from_drawings
from sim_bim.bim.schema import BuildingSchema
from sim_bim.drawing.elevation import compile_elevation
from sim_bim.drawing.images import (
    candidate_regions,
    load_sheet,
    png_bytes,
    render_region,
    render_regions,
)
from sim_bim.drawing.issues import CompileError
from sim_bim.drawing.plan import compile_plan
from sim_bim.drawing.readings import PlanReadingSchema, ViewBoxSchema

SHEET_SUFFIXES: Final = {".png", ".tif", ".tiff"}
OVERVIEW_SIDE_PX: Final = 2048

COORDINATOR_INSTRUCTIONS = """\
You coordinate the conversion of one building's drawings into a lightweight BIM. Readers with
vision read individual views; you work from their text reports and never see the drawings.

1. Call read_views once with a datum that every floor plan shows, described so a reader can
   find the same physical point on each plan (default: the outer south-west corner of the
   main block's exterior walls, ignoring porches and wings), and with the storey names to use,
   lowest first, exactly as the plan titles name them.
2. Call build_building and study its report: storey heights, spaces per storey, opening
   heights taken from elevations, and every unresolved mismatch.
3. Where a report shows a likely misreading, call reread_view with specific guidance (which
   object, what looks wrong, what to check). Reread each view at most twice.
4. Finish with a short report of what was built and the questions a person must still answer.
"""


class CoordinatorReportSchema(BaseSchema):
    summary: str
    open_questions: list[str] = Field(default_factory=list)


@dataclass
class CoordinatorDeps:
    session: Session
    building: BuildingSchema | None = None


coordinator_agent = Agent(
    deps_type=CoordinatorDeps,
    output_type=CoordinatorReportSchema,
    instructions=COORDINATOR_INSTRUCTIONS,
    name="coordinator",
)


def _crop(session: Session, view_id: str) -> Image.Image:
    sheet, box = session.views[view_id]
    return session.sheets[sheet].crop(box.box_px)


def _overview(view: Image.Image) -> BinaryContent:
    shown = render_region(
        view, (0, 0, view.width, view.height), max_side=OVERVIEW_SIDE_PX
    )
    return BinaryContent(png_bytes(shown), media_type="image/png")


async def _read_one(session: Session, box: ViewBoxSchema, guidance: str) -> None:
    state = session.state
    view = await asyncio.to_thread(_crop, session, box.view_id)
    name = f"reader-{box.view_id}"
    try:
        if box.kind == "plan":
            storey = (box.storey or "").casefold()

            def save_draft(reading: PlanReadingSchema) -> None:
                state.drafts[box.view_id] = reading
                session.save()

            deps = PlanDeps(
                view=view,
                view_id=box.view_id,
                storey=storey,
                save_draft=save_draft,
                reading=state.drafts.get(box.view_id),
            )
            prompt = (
                f"Read plan view {box.view_id} ('{box.title}', storey '{storey}'). "
                f"Datum: {state.datum}. {guidance}"
            )
            await session.run_agent(
                plan_agent,
                name,
                [prompt, _overview(view)],
                deps,
                model=session.reader_model,
            )
            if deps.reading is None:
                raise RuntimeError(f"{name} finished without a reading")
            state.plans[box.view_id] = deps.reading
        else:
            deps = ElevationDeps(view=view, view_id=box.view_id, storeys=state.storeys)
            prompt = (
                f"Read elevation view {box.view_id} ('{box.title}', facing {box.facing}). "
                f"Storey names: {state.storeys}. {guidance}"
            )
            state.elevations[box.view_id] = await session.run_agent(
                elevation_agent,
                name,
                [prompt, _overview(view)],
                deps,
                model=session.reader_model,
            )
        state.readers[box.view_id] = ReaderStatusSchema(state="done")
    except UnexpectedModelBehavior as error:
        logger.warning("{} failed: {}", name, error)
        state.readers[box.view_id] = ReaderStatusSchema(
            state="failed", detail=str(error)
        )
    session.save()


async def _read_all(
    session: Session, view_ids: list[str], guidance: dict[str, str]
) -> None:
    gate = asyncio.Semaphore(session.settings.max_concurrent_readers)

    async def bounded(view_id: str) -> None:
        async with gate:
            await _read_one(
                session, session.views[view_id][1], guidance.get(view_id, "")
            )

    async with asyncio.TaskGroup() as group:
        for view_id in view_ids:
            group.create_task(bounded(view_id))


def _reader_report(session: Session) -> str:
    lines = []
    for view_id, status in sorted(session.state.readers.items()):
        lines.append(f"{view_id}: {status.state} {status.detail}".rstrip())
    return "\n".join(lines)


@coordinator_agent.tool
async def read_views(
    ctx: RunContext[CoordinatorDeps], datum: str, storeys: list[str]
) -> str:
    """Read every plan and elevation view concurrently with the given datum and storey names."""
    session = ctx.deps.session
    session.state.datum = datum
    session.state.storeys = [storey.casefold() for storey in storeys]
    session.save()
    pending = [
        view_id
        for view_id in session.views
        if session.state.readers.get(view_id, ReaderStatusSchema()).state != "done"
    ]
    await _read_all(session, pending, {})
    return _reader_report(session)


@coordinator_agent.tool
async def reread_view(
    ctx: RunContext[CoordinatorDeps], view_id: str, guidance: str
) -> str:
    """Read one view again from scratch with specific guidance."""
    session = ctx.deps.session
    if view_id not in session.views:
        raise ModelRetry(f"unknown view {view_id}; views are {sorted(session.views)}")
    session.log.history_path(f"reader-{view_id}").unlink(missing_ok=True)
    session.state.plans.pop(view_id, None)
    session.state.drafts.pop(view_id, None)
    session.state.elevations.pop(view_id, None)
    await _read_one(
        session, session.views[view_id][1], f"Guidance from the coordinator: {guidance}"
    )
    return _reader_report(session)


async def build(session: Session) -> tuple[BuildingSchema, str]:
    """Compile all stored readings into the building and describe the result.

    Raises:
        ValueError: If no plan reading is stored.
        CompileError: If a stored reading no longer compiles.
    """
    plans = [compile_plan(reading) for reading in session.state.plans.values()]
    elevations = [compile_elevation(r) for r in session.state.elevations.values()]
    if not plans:
        raise ValueError("no plan was read successfully")
    name = session.brief.splitlines()[0][:80] if session.brief else "building"
    building = await asyncio.to_thread(
        build_from_drawings, name, plans, elevations, sorted(session.sheets)
    )
    lines = []
    for storey in building.storeys:
        spaces = ", ".join(
            f"{sp.name} {sp.area_m2:.1f} m²"
            for sp in building.spaces
            if sp.storey_id == storey.id
        )
        lines.append(
            f"storey {storey.id} '{storey.name}': floor {storey.elevation_m:.2f} m, "
            f"height {storey.height_m:.2f} m, spaces: {spaces}"
        )
    sourced = sum(o.height_source != "default" for o in building.openings)
    lines.append(
        f"{len(building.openings)} openings, {sourced} with heights from elevations"
    )
    lines += ["unresolved: " + item for item in building.unresolved]
    lines += ["assumption: " + item for item in building.assumptions]
    return building, "\n".join(lines)


@coordinator_agent.tool
async def build_building(ctx: RunContext[CoordinatorDeps]) -> str:
    """Compile all readings into the building and report heights, spaces and mismatches."""
    try:
        ctx.deps.building, report = await build(ctx.deps.session)
    except (ValueError, CompileError) as error:
        raise ModelRetry(f"{error}; reread the affected views") from error
    return report


async def layout_sheets(session: Session) -> None:
    """Group each sheet's ink regions into views, reusing stored layouts."""
    for name, sheet in session.sheets.items():
        regions = await asyncio.to_thread(candidate_regions, sheet)
        if name not in session.state.sheets:
            overview = render_regions(sheet, regions)
            prompt = f"Sheet {name}, {sheet.width}x{sheet.height} px, with {len(regions)} boxed regions."
            session.state.sheets[name] = await session.run_agent(
                layout_agent,
                f"layout-{Path(name).stem}",
                [prompt, BinaryContent(png_bytes(overview), media_type="image/png")],
                SheetDeps(sheet=sheet, regions=regions),
                model=session.reader_model,
            )
            session.save()
        for view in session.state.sheets[name].views:
            boxes = [regions[number] for number in view.regions]
            margin = round(0.005 * max(sheet.size))
            box_px = (
                max(0, min(b[0] for b in boxes) - margin),
                max(0, min(b[1] for b in boxes) - margin),
                min(sheet.width, max(b[2] for b in boxes) + margin),
                min(sheet.height, max(b[3] for b in boxes) + margin),
            )
            view_id = (
                view.view_id
                if view.view_id not in session.views
                else f"{Path(name).stem}-{view.view_id}"
            )
            session.views[view_id] = (
                name,
                ViewBoxSchema.model_validate(
                    view.model_dump() | {"view_id": view_id, "box_px": box_px}
                ),
            )


def load_sheets(input_dir: Path) -> dict[str, Image.Image]:
    """Load every PNG or TIFF sheet directly inside ``input_dir``.

    Raises:
        FileNotFoundError: If the directory holds no sheet.
    """
    paths = sorted(p for p in input_dir.iterdir() if p.suffix.lower() in SHEET_SUFFIXES)
    if not paths:
        raise FileNotFoundError(f"no PNG or TIFF drawings in {input_dir}")
    return {path.name: load_sheet(path) for path in paths}


async def run_drawings(
    session: Session,
) -> tuple[BuildingSchema, CoordinatorReportSchema]:
    """Lay out sheets, read views through the coordinator and return the building.

    Raises:
        ValueError: If no plan could be read.
    """
    await layout_sheets(session)
    views = "\n".join(
        f"- {view_id}: {box.kind} '{box.title}'"
        + (f" storey '{box.storey}'" if box.storey else "")
        + (f" facing {box.facing}" if box.facing else "")
        for view_id, (_, box) in session.views.items()
    )
    deps = CoordinatorDeps(session=session)
    prompt = f"Building brief:\n{session.brief or '(none)'}\n\nViews found on the sheets:\n{views}"
    report = await session.run_agent(
        coordinator_agent,
        "coordinator",
        [prompt],
        deps,
        model=session.coordinator_model,
    )
    building = deps.building or (await build(session))[0]
    return building, report
