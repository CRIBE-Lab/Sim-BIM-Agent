"""Geometry tolerances used by explicit plan alignment operations."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import yaml

GEOMETRY_CONFIG_ENV = "SIM_BIM_GEOMETRY_CONFIG"
_DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "configs" / "geometry.yaml"


@dataclass(frozen=True)
class GeometryTolerances:
    min_edge_length_m: float


def resolve_geometry_config_path() -> Path:
    path = Path(os.environ.get(GEOMETRY_CONFIG_ENV, _DEFAULT_CONFIG))
    if not path.is_file():
        raise FileNotFoundError(f"geometry configuration does not exist: {path}")
    return path.resolve()


@lru_cache(maxsize=8)
def _load_cached(path: str) -> GeometryTolerances:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("geometry configuration must be a mapping")
    values = data.get("geometry", data)
    minimum = float(values["min_edge_length_m"])
    if not math.isfinite(minimum) or minimum <= 0:
        raise ValueError("min_edge_length_m must be finite and positive")
    return GeometryTolerances(min_edge_length_m=minimum)


def load_geometry_tolerances() -> GeometryTolerances:
    return _load_cached(str(resolve_geometry_config_path()))
