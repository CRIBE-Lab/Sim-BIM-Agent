"""Image access for readers: views, gridded crops, ink profiles and compile previews."""

import io
import math
from pathlib import Path
from typing import Final

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from sim_bim._share import Point2
from sim_bim.drawing.plan import CompiledPlanSchema
from sim_bim.drawing.readings import PlanReadingSchema

Image.MAX_IMAGE_PIXELS = 400_000_000

MAX_SIDE_PX: Final = 1600
INK_THRESHOLD: Final = 128
_FONT: Final = ImageFont.load_default(size=14)


def load_sheet(path: Path) -> Image.Image:
    """Load a drawing sheet as 8-bit greyscale."""
    with Image.open(path) as image:
        return image.convert("L")


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def _grid_step(span_px: float, lines: int = 8) -> int:
    raw = span_px / lines
    magnitude = 10.0 ** math.floor(math.log10(raw))
    candidates = [m * magnitude for m in (1.0, 2.0, 5.0, 10.0)]
    best = min(candidates, key=lambda step: abs(step - raw))
    return max(1, int(best))


def render_region(
    image: Image.Image, box: tuple[int, int, int, int], max_side: int = MAX_SIDE_PX
) -> Image.Image:
    """Crop ``box`` from ``image`` and draw a grid labelled in source pixel coordinates.

    The returned image is scaled to at most ``max_side`` pixels, but every grid
    label is a coordinate in ``image``, so a reader can report positions
    without knowing the scale factor.
    """
    left, top, right, bottom = _clamp(box, image.size)
    crop = image.crop((left, top, right, bottom))
    factor = min(1.0, max_side / max(crop.size))
    shown = crop.resize(
        (max(1, round(crop.width * factor)), max(1, round(crop.height * factor))),
        Image.Resampling.LANCZOS,
    ).convert("RGB")
    draw = ImageDraw.Draw(shown)
    step = _grid_step(max(right - left, bottom - top))
    for x in range(math.ceil(left / step) * step, right, step):
        sx = (x - left) * factor
        draw.line([(sx, 0), (sx, shown.height)], fill=(0, 140, 255), width=1)
        draw.text((sx + 2, 2), str(x), fill=(0, 90, 200), font=_FONT)
    for y in range(math.ceil(top / step) * step, bottom, step):
        sy = (y - top) * factor
        draw.line([(0, sy), (shown.width, sy)], fill=(0, 140, 255), width=1)
        draw.text((2, sy + 2), str(y), fill=(0, 90, 200), font=_FONT)
    return shown


def _clamp(
    box: tuple[int, int, int, int], size: tuple[int, int]
) -> tuple[int, int, int, int]:
    left, top, right, bottom = box
    left, right = sorted((max(0, left), min(size[0], right)))
    top, bottom = sorted((max(0, top), min(size[1], bottom)))
    if right - left < 4 or bottom - top < 4:
        raise ValueError(f"box {box} is empty inside an image of size {size}")
    return left, top, right, bottom


def ink_profile(
    image: Image.Image, start: Point2, end: Point2
) -> list[tuple[float, float]]:
    """Ink runs crossed by the segment from ``start`` to ``end``.

    Returns ``(centre_x, centre_y)`` image coordinates of each dark run, in
    order from ``start``. Measuring across a wall gives its two faces as one
    run each when the wall is drawn as two lines, or one run for a solid wall.
    """
    pixels = np.asarray(image)
    length = math.dist(start, end)
    samples = max(2, int(length) + 1)
    xs = np.clip(
        np.linspace(start[0], end[0], samples).round().astype(int),
        0,
        pixels.shape[1] - 1,
    )
    ys = np.clip(
        np.linspace(start[1], end[1], samples).round().astype(int),
        0,
        pixels.shape[0] - 1,
    )
    dark = pixels[ys, xs] < INK_THRESHOLD
    edges = np.flatnonzero(np.diff(np.concatenate([[0], dark.astype(int), [0]])))
    runs = []
    for begin, finish in zip(edges[::2], edges[1::2], strict=True):
        t = float((begin + finish - 1) / 2 / (samples - 1))
        runs.append(
            (
                round(start[0] + t * (end[0] - start[0]), 1),
                round(start[1] + t * (end[1] - start[1]), 1),
            )
        )
    return runs


def render_plan_preview(
    view: Image.Image, reading: PlanReadingSchema, plan: CompiledPlanSchema
) -> Image.Image:
    """Draw the compiled plan back onto the view so the reader can compare it with the ink."""
    scale = plan.px_per_m
    datum = reading.datum_px

    def to_px(point: Point2) -> Point2:
        return (datum[0] + point[0] * scale, datum[1] - point[1] * scale)

    canvas = view.convert("RGB")
    draw = ImageDraw.Draw(canvas, "RGBA")
    for space in plan.spaces:
        ring = [to_px(p) for p in space.polygon]
        draw.polygon(ring, fill=(0, 200, 0, 50), outline=(0, 150, 0, 255))
        cx = sum(p[0] for p in ring) / len(ring)
        cy = sum(p[1] for p in ring) / len(ring)
        draw.text((cx, cy), f"{space.id} {space.name}", fill=(0, 110, 0), font=_FONT)
    walls = {wall.id: wall for wall in plan.walls}
    for wall in plan.walls:
        colour = (220, 0, 0) if wall.kind == "wall" else (230, 140, 0)
        draw.line([to_px(wall.start), to_px(wall.end)], fill=colour, width=3)
        mid = to_px(
            ((wall.start[0] + wall.end[0]) / 2, (wall.start[1] + wall.end[1]) / 2)
        )
        draw.text(mid, wall.id, fill=colour, font=_FONT)
    for opening in plan.openings:
        wall = walls[opening.wall_id]
        length = wall.length_m
        ux, uy = (
            (wall.end[0] - wall.start[0]) / length,
            (wall.end[1] - wall.start[1]) / length,
        )
        a = opening.offset_m - opening.width_m / 2
        b = opening.offset_m + opening.width_m / 2
        p1 = to_px((wall.start[0] + ux * a, wall.start[1] + uy * a))
        p2 = to_px((wall.start[0] + ux * b, wall.start[1] + uy * b))
        draw.line([p1, p2], fill=(0, 60, 255), width=7)
    factor = min(1.0, MAX_SIDE_PX / max(canvas.size))
    return canvas.resize((round(canvas.width * factor), round(canvas.height * factor)))
