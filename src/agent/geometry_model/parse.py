"""Parse a supported geometry input without repairing or changing its shape."""
from __future__ import annotations

from src.agent.geometry_model.constants import SCHEMA_VERSION_V2, SCHEMA_VERSION_SOURCE_POLYGON
from src.agent.geometry_model.schema import GeometryInput


def parse_geometry(value: dict | GeometryInput) -> GeometryInput:
    raw = value if isinstance(value, dict) else value.model_dump(mode="python")
    version = str(raw.get("schema_version", SCHEMA_VERSION_V2))
    if version == SCHEMA_VERSION_SOURCE_POLYGON:
        from src.agent.geometry_model.source_polygon import SourcePolygonGeometry
        return SourcePolygonGeometry.model_validate(raw)
    if version == SCHEMA_VERSION_V2:
        return GeometryInput.model_validate(raw)
    raise ValueError(f"unsupported geometry schema_version {version!r}")
