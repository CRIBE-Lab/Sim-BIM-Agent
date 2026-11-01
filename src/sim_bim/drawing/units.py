"""Dimension label parsing and pixel-to-metre calibration."""

import re
from dataclasses import dataclass
from typing import Final

import numpy as np

FOOT_M: Final = 0.3048
INCH_M: Final = 0.0254
_TYPOGRAPHIC_MARKS: Final = str.maketrans(
    {"\u2019": "'", "\u2032": "'", "\u201d": '"', "\u2033": '"'}
)
_METRIC_FACTORS: Final = {None: 0.001, "mm": 0.001, "cm": 0.01, "m": 1.0}

_IMPERIAL = re.compile(
    r"""^\s*(?:(?P<feet>\d+(?:\.\d+)?)\s*'\s*-?\s*)?
    (?:(?P<inches>\d+(?:\.\d+)?)?\s*(?:(?P<num>\d+)\s*/\s*(?P<den>\d+))?\s*")?\s*$""",
    re.VERBOSE,
)
_METRIC = re.compile(r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>mm|cm|m)?\s*$")


def parse_length(label: str) -> float:
    """Convert a dimension label to metres.

    Imperial labels such as ``24'-4 1/2"``, ``6'-6"`` or ``5 1/2"`` are read as
    feet and inches. A bare number is read as millimetres, matching metric
    drawing convention; ``m`` and ``cm`` suffixes are honoured.

    Raises:
        ValueError: If the label is neither a recognised imperial nor metric length.
    """
    text = (
        label.translate(_TYPOGRAPHIC_MARKS).replace("±", "").replace("+/-", "").strip()
    )
    if ("'" in text or '"' in text) and (match := _IMPERIAL.match(text)):
        feet = float(match["feet"] or 0)
        inches = float(match["inches"] or 0)
        if match["num"]:
            inches += int(match["num"]) / int(match["den"])
        return feet * FOOT_M + inches * INCH_M
    if match := _METRIC.match(text):
        value = float(match["value"])
        return value * _METRIC_FACTORS[match["unit"]]
    raise ValueError(f"unrecognised dimension label {label!r}")


@dataclass(frozen=True)
class AxisScale:
    """Linear map from a pixel coordinate on one axis to metres."""

    px_per_m: float
    origin_px: float
    residual_m: float

    def to_m(self, px: float) -> float:
        return (px - self.origin_px) / self.px_per_m


def fit_chain(ticks_px: list[float], labels: list[str]) -> AxisScale:
    """Fit a pixel scale to a dimension chain.

    ``ticks_px`` are the pixel positions of the chain's tick marks in order and
    ``labels`` the segment labels between consecutive ticks. The scale is the
    least-squares slope of cumulative metres against tick pixels; the residual
    is the largest absolute misfit in metres.

    Raises:
        ValueError: If the chain has fewer than two ticks, the label count does
            not match, or ticks are not strictly monotonic.
    """
    if len(ticks_px) < 2 or len(labels) != len(ticks_px) - 1:
        raise ValueError(
            f"dimension chain needs n ticks and n-1 labels, got {len(ticks_px)} ticks and {len(labels)} labels"
        )
    ticks = np.asarray(ticks_px, dtype=float)
    steps = np.diff(ticks)
    if not (np.all(steps > 0) or np.all(steps < 0)):
        raise ValueError("dimension chain ticks must be strictly monotonic")
    cumulative = np.concatenate(
        [[0.0], np.cumsum([parse_length(label) for label in labels])]
    )
    slope, intercept = np.polyfit(cumulative, ticks, 1)
    residual = np.abs((ticks - intercept) / slope - cumulative).max()
    return AxisScale(
        px_per_m=float(abs(slope)),
        origin_px=float(intercept),
        residual_m=float(residual),
    )


def fit_levels(marks_px: list[float], labels: list[str]) -> AxisScale:
    """Fit a vertical scale to level marks, each labelled with its height above datum.

    Image rows grow downwards while heights grow upwards; convert rows with
    :func:`height_m`, not :meth:`AxisScale.to_m`.

    Raises:
        ValueError: If fewer than two marks are given or counts differ.
    """
    if len(marks_px) < 2 or len(marks_px) != len(labels):
        raise ValueError(
            f"level fit needs at least two labelled marks, got {len(marks_px)}"
        )
    heights = np.array([parse_length(label) for label in labels])
    y = np.asarray(marks_px, dtype=float)
    slope, intercept = np.polyfit(heights, y, 1)
    if slope >= 0:
        raise ValueError(
            "level marks must sit higher on the image as their heights increase"
        )
    residual = np.abs((y - intercept) / slope - heights).max()
    return AxisScale(
        px_per_m=float(-slope), origin_px=float(intercept), residual_m=float(residual)
    )


def height_m(scale: AxisScale, y_px: float) -> float:
    """Height above datum for an image row, given a scale from :func:`fit_levels`."""
    return (scale.origin_px - y_px) / scale.px_per_m
