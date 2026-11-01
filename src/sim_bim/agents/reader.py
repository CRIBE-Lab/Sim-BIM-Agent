"""Vision reader agents: sheet layout, plan reading and elevation reading."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from PIL import Image
from pydantic import Field
from pydantic_ai import Agent, BinaryContent, ModelRetry, RunContext, ToolReturn

from sim_bim._share import BaseSchema
from sim_bim.drawing.elevation import compile_elevation
from sim_bim.drawing.images import (
    ink_profile,
    png_bytes,
    render_plan_preview,
    render_region,
)
from sim_bim.drawing.issues import CompileError
from sim_bim.drawing.plan import CompiledPlanSchema, apply_patch, compile_plan
from sim_bim.drawing.readings import (
    ElevationReadingSchema,
    PlanPatchSchema,
    PlanReadingSchema,
    SheetLayoutSchema,
)
from sim_bim.drawing.units import fit_chain

LAYOUT_INSTRUCTIONS = """\
You identify the views on one sheet of building drawings. The image shows the sheet with
numbered red boxes around separate groups of ink; a single drawing may span several boxes.
Use view_region (sheet pixel coordinates) to look closer where needed.

Report every floor plan and every elevation drawing with the numbers of all boxes that belong
to it, including its dimension chains and level marks. Leave out boxes holding text, titles,
sections, details, title blocks and scale bars. Name plan storeys as printed (for example
'first floor') and give each elevation the compass side it shows, taken from its title."""

PLAN_INSTRUCTIONS = """\
You read one floor plan into structured data. Pixel coordinates are in the view image shown
first, whose grid is labelled in view pixels; view_region renders any part of it larger.

Procedure:
1. Within your first three turns, call set_reading with a complete first reading taken from
   the view: at least one horizontal and one vertical dimension chain (tick positions and
   labels), every wall centreline, one seed point inside each named room, porch and
   balcony, and every door and window on its host wall. Use kind 'open_edge' for the open
   sides of porches and balconies so they still enclose a space. Wall ends within 0.30 m
   of another wall are joined automatically.
2. Read the compile report and the preview overlay, then correct only what they show to be
   wrong with patch_reading. Measure where needed: ink_runs across a chain gives its tick
   positions, across a wall drawn as two lines it gives both faces (the centreline is
   midway), and check_chain tests a chain; pass all segments in one ink_runs call.
3. When the reading compiles and the preview matches the drawing, return your notes.

Do not invent walls or rooms that are not drawn. Record anything you cannot read in
'unresolved'."""

ELEVATION_INSTRUCTIONS = """\
You read one building elevation into structured data. Pixel coordinates are in the view
image shown first, whose grid is labelled in view pixels; view_region renders any part of it
larger and ink_runs finds exact line positions; measure several lines in one call.

