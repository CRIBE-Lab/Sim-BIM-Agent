"""Locale-independent, file-safe storey codes for standard BIM names."""
import re
import unicodedata
from collections import Counter


_ALIASES = {
    "G": ("ground", "ground floor", "ground level", "首层", "首層", "地面层", "地面層"),
    "U": ("upper", "upper ground", "upper ground floor", "upper retail", "商业上层", "商業上層", "上首层", "上首層"),
    "R": ("roof", "roof level", "roof floor", "屋顶", "屋頂", "屋面", "屋面层", "屋面層", "天台"),
    "M": ("mezzanine", "夹层", "夾層"),
}
_ALIAS_CODES = {alias: code for code, aliases in _ALIASES.items() for alias in aliases}
_CODE = re.compile(r"(?:[A-Z]{1,3}(?:[0-9]{1,4}[A-Z]?)?|[0-9]{1,4}[A-Z]?)", re.ASCII)
_NUMBERED = (
    ("O", r"(?:office|办公|辦公)(?:\s*(?:floor|level|层|層))?\s*[- ]?\s*([0-9]{1,4})(?:层|層)?"),
    ("B", r"(?:basement|地下|负|負)\s*([0-9]{1,4})(?:层|層)?"),
    ("F", r"(?:floor|level|第)\s*([0-9]{1,4})(?:层|層)?|([0-9]{1,4})(?:层|層|楼|樓)"),
)
_SHAFT = re.compile(r"贯通|貫通|竖井|豎井|(?:^|[\s_-])(?:shaft|stack)(?:$|[\s_-])", re.I)
_PLANT = re.compile(r"机房|機房|机组|機組|排气|排氣|(?:^|[\s_-])(?:plant|plantroom|equipment|exhaust)(?:$|[\s_-])", re.I)


def _label(value):
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _preferred(label):
    text = label.casefold()
    if code := _ALIAS_CODES.get(text):
        return code
    for prefix, pattern in _NUMBERED:
        if match := re.fullmatch(pattern, text):
            number = int(next(value for value in match.groups() if value is not None))
            return f"O{number:02d}" if prefix == "O" else f"{prefix}{number}"
    # G/F and 23/F are common notations. Slashes never enter plan filenames.
    code = re.sub(r"(?<=[A-Z0-9])\s*/\s*F$", "F", label.upper(), flags=re.ASCII)
    return code if label.isascii() and _CODE.fullmatch(code) else None


def public_storey_codes(floors):
    """Return unique public codes keyed by source ID; source labels are read-only.

    Reserve supplied codes before naming translated, unnamed or auxiliary groups
    so a historical alias cannot take an explicitly identified storey's code.
    """
    rows = sorted(floors, key=lambda f: (f["z_floor"], f["id"]))
    labels = {f["id"]: [_label(f.get("name")), _label(f["id"])] for f in rows}
    wanted = {}
    for f in rows:
        fid = f["id"]
        for label in labels[fid]:
            if code := _preferred(label):
                wanted[fid] = (code, 0 if label.upper() == code else 1)
                break
        else:
            # Partial archives may retain an explicit public code alongside a
            # descriptive source label that doesn't identify a storey.
            if code := _preferred(_label(f.get("archived_code"))):
                wanted[fid] = (code, 2)
    assigned, used = {}, set()
    for fid in sorted(wanted, key=lambda key: wanted[key][1]):
        code, _ = wanted[fid]
        if code not in used:
            assigned[fid] = code
            used.add(code)
    roof_levels = [f["z_floor"] for f in rows if assigned.get(f["id"]) in {"R", "RF"}]
    sequence = Counter()
    for ordinal, f in enumerate(rows, 1):
        fid = f["id"]
        if fid in assigned:
            continue
        text = " ".join(labels[fid])
        if _SHAFT.search(text):
            prefix = "S"
        elif _PLANT.search(text):
            prefix = "RP" if roof_levels and f["z_floor"] >= min(roof_levels) else "ME"
        else:
            prefix = "F"
        number = ordinal if prefix == "F" else sequence[prefix] + 1
        while True:
            code = f"F{number}" if prefix == "F" else f"{prefix}{number:02d}"
            if code not in used:
                break
            number += 1
        assigned[fid] = code
        used.add(code)
        sequence[prefix] = number
    return {f["id"]: assigned[f["id"]] for f in rows}
