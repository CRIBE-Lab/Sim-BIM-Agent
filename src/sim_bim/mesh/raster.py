"""Vectorised orthographic rendering of facades with a z-buffer."""

from dataclasses import dataclass
from typing import Final, assert_never

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from sim_bim.drawing.readings import Facing
from sim_bim.mesh.model import MeshModel

CHUNK_PIXELS: Final = 2_000_000
BACKGROUND: Final = (190, 230, 185)
MARGIN_SHARE: Final = 0.05
_VIEW_DIRECTION: Final[dict[Facing, tuple[float, float, float]]] = {
    "south": (0.0, -1.0, 0.0),
    "north": (0.0, 1.0, 0.0),
    "east": (1.0, 0.0, 0.0),
    "west": (-1.0, 0.0, 0.0),
}


@dataclass(frozen=True)
class FacadeProjection:
    """Pixel ↔ metre map of one facade image.

    ``u`` runs left to right as seen by a viewer outside the ``facing`` side,
    measured from the building's left extent; heights are above ground.
    """

    facing: Facing
    px_per_m: float
    u_min: float
    z_max: float

    def u_m(self, x_px: float) -> float:
        return self.u_min + x_px / self.px_per_m

    def z_m(self, y_px: float) -> float:
        return self.z_max - y_px / self.px_per_m


