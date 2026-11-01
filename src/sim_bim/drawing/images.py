"""Image access for readers: views, gridded crops, ink profiles and compile previews."""

import io
import math
from pathlib import Path
from typing import Final

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

from sim_bim._share import Point2
from sim_bim.drawing.plan import CompiledPlanSchema
from sim_bim.drawing.readings import PlanReadingSchema

Image.MAX_IMAGE_PIXELS = 400_000_000

MAX_SIDE_PX: Final = 1600
INK_THRESHOLD: Final = 128
_FONT: Final = ImageFont.load_default(size=14)
_LABEL_FONT: Final = ImageFont.load_default(size=36)
REGION_CELLS: Final = 400
REGION_GAP_CELLS: Final = 2
MIN_REGION_SHARE: Final = 0.004
FRAME_SHARE: Final = 0.75


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


def _longest_runs(mask: np.ndarray) -> np.ndarray:
    """Length of the longest run of True in each row."""
    edges = np.diff(np.pad(mask.astype(np.int8), ((0, 0), (1, 1))), axis=1)
    rows, starts = np.nonzero(edges == 1)
    _, ends = np.nonzero(edges == -1)
    longest = np.zeros(mask.shape[0], dtype=np.int64)
    np.maximum.at(longest, rows, ends - starts)
    return longest


def candidate_regions(
    sheet: Image.Image, cells: int = REGION_CELLS, gap_cells: int = REGION_GAP_CELLS
) -> list[tuple[int, int, int, int]]:
    """Boxes around separate groups of ink on a sheet, in reading order.

    The sheet is reduced to about ``cells`` cells along its long side, each
    keeping its darkest pixel; rows and columns holding one unbroken ink run
    longer than three quarters of the sheet (the frame) are cleared, ink within ``gap_cells`` is joined, and boxes that
    overlap by more than half of the smaller one are merged. A drawing may
    still span several boxes, which readers group into views.
    """
    pixels = np.asarray(sheet)
    k = max(1, max(pixels.shape) // cells)
    h, w = pixels.shape[0] // k * k, pixels.shape[1] // k * k
    ink = pixels[:h, :w].reshape(h // k, k, w // k, k).min(axis=(1, 3)) < INK_THRESHOLD
    ink[_longest_runs(ink) > FRAME_SHARE * ink.shape[1], :] = False
    ink[:, _longest_runs(ink.T) > FRAME_SHARE * ink.shape[0]] = False
    labels, _ = ndimage.label(ndimage.binary_dilation(ink, iterations=gap_cells))
    boxes = [
        (s[1].start * k, s[0].start * k, s[1].stop * k, s[0].stop * k)
        for s in ndimage.find_objects(labels)
        if s is not None
        and (s[0].stop - s[0].start) * (s[1].stop - s[1].start)
        >= MIN_REGION_SHARE * ink.size
    ]
    merged = True
    while merged:
        merged = False
        for i, a in enumerate(boxes):
            for j in range(i + 1, len(boxes)):
                b = boxes[j]
                overlap_w = min(a[2], b[2]) - max(a[0], b[0])
                overlap_h = min(a[3], b[3]) - max(a[1], b[1])
                smaller = min(
                    (a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])
                )
                if (
                    overlap_w > 0
                    and overlap_h > 0
                    and overlap_w * overlap_h > smaller / 2
                ):
                    boxes[i] = (
                        min(a[0], b[0]),
                        min(a[1], b[1]),
                        max(a[2], b[2]),
                        max(a[3], b[3]),
                    )
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    return sorted(boxes, key=lambda box: (box[1] * 10 // sheet.height, box[0]))


def render_regions(
    sheet: Image.Image, boxes: list[tuple[int, int, int, int]], max_side: int = 2048
) -> Image.Image:
    """Overview of the sheet with every candidate region boxed and numbered."""
    shown = sheet.convert("RGB")
    shown.thumbnail((max_side, max_side))
    factor = shown.width / sheet.width
    draw = ImageDraw.Draw(shown)
    for number, (left, top, right, bottom) in enumerate(boxes):
        corners = [left * factor, top * factor, right * factor, bottom * factor]
        draw.rectangle(corners, outline=(220, 0, 0), width=3)
        draw.text(
            (corners[0] + 6, corners[1] + 4),
            str(number),
            fill=(220, 0, 0),
            font=_LABEL_FONT,
        )
    return shown


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