Read every level mark with its printed height label and say what it marks (grade, a storey
floor or ceiling, eave or ridge). If the view has no printed level heights, still mark the
grade line and each storey floor line with a null label; heights then come from the sheet's
scale. Give the outer faces of the leftmost and rightmost main
walls. List every door and window of the main building with its storey and its outer frame
edges in pixels. Use the storey names given in the task."""


class PlanDoneSchema(BaseSchema):
    notes: str = Field(description="What was read and anything uncertain.")


@dataclass
class SheetDeps:
    sheet: Image.Image
    regions: list[tuple[int, int, int, int]]


@dataclass
class PlanDeps:
    view: Image.Image
    view_id: str
    storey: str
    save_draft: Callable[[PlanReadingSchema], None]
    reading: PlanReadingSchema | None = None
    compiled: CompiledPlanSchema | None = None
    issues: list[str] = field(default_factory=list)


@dataclass
class ElevationDeps:
    view: Image.Image
    view_id: str
    storeys: list[str]


type ImageDeps = SheetDeps | PlanDeps | ElevationDeps


def _image(deps: ImageDeps) -> Image.Image:
    return deps.sheet if isinstance(deps, SheetDeps) else deps.view


def view_region(
    ctx: RunContext[ImageDeps], left: int, top: int, right: int, bottom: int
) -> ToolReturn:
    """Render a rectangle of the image with a grid labelled in its pixel coordinates."""
    try:
        region = render_region(_image(ctx.deps), (left, top, right, bottom))
    except ValueError as error:
        raise ModelRetry(str(error)) from error
    return ToolReturn(
        return_value=f"region {left},{top},{right},{bottom} rendered",
        content=[BinaryContent(png_bytes(region), media_type="image/png")],
    )


def ink_runs(
    ctx: RunContext[ImageDeps], segments: list[tuple[float, float, float, float]]
) -> list[list[tuple[float, float]]]:
    """Centres of the ink lines crossed by each segment (x1, y1, x2, y2), in order along it.

    Measure everything you need in one call: pass all segments together.
    """
    image = _image(ctx.deps)
    return [ink_profile(image, (x1, y1), (x2, y2)) for x1, y1, x2, y2 in segments]


layout_agent = Agent(
    deps_type=SheetDeps,
    output_type=SheetLayoutSchema,
    instructions=LAYOUT_INSTRUCTIONS,
    name="layout",
    tools=[view_region],
)


@layout_agent.output_validator
def _check_layout(
    ctx: RunContext[SheetDeps], layout: SheetLayoutSchema
) -> SheetLayoutSchema:
    problems = []
    ids = [view.view_id for view in layout.views]
    if len(set(ids)) != len(ids):
        problems.append("view ids must be unique")
    count = len(ctx.deps.regions)
    owner: dict[int, str] = {}
    for view in layout.views:
        for number in view.regions:
            if not 0 <= number < count:
                problems.append(
                    f"{view.view_id}: there is no box {number}; boxes are 0..{count - 1}"
                )
            elif number in owner:
                problems.append(
                    f"box {number} is given to both {owner[number]} and {view.view_id}"
                )
            else:
                owner[number] = view.view_id
        if view.kind == "plan" and not view.storey:
            problems.append(f"{view.view_id}: plans need a storey")
        if view.kind == "elevation" and not view.facing:
            problems.append(f"{view.view_id}: elevations need a facing")
    if problems:
        raise ModelRetry("\n".join(problems))
    return layout


plan_agent = Agent(
    deps_type=PlanDeps,
    output_type=PlanDoneSchema,
    instructions=PLAN_INSTRUCTIONS,
    name="plan_reader",
    tools=[view_region, ink_runs],
)


@plan_agent.tool_plain
def check_chain(ticks_px: list[float], labels: list[str]) -> str:
    """Fit a dimension chain: ``labels`` are the segments between consecutive ticks."""
    try:
        scale = fit_chain(ticks_px, labels)
    except ValueError as error:
        return f"chain rejected: {error}"
    return f"{scale.px_per_m:.2f} px/m, largest misfit {scale.residual_m:.3f} m"


async def _compile_into(
    ctx: RunContext[PlanDeps], reading: PlanReadingSchema
) -> ToolReturn:
    reading = reading.model_copy(
        update={"view_id": ctx.deps.view_id, "storey": ctx.deps.storey}
    )
    ctx.deps.reading = reading
    ctx.deps.save_draft(reading)
    try:
        compiled = await asyncio.to_thread(compile_plan, reading)
    except CompileError as error:
        ctx.deps.compiled = None
        ctx.deps.issues = [str(issue) for issue in error.issues]
        return ToolReturn(
            return_value="Not compiled. Fix with patch_reading:\n" + str(error)
        )
    ctx.deps.compiled, ctx.deps.issues = compiled, []
    spaces = ", ".join(f"{s.id} {s.name}" for s in compiled.spaces)
    summary = (
        f"Compiled: {len(compiled.walls)} walls, {len(compiled.openings)} openings, "
        f"spaces: {spaces}. Scale {compiled.px_per_m:.1f} px/m."
    )
    if compiled.warnings:
        summary += "\nWarnings:\n" + "\n".join(compiled.warnings)
    preview = await asyncio.to_thread(
        render_plan_preview, ctx.deps.view, reading, compiled
    )
    return ToolReturn(
        return_value=summary + "\nThe image overlays the compiled plan on the drawing.",
        content=[BinaryContent(png_bytes(preview), media_type="image/png")],
    )


@plan_agent.tool(sequential=True)
async def set_reading(
    ctx: RunContext[PlanDeps], reading: PlanReadingSchema
) -> ToolReturn:
    """Replace the whole plan reading and compile it."""
    return await _compile_into(ctx, reading)


@plan_agent.tool(sequential=True)
async def patch_reading(
    ctx: RunContext[PlanDeps], patch: PlanPatchSchema
) -> ToolReturn:
    """Apply edits by object id to the current reading and compile it."""
    if ctx.deps.reading is None:
        raise ModelRetry("call set_reading first")
    try:
        reading = apply_patch(ctx.deps.reading, patch)
    except (KeyError, ValueError) as error:
        raise ModelRetry(f"patch rejected: {error}") from error
    return await _compile_into(ctx, reading)


@plan_agent.output_validator
def _check_plan(ctx: RunContext[PlanDeps], done: PlanDoneSchema) -> PlanDoneSchema:
    if ctx.deps.reading is None:
        raise ModelRetry("No reading yet: call set_reading before finishing.")
    try:
        ctx.deps.compiled = compile_plan(ctx.deps.reading)
    except CompileError as error:
        raise ModelRetry(
            f"The current reading does not compile:\n{error}\n"
            "Fix these objects with patch_reading, then finish."
        ) from error
    return done


STRUCTURE_CHECK_PX_PER_M: Final = 100.0
"""Stand-in scale for unlabelled elevations: the check covers structure, not heights."""


elevation_agent = Agent(
    deps_type=ElevationDeps,
    output_type=ElevationReadingSchema,
    instructions=ELEVATION_INSTRUCTIONS,
    name="elevation_reader",
    tools=[view_region, ink_runs],
)


@elevation_agent.output_validator
def _check_elevation(
    ctx: RunContext[ElevationDeps], reading: ElevationReadingSchema
) -> ElevationReadingSchema:
    reading = reading.model_copy(update={"view_id": ctx.deps.view_id})
    unknown = {o.storey.casefold() for o in reading.openings} - {
        s.casefold() for s in ctx.deps.storeys
    }
    if unknown:
        raise ModelRetry(
            f"use only these storey names: {ctx.deps.storeys}; got {sorted(unknown)}"
        )
    labelled = sum(1 for mark in reading.levels if mark.label)
    has_grade = any(mark.role == "grade" for mark in reading.levels)
    if labelled < 2 and not has_grade:
        raise ModelRetry(
            "mark the grade line, or give at least two level marks with heights"
        )
    structure_scale = None if labelled >= 2 else STRUCTURE_CHECK_PX_PER_M
    try:
        compile_elevation(reading, structure_scale)
    except CompileError as error:
        raise ModelRetry(
            "Fix these objects and return the whole reading again:\n" + str(error)
        ) from error
    return reading
