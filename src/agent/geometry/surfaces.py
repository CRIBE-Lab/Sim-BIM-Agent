"""Surface orientation, cardinal windows and wall-plane coordinate transforms."""
from __future__ import annotations

import math
import numpy as np
from shapely.geometry import Polygon

WALL_PLANE_EPS_M = 2e-6
FACADE_NORMALS = {"north": (0.0, 1.0), "south": (0.0, -1.0),
                  "east": (1.0, 0.0), "west": (-1.0, 0.0)}


class OpeningBindingError(ValueError):
    """An aperture cannot be represented on its explicitly selected wall."""


def polygon_normal(verts: list[tuple[float, float, float]]) -> np.ndarray:
    pts = np.asarray(verts, float)
    n = np.zeros(3)
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        n[0] += (a[1] - b[1]) * (a[2] + b[2])
        n[1] += (a[2] - b[2]) * (a[0] + b[0])
        n[2] += (a[0] - b[0]) * (a[1] + b[1])
    m = np.linalg.norm(n)
    return n / m if m > 1e-12 else n


def orient_vertices(verts: list, desired: np.ndarray) -> list:
    """Reverse vertex order if the polygon normal opposes `desired`."""
    if float(np.dot(polygon_normal(verts), desired)) < 0:
        return verts[::-1]
    return verts


def cardinal_window_vertices(w, wall_vertices: list) -> list:
    """Window rectangle on the parent wall's plane, CCW from outside."""
    span = sorted(float(s) for s in w.span)
    z0, z1 = sorted(float(z) for z in w.z)
    axis = "x" if w.facade.lower().startswith(("e", "w")) else "y"
    if axis == "y":  # N/S: plane y=const
        y = wall_vertices[0][1]
        v = [(span[0], y, z0), (span[1], y, z0), (span[1], y, z1), (span[0], y, z1)]
    else:            # E/W: plane x=const
        x = wall_vertices[0][0]
        v = [(x, span[0], z0), (x, span[1], z0), (x, span[1], z1), (x, span[0], z1)]
    # orient to match the parent wall's outward normal
    return orient_vertices(v, polygon_normal(wall_vertices))


def wall_frame(p1, p2):
    length = math.dist(p1, p2)
    if not math.isfinite(length) or length <= 0:
        raise OpeningBindingError("opening wall line must have positive finite length")
    return ((p2[0] - p1[0]) / length, (p2[1] - p1[1]) / length), length


def project_to_wall(vertices, origin, direction):
    result = []
    for x, y, z in vertices:
        dx, dy = x - origin[0], y - origin[1]
        if abs(dx * direction[1] - dy * direction[0]) > WALL_PLANE_EPS_M:
            return None
        result.append((dx * direction[0] + dy * direction[1], z))
    return Polygon(result)


def lift_from_wall(ring, origin, direction):
    return [(origin[0] + t * direction[0], origin[1] + t * direction[1], z) for t, z in ring]