def _screen(
    model: MeshModel, facing: Facing
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Horizontal screen coordinate and depth (smaller is nearer) per vertex."""
    x, y = model.vertices[:, 0], model.vertices[:, 1]
    match facing:
        case "south":
            return x, y
        case "north":
            return -x, -y
        case "east":
            return y, -x
        case "west":
            return -y, x
        case _:
            assert_never(facing)


def _shade(model: MeshModel, facing: Facing) -> NDArray[np.float64]:
    """Brightness factor per face, darker as faces turn away from the viewer and the light.

    The range is narrow so textures that already carry lighting stay legible,
    while untextured walls of different orientation become distinguishable.
    """
    triangles = model.vertices[model.faces]
    normals = np.cross(
        triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
    )
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
    view = np.asarray(_VIEW_DIRECTION[facing])
    light = view + np.array([0.3, 0.3, 0.8])
    light /= np.linalg.norm(light)
    facing_viewer = np.abs(normals @ view)
    lit = np.abs(normals @ light)
    return 0.6 + 0.25 * facing_viewer + 0.15 * lit


def render_facade(
    model: MeshModel, facing: Facing, px_per_m: float
) -> tuple[Image.Image, FacadeProjection]:
    """Render the ``facing`` side orthographically at ``px_per_m``."""
    u, depth = _screen(model, facing)
    z = model.vertices[:, 2]
    margin = MARGIN_SHARE * float(max(u.max() - u.min(), z.max() - z.min()))
    u_min, z_max = float(u.min()) - margin, float(z.max()) + margin
    width = int(np.ceil((u.max() + margin - u_min) * px_per_m)) + 1
    height = int(np.ceil((z_max - z.min() + margin) * px_per_m)) + 1
    points = np.stack([(u - u_min) * px_per_m, (z_max - z) * px_per_m], axis=-1)[
        model.faces
    ]
    depths = depth[model.faces]
    a, b, c = points[:, 0], points[:, 1], points[:, 2]
    denominator = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (
        a[:, 1] - c[:, 1]
    )
    x0 = np.clip(np.floor(points[:, :, 0].min(1)).astype(np.int64), 0, width - 1)
    x1 = np.clip(np.ceil(points[:, :, 0].max(1)).astype(np.int64), 0, width - 1)
    y0 = np.clip(np.floor(points[:, :, 1].min(1)).astype(np.int64), 0, height - 1)
    y1 = np.clip(np.ceil(points[:, :, 1].max(1)).astype(np.int64), 0, height - 1)
    nx, ny = x1 - x0 + 1, y1 - y0 + 1
    candidates = np.flatnonzero(np.abs(denominator) > 1e-12)
    z_buffer = np.full(width * height, np.inf)
    face_buffer = np.full(width * height, -1, np.int64)
    weights_buffer = np.zeros((width * height, 3))

    def barycentric(
        face: NDArray[np.int64], sx: NDArray[np.floating], sy: NDArray[np.floating]
    ) -> tuple[NDArray[np.floating], NDArray[np.floating], NDArray[np.floating]]:
        A, B, C, d = a[face], b[face], c[face], denominator[face]
        l0 = (
            (B[:, 1] - C[:, 1]) * (sx - C[:, 0]) + (C[:, 0] - B[:, 0]) * (sy - C[:, 1])
        ) / d
        l1 = (
            (C[:, 1] - A[:, 1]) * (sx - C[:, 0]) + (A[:, 0] - C[:, 0]) * (sy - C[:, 1])
        ) / d
        return l0, l1, 1 - l0 - l1

    counts = (nx * ny)[candidates]
    ends = np.cumsum(counts)
    start = 0
    while start < len(candidates):
        stop = int(
            np.searchsorted(
                ends, (ends[start - 1] if start else 0) + CHUNK_PIXELS, "right"
            )
        )
        stop = max(stop, start + 1)
        chunk = candidates[start:stop]
        start = stop
        k = (nx * ny)[chunk]
        face = np.repeat(chunk, k)
        local = np.arange(k.sum()) - np.repeat(np.cumsum(k) - k, k)
        xx = x0[face] + local % nx[face]
        yy = y0[face] + local // nx[face]
        l0, l1, l2 = barycentric(face, xx + 0.5, yy + 0.5)
        inside = (l0 >= -1e-9) & (l1 >= -1e-9) & (l2 >= -1e-9)
        face, xx, yy, l0, l1, l2 = (
            face[inside],
            xx[inside],
            yy[inside],
            l0[inside],
            l1[inside],
            l2[inside],
        )
        zz = l0 * depths[face, 0] + l1 * depths[face, 1] + l2 * depths[face, 2]
        pixel = yy * width + xx
        order = np.lexsort((zz, pixel))
        pixel, zz, face = pixel[order], zz[order], face[order]
        weights = np.stack([l0, l1, l2], -1)[order]
        first = np.r_[True, pixel[1:] != pixel[:-1]]
        pixel, zz, face, weights = pixel[first], zz[first], face[first], weights[first]
        nearer = zz < z_buffer[pixel]
        z_buffer[pixel[nearer]] = zz[nearer]
        face_buffer[pixel[nearer]] = face[nearer]
        weights_buffer[pixel[nearer]] = weights[nearer]

    image = np.full((width * height, 3), BACKGROUND, np.uint8)
    covered = np.flatnonzero(face_buffer >= 0)
    face = face_buffer[covered]
    texture_of = model.face_texture[face]
    plain = texture_of < 0
    image[covered[plain]] = model.face_colour[face[plain]]
    weights = weights_buffer[covered]
    uv = np.einsum("ij,ijk->ik", weights, model.uv[model.faces[face]])
    for texture_id in np.unique(texture_of[~plain]):
        mask = texture_of == texture_id
        texture = model.textures[texture_id]
        h, w = texture.shape[:2]
        u_t, v_t = np.mod(uv[mask, 0], 1), np.mod(uv[mask, 1], 1)
        rows = np.clip(((1 - v_t) * h).astype(np.int64), 0, h - 1)
        cols = np.clip((u_t * w).astype(np.int64), 0, w - 1)
        image[covered[mask]] = texture[rows, cols]
    image[covered] = (image[covered] * _shade(model, facing)[face, None]).astype(
        np.uint8
    )
    projection = FacadeProjection(
        facing=facing, px_per_m=px_per_m, u_min=u_min, z_max=z_max
    )
    return Image.fromarray(image.reshape(height, width, 3)), projection
