"""Numeric axis-alignment predicate for orthogonal plan edges."""

ORTHOGONALITY_EPSILON = 1e-9


def edge_is_axis_aligned(dx: float, dy: float, *, epsilon: float = ORTHOGONALITY_EPSILON) -> bool:
    """Accept an axis-aligned edge within the floating-point comparison tolerance."""
    return abs(dx) <= epsilon or abs(dy) <= epsilon
