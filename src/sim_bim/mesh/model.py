"""Load a textured GLB into one z-up triangle soup with per-face colour sources."""

from dataclasses import dataclass
from pathlib import Path
from typing import Self, cast

import numpy as np
import trimesh
from numpy.typing import NDArray
from PIL import Image


@dataclass
class MeshModel:
    """Triangles in metres, z up, with either a texture or a colour for every face."""

    vertices: NDArray[np.float64]
    faces: NDArray[np.int64]
    uv: NDArray[np.float64]
    face_texture: NDArray[np.int64]
    textures: list[NDArray[np.uint8]]
    face_colour: NDArray[np.uint8]

    def transformed(self, matrix: NDArray[np.float64]) -> Self:
        return type(self)(
            vertices=trimesh.transform_points(self.vertices, matrix),
            faces=self.faces,
            uv=self.uv,
            face_texture=self.face_texture,
            textures=self.textures,
            face_colour=self.face_colour,
        )


def _texture(geometry: trimesh.Trimesh) -> Image.Image | None:
    material = getattr(geometry.visual, "material", None)
    image = getattr(material, "baseColorTexture", None) or getattr(
        material, "image", None
    )
    return image if isinstance(image, Image.Image) else None


def load_glb(path: Path) -> MeshModel:
    """Load every mesh node of a GLB with its scene transform, converted from y up to z up.

    Faces without a texture keep their vertex or face colour; texture lookups use
    ``face_texture`` (``-1`` means use ``face_colour``).

    Raises:
        ValueError: If the file holds no triangles.
    """
    scene = cast(trimesh.Scene, trimesh.load(path, force="scene", process=False))
    y_up_to_z_up = trimesh.transformations.rotation_matrix(np.pi / 2, (1, 0, 0))
    vertices, faces, uvs, face_texture, colours = [], [], [], [], []
    textures: list[NDArray[np.uint8]] = []
    texture_index: dict[int, int] = {}
    offset = 0
    for node in scene.graph.nodes_geometry:
        transform, name = scene.graph.get(node)
        geometry = scene.geometry[name]
        if not isinstance(geometry, trimesh.Trimesh) or len(geometry.faces) == 0:
            continue
        points = trimesh.transform_points(geometry.vertices, y_up_to_z_up @ transform)
        vertices.append(points)
        faces.append(geometry.faces + offset)
        offset += len(points)
        uv = getattr(geometry.visual, "uv", None)
        uvs.append(
            np.asarray(uv, float) if uv is not None else np.zeros((len(points), 2))
        )
        image = _texture(geometry)
        if image is not None and uv is not None:
            key = id(image)
            if key not in texture_index:
                texture_index[key] = len(textures)
                textures.append(np.asarray(image.convert("RGB")))
            face_texture.append(np.full(len(geometry.faces), texture_index[key]))
            colours.append(np.full((len(geometry.faces), 3), 200, np.uint8))
        else:
            face_texture.append(np.full(len(geometry.faces), -1))
            colours.append(
                np.asarray(geometry.visual.face_colors)[:, :3].astype(np.uint8)
            )
    if not faces:
        raise ValueError(f"{path} contains no triangles")
    return MeshModel(
        vertices=np.vstack(vertices),
        faces=np.vstack(faces).astype(np.int64),
        uv=np.vstack(uvs),
        face_texture=np.concatenate(face_texture).astype(np.int64),
        textures=textures,
        face_colour=np.vstack(colours),
    )
