"""Image loading and scale fitting from explicitly supplied dimension anchors."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any
import numpy as np
from PIL import Image

TOOL_VERSION = "1"
_CALIBRATION_RECIPE = {
    "recipe_id": "clean_vector_v1", "applicability": "clean_vector",
    "calibration_warn_residual_px": 2.0, "calibration_warn_residual_m": 0.05,
    "calibration_max_axis_relative_deviation": 0.003,
}


def _calibration_recipe(recipe_id: str) -> dict:
    if recipe_id != _CALIBRATION_RECIPE["recipe_id"]:
        raise ValueError(f"unknown calibration recipe {recipe_id!r}")
    return dict(_CALIBRATION_RECIPE)


def _load_rgb(image: str | Path | Image.Image) -> Image.Image:
    if isinstance(image, Image.Image):
        img = image
    else:
        img = Image.open(image)
    if img.mode in {"RGBA", "LA"} or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    return img.convert("RGB")


def _make_payload(
    tool: str,
    recipe: dict[str, Any],
    params: dict[str, Any],
    results: list[dict[str, Any]],
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "tool": tool,
        "tool_version": TOOL_VERSION,
        "recipe_id": recipe["recipe_id"],
        "params": params,
        "results": results,
        "diagnostics": diagnostics or {},
        "applicability": recipe["applicability"],
    }


def _base_result(
    *,
    candidate_id: str,
    candidate_kind: str,
    anchor_px: dict[str, Any],
    tool: str,
    recipe: dict[str, Any],
    visual_confidence: str = "medium",
    metric_confidence: str = "medium",
    metric: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metric_payload = {
        "dimension_refs": [],
        "raw_segments": [],
        "scale_px_per_m": None,
        "residuals": [],
        "confidence": metric_confidence,
    }
    if metric:
        metric_payload.update(metric)
    return {
        "candidate_id": candidate_id,
        "candidate_kind": candidate_kind,
        "coord_space": "source_px",
        "anchor_px": anchor_px,
        "visual": {
            "anchor_px": anchor_px,
            "relative": {},
            "confidence": visual_confidence,
        },
        "metric": metric_payload,
        "provenance": {
            "tool": tool,
            "tool_version": TOOL_VERSION,
            "recipe_id": recipe["recipe_id"],
            "source_image_sha256": "",
            "crop_chain_id": "",
        },
    }


def _span_px(anchor: dict[str, Any]) -> float:
    axis = anchor["axis"]
    if axis == "xy":
        a = anchor["px_a"]
        b = anchor["px_b"]
        return math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
    return abs(float(anchor["px_b"]) - float(anchor["px_a"]))


def px_m_calibrator(
    anchors: list[dict[str, Any]],
    *,
    recipe_id: str = "clean_vector_v1",
    residual_warn_px: float | None = None,
    residual_warn_m: float | None = None,
    source_name: str = "image",
) -> dict[str, Any]:
    recipe = _calibration_recipe(recipe_id)
    if not anchors:
        raise ValueError("anchors must not be empty")
    spans = np.array([_span_px(anchor) for anchor in anchors], dtype=float)
    values = np.array([float(anchor["value_m"]) for anchor in anchors], dtype=float)
    if np.any(values <= 0):
        raise ValueError("anchor value_m must be positive")
    if np.any(spans <= 0):
        raise ValueError("anchor pixel span must be positive")

    axis_px_per_m = {}
    for axis in ("x", "y"):
        axis_indices = [index for index, anchor in enumerate(anchors) if anchor["axis"] == axis]
        if axis_indices:
            axis_values = values[axis_indices]
            axis_spans = spans[axis_indices]
            axis_px_per_m[axis] = float(
                np.sum(axis_values * axis_spans) / np.sum(axis_values**2)
            )
    axis_relative_deviation = None
    max_axis_relative_deviation = float(
        recipe["calibration_max_axis_relative_deviation"]
    )
    # Keep cross-axis disagreement visible without replacing the measured scale.
    warnings: list[dict[str, Any]] = []
    axis_calibration_disagreement = False
    if set(axis_px_per_m) == {"x", "y"}:
        x_scale = axis_px_per_m["x"]
        y_scale = axis_px_per_m["y"]
        axis_relative_deviation = abs(x_scale - y_scale) / ((x_scale + y_scale) / 2.0)
        if axis_relative_deviation > max_axis_relative_deviation:
            axis_calibration_disagreement = True
            warnings.append(
                {
                    "type": "cross_axis_disagreement",
                    "x_px_per_m": x_scale,
                    "y_px_per_m": y_scale,
                    "relative_deviation": axis_relative_deviation,
                    "relative_deviation_limit": max_axis_relative_deviation,
                    "guidance": (
                        f"x={x_scale:.9g} px/m and y={y_scale:.9g} px/m disagree by "
                        f"{axis_relative_deviation:.6%}, over the "
                        f"{max_axis_relative_deviation:.6%} limit — at least one endpoint "
                        "pair or transcribed dimension is likely wrong. Do not silently "
                        "trust the blended px_per_m below: re-crop and re-measure both "
                        "axes' anchors at their dimension-chain extension-line "
                        "intersections, then recalibrate. If you must proceed now, prefer "
                        "the single-axis scale (axis_px_per_m) for geometry measured along "
                        "that axis over the blended value, and record this disagreement "
                        "instead of treating the reading as clean."
                    ),
                }
            )
    px_per_m = float(np.sum(values * spans) / np.sum(values**2))
    m_per_px = 1.0 / px_per_m
    residuals: list[dict[str, Any]] | None
    rmse_px: float | None
    rmse_m: float | None
    if len(anchors) == 1:
        residuals = None
        rmse_px = None
        rmse_m = None
    else:
        residuals = []
        for anchor, span, value in zip(anchors, spans, values, strict=True):
            residual_px = float(span - px_per_m * value)
            residual_m = float(residual_px / px_per_m)
            residuals.append(
                {
                    "axis": anchor["axis"],
                    "dimension_ref": anchor.get("dimension_ref"),
                    "span_px": float(span),
                    "value_m": float(value),
                    "residual_px": residual_px,
                    "residual_m": residual_m,
                }
            )
        rmse_px = float(math.sqrt(np.mean([r["residual_px"] ** 2 for r in residuals])))
        rmse_m = float(math.sqrt(np.mean([r["residual_m"] ** 2 for r in residuals])))

    warn_px = float(residual_warn_px if residual_warn_px is not None else recipe["calibration_warn_residual_px"])
    warn_m = float(residual_warn_m if residual_warn_m is not None else recipe["calibration_warn_residual_m"])
    # NOTE: `warnings` was seeded above (possibly with a cross_axis_disagreement
    # entry) — appended to here, never reset, so neither warning source can
    # clobber the other.
    if residuals is not None:
        for residual in residuals:
            if abs(residual["residual_px"]) > warn_px or abs(residual["residual_m"]) > warn_m:
                warnings.append({"type": "residual_exceeds_threshold", **residual})

    raw_segments = [
        {
            "axis": anchor["axis"],
            "px_a": anchor["px_a"],
            "px_b": anchor["px_b"],
            "value_m": anchor["value_m"],
            "dimension_ref": anchor.get("dimension_ref"),
        }
        for anchor in anchors
    ]
    dimension_refs = [a["dimension_ref"] for a in anchors if a.get("dimension_ref")]
    if axis_calibration_disagreement:
        metric_confidence = "low"
    elif warnings:
        metric_confidence = "medium"
    else:
        metric_confidence = "high"
    result = _base_result(
        candidate_id=f"{Path(source_name).stem}:px_m_calibrator:001:scale",
        candidate_kind="calibration",
        anchor_px={"kind": "span", "value": {"anchors": raw_segments}},
        tool="px_m_calibrator",
        recipe=recipe,
        visual_confidence="high",
        metric_confidence=metric_confidence,
        metric={
            "dimension_refs": dimension_refs,
            "raw_segments": raw_segments,
            "scale_px_per_m": px_per_m,
            "axis_px_per_m": axis_px_per_m,
            "axis_relative_deviation": axis_relative_deviation,
            "axis_relative_deviation_limit": max_axis_relative_deviation,
            "axis_calibration_disagreement": axis_calibration_disagreement,
            "residuals": residuals,
        },
    )
    result.update(
        {
            "px_per_m": px_per_m,
            "m_per_px": m_per_px,
            "rmse_px": rmse_px,
            "rmse_m": rmse_m,
            "residuals": residuals,
            "warnings": warnings,
            "axis_px_per_m": axis_px_per_m,
            "axis_relative_deviation": axis_relative_deviation,
            "axis_relative_deviation_limit": max_axis_relative_deviation,
            "axis_calibration_disagreement": axis_calibration_disagreement,
        }
    )
    return _make_payload(
        "px_m_calibrator",
        recipe,
        {
            "anchors": anchors,
            "residual_warn_px": warn_px,
            "residual_warn_m": warn_m,
            "max_axis_relative_deviation": max_axis_relative_deviation,
        },
        [result],
        {"anchor_count": len(anchors), "warnings": warnings},
    )
