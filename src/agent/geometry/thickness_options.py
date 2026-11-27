"""User-selected nominal thicknesses, in millimetres. Empty groups use the model defaults."""
import sys


PRESETS_MM = {
    "wall": [60, 75, 90, 100, 120, 150, 180, 200, 240, 250, 300, 370, 400, 490, 500, 600, 800],
    "horizontal": [80, 100, 120, 150, 180, 200, 250, 300, 350, 400, 500, 600],
    "roof": [50, 75, 100, 120, 150, 180, 200, 240, 300, 400, 500],
    "glazing": [3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 28, 32, 36, 40, 48],
}


def normalize_candidates(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - PRESETS_MM.keys():
        raise ValueError("Unknown thickness group. Use wall, horizontal, roof or glazing.")
    result = {}
    for group in PRESETS_MM:
        values = value.get(group, [])
        if (not isinstance(values, list) or
                any(type(n) not in (int, float) or not 0 < n <= sys.float_info.max for n in values)):
            raise ValueError("Thickness candidates must be positive, finite millimetres.")
        if values:
            result[group] = sorted(set(values))
    return result
