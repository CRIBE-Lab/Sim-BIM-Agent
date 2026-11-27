"""Admit the editable lineage and modelling evidence of one delivered parent."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import time

from src.agent.bim_inputs import dump


CANDIDATE = re.compile(r"candidate_[0-9]+")
UNCHANGED_REVISION = ".unchanged-revision"
_CANDIDATE_FILES = {"source_model.json", "proposal.json", "report.json", "parametric_plan.json",
                    "parametric_state.json", "sim_inspection.json", "mesh_review.json"}
# Only editable domain records and their supporting views cross into a new run.
# Provider traffic, runtime state, past handoffs and generated viewers stay behind.
_EVIDENCE = {
    "architectural_drafts": r"draft_\d+(?:\.[a-z_]+)?\.json",
    "parametric_drafts": r"draft_\d+(?:\.[a-z_]+)?\.json",
    "parametric_patches": r"patch_\d+\.json",
    "image_views": r"view_\d+\.json",
    "mesh_observations": r"mesh_\d+\.(?:json|png|npz)",
    "mesh_sections": r"section_\d+\.(?:json|png)",
    "mesh_evidence": r"(?:correspondence|directions|measure)_\d+\.json",
    "overlay_calibrations": r"calibration_\d+\.json",
    "plan_assemblies": r"assembly_\d+\.json",
    "claims": r"(?:application|claim|decision)_\d+\.json",
    "opening_reviews": r"review_\d+\.json",
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _read(path):
    return json.loads(path.read_bytes())


def _member(root, relative):
    parts = relative.split("/")
    if len(parts) != 2:
        raise ValueError("Invalid revision member")
    folder, name = parts
    if not ((CANDIDATE.fullmatch(folder) and name in _CANDIDATE_FILES)
            or (folder in _EVIDENCE and re.fullmatch(_EVIDENCE[folder], name))):
        raise ValueError("Invalid revision member")
    directory = root / folder
    path = directory / name
    if directory.is_symlink() or directory.is_junction() or path.is_symlink() or not path.is_file():
        raise ValueError(f"Revision member must be a regular file: {relative}")
    return path


def _materials(manifest):
    return {"images": {k: v["sha256"] for k, v in manifest["images"].items()},
            "mesh": (manifest.get("mesh_input") or {}).get("sha256"),
            "declaration": (manifest.get("building_input") or {}).get("raw_sha256")}


def freeze_revision_parent(parent_bim: Path, destination: Path, *, candidate: str, source_sha256: str):
    """Copy only modelling artifacts. The returned digest binds the frozen index to the case."""
    from scripts.tool_scripts.bim_agent_parametric_lineage import _read_node
    from scripts.tool_scripts.run_bim_agent import Toolkit
    toolkit = Toolkit(parent_bim)
    destination.mkdir(parents=True, exist_ok=False)
    chain, members = [], []
    name, expected_proposal = candidate, None
    while name:
        if not CANDIDATE.fullmatch(name) or name in chain:
            raise ValueError("Invalid parent candidate lineage")
        for required in ("source_model.json", "proposal.json", "report.json"):
            _member(parent_bim, f"{name}/{required}")
        node = _read_node(toolkit, name, {})
        if not chain and node["source_sha256"] != source_sha256:
            raise ValueError("Delivered parent source changed")
        if expected_proposal and node["proposal_sha256"] != expected_proposal:
            raise ValueError("Parent proposal does not match its recorded lineage")
        chain.append(name)
        members.extend(f"{name}/{key}" for key in sorted(_CANDIDATE_FILES) if (parent_bim / name / key).exists())
        expected_proposal = node["provenance"].get("parent_proposal_sha256")
        name = node["provenance"].get("parent_candidate")
    if not chain:
        raise ValueError("A revision needs a delivered candidate")
    for folder, pattern in _EVIDENCE.items():
        directory = parent_bim / folder
        if directory.is_dir():
            members.extend(f"{folder}/{path.name}" for path in directory.iterdir()
                           if re.fullmatch(pattern, path.name))
    files = {}
    for relative in members:
        path = _member(parent_bim, relative)
        target = destination / relative
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(path, target)
        files[relative] = digest(target)
    if files[f"{candidate}/source_model.json"] != source_sha256:
        raise ValueError("Delivered parent changed during snapshot")
    dump(destination / "snapshot.json", {"candidate": candidate, "ancestors": chain,
         "materials": _materials(toolkit.manifest), "revisionEvidence": toolkit.manifest.get("revisionEvidence", {}),
         "files": files})
    return digest(destination / "snapshot.json")


def current_revision_evidence(revision):
    draft = revision["draft"]
    stamp = hashlib.sha256(json.dumps(draft, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    prefix = f"{revision['parentRun']}/{stamp}"
    current = {f"{prefix}/instructions": draft["instructions"]} if draft["instructions"].strip() else {}
    current.update({f"{prefix}/notes/{i}": note["text"] for i, note in enumerate(draft["notes"], 1)})
    return current


def admit_revision(run, revision, directory):
    """Populate a new, independently writable BIM workspace with verified parent bytes."""
    index = directory / "snapshot.json"
    if index.is_symlink() or digest(index) != revision["snapshotSha256"]:
        raise ValueError("Revision snapshot changed before admission")
    frozen = _read(index)
    base = revision["parentCandidate"]
    manifest = _read(run / "inputs.json")
    if (frozen["candidate"] != base or frozen["files"][f"{base}/source_model.json"] != revision["parentSourceSha256"]
            or _materials(manifest) != frozen["materials"]):
        raise ValueError("Revision inputs differ from the delivered parent")
    for relative, expected in frozen["files"].items():
        source = _member(directory, relative)
        target = run / relative
        target.parent.mkdir(exist_ok=True)
        with source.open("rb") as stream, target.open("xb") as dest:
            shutil.copyfileobj(stream, dest)
        if digest(target) != expected:
            raise ValueError(f"Revision input changed: {relative}")
    draft = revision["draft"]
    refs = frozen["revisionEvidence"].copy()
    current = current_revision_evidence(revision)
    refs.update(current)
    manifest["revisionEvidence"] = refs
    manifest["model_revision"] = {**revision, "inheritedCandidates": frozen["ancestors"]}
    dump(run / "inputs.json", manifest)
    selected = _target_rows(_read(run / base / "source_model.json"), _read(run / base / "proposal.json"), draft)
    sources = ["revision:" + ref for ref in current]
    task = ("Revise the delivered building, starting from its exact local candidate " + base + ". "
            "The notes express the user's intent; include affected neighbors and preserve unrelated work. "
            "Use the inherited inspections, materials, mesh observations and floor/zone IDs. "
            "Use revise_bim on the parent proposal. To translate a shared partition between polygon rooms, use "
            "replace_space_region with space_id, neighbor_space_id, the first room's full polygon in its saved "
            "vertex order, reason and source_refs. Shift only the two endpoints of the common edge. The neighbor "
            "is derived from the same union and openings retain their positions. Use inspect_parametric_plan "
            "for an edit to a saved parameter. For a window sill/head offset, "
            "one revise_bim operation "
            "shift_window_heights takes ids, floor_ids or all:true and sill_delta_m and/or head_delta_m, "
            "with reason and source_refs pointing to the confirmed edit. "
            "A positive delta lifts an edge; omit a delta to retain that edge. "
            "For attribute-only edits, revise_bim with operations_json='[]' creates "
            "an editable child retaining geometry; set properties on that child with record_sim_inspection "
            "or finish_bim inspection_json. Apply the requested attributes to the specified objects and finish "
            "a new descendant. Use the saved child's revision_inspection counts and missing_spatial:true "
            "selectors for the remaining spatial assessments. Apply the same grade and reason only to objects "
            "sharing the same evidence. Narrow by type, floor_ids or ids as needed. The confirmed compass "
            "bearing is applied on save.\n"
            "Confirmed edit request: " + json.dumps(draft, ensure_ascii=False) +
            "\nCurrent user evidence references (instructions first if present, then notes in order): " +
            json.dumps(sources) + "\nSelected source objects: " + json.dumps(selected, ensure_ascii=False))
    return task


def _target_rows(source, proposal, draft):
    inventory = {kind: {r["id"]: r for r in source[group]} for kind, group in
                 (("zone", "spaces"), ("floor", "floors"), ("surface", "boundaries"))}
    inventory["surface"].update({row["id"]: row for row in source["openings"]})
    apertures = {row["id"]: (group, row) for group in ("windows", "openings")
                 for row in proposal["geometry"].get(group, [])}
    selected = {}
    for note in draft["notes"]:
        for target in note["targets"]:
            key = (target["kind"], target["id"])
            row = inventory[key[0]].get(key[1])
            if row is not None:
                # Include working dimensions and current host, without loading a whole floor's geometry.
                fields = ("id", "kind", "name", "space_id", "other_space_id", "floor_id", "z_floor", "height",
                          "p1", "p2", "z", "vertices", "host_boundary_id", "type", "role")
                selected[key] = {k: row[k] for k in fields if k in row}
                if key[1] in source.get("opening_hosts", {}):
                    selected[key]["host_boundaries"] = source["opening_hosts"][key[1]]
                    if key[1] in apertures:
                        group, aperture = apertures[key[1]]
                        selected[key]["editable_collection"] = group
                        selected[key]["editable_aperture"] = {k: aperture[k] for k in (
                            "id", "floor", "facade", "room", "space_id", "other_space_id", "span",
                            "p1", "p2", "z", "kind") if k in aperture}
    return list(selected.values())


def child_north_axis(toolkit):
    revision = toolkit.manifest["model_revision"]
    value = revision["draft"]["northAngle"]
    if value is None:
        parent = _read(toolkit.candidate_path(revision["parentCandidate"]) / "source_model.json")
        return parent["coordinate_system"].get("north_axis")
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 360:
        raise ValueError("Invalid north bearing")
    return {"value_deg": (-value) % 360, "source": "from user"}


def north_axis_degrees(source):
    axis = source.get("coordinate_system", {}).get("north_axis")
    value = axis.get("value_deg") if isinstance(axis, dict) else axis
    return value % 360 if type(value) in (int, float) else None


def _ring(points):
    # The starting vertex and winding do not change a face's extent. Keep edge
    # order so rings using the same vertices with different edges stay distinct.
    vertices = tuple(tuple(point) for point in points)
    if len(vertices) > 1 and vertices[0] == vertices[-1]:
        vertices = vertices[:-1]
    if not vertices:
        return ()
    first = vertices.index(min(vertices))
    ring = vertices[first:] + vertices[:first]
    return min(ring, ring[:1] + ring[:0:-1])


def _face(row, outline, holes="holes"):
    return _ring(row.get(outline, ())), sorted(_ring(hole) for hole in row.get(holes, ()))


def _building_content(source):
    # Compare the built objects and their public semantics, independently of
    # candidate/geometry digests (both can include evidence and build history).
    mesh = source.get("mesh_frame") or {}
    return {
        "north": north_axis_degrees(source),
        "mesh_placement": (mesh.get("yaw_degrees"), mesh.get("translation_m")),
        "floors": {f["id"]: (f["name"], f["z_floor"], f["height"], _face(f, "footprint", "footprint_holes"),
                            sorted(f.get("spanning_space_ids", ()))) for f in source["floors"]},
        "spaces": {s["id"]: (s["role"], s["floor_id"], s["z_floor"], s["height"], s.get("enclosure"),
                            _face(s, "polygon"), [(v["z"], _face(v, "polygon")) for v in s.get("vertical_sections", ())])
                   for s in source["spaces"]},
        "boundaries": {b["id"]: (b["space_id"], b["geometry_type"], b["kind"], b.get("enclosure"), _face(b, "vertices"),
                                sorted(b.get("counterpart_ids", ())), sorted(b.get("adjacent_space_ids", ())),
                                sorted((r["condition"], _face(r, "vertices")) for r in b.get("enclosure_regions", ())))
                       for b in source["boundaries"]},
        "openings": {o["id"]: (o["kind"], o["connectivity"], o["exterior"], o["host_boundary_id"],
                              sorted(o["space_ids"]), _face(o, "vertices")) for o in source["openings"]},
        "contacts": {tuple(sorted(r["boundary_ids"])): sorted(_face(region, "vertices") for region in r["regions"])
                     for r in source.get("boundary_relations", ())},
    }


def require_revision_change(toolkit, source, inspection):
    """Reject an unchanged Edit before any new viewer or finish receipt is written."""
    from scripts.tool_scripts.bim_viewer_inspection import public_inspection
    directory = toolkit.candidate_path(toolkit.manifest["model_revision"]["parentCandidate"])
    parent = _read(directory / "source_model.json")
    changed = _building_content(parent) != _building_content(source)
    if not changed:
        parent_inspection = _read(directory / "sim_inspection.json")
        changed = public_inspection(parent, parent_inspection) != public_inspection(source, inspection)
    # Keep the outcome of the most recent comparison for the workbench if the
    # run ultimately stops. A subsequent successful edit supersedes the marker.
    marker = toolkit.run / UNCHANGED_REVISION
    if changed:
        marker.unlink(missing_ok=True)
        return
    marker.touch()
    raise ValueError("No visible change from the delivered parent. Its geometry, room uses, north, "
                     "surface properties and public confidence explanations are unchanged. "
                     "Apply the requested edit before finishing. If the parent already meets the request, "
                     "report that it is already satisfied without claiming a new delivery.")


def require_revision_descendant(toolkit, candidate):
    revision = toolkit.manifest.get("model_revision")
    if not revision:
        return
    inherited = set(revision["inheritedCandidates"])
    seen = set()
    name = candidate
    while name not in inherited:
        if not isinstance(name, str) or not CANDIDATE.fullmatch(name) or name in seen:
            break
        seen.add(name)
        folder = toolkit.candidate_path(name)
        source = _read(folder / "source_model.json")
        provenance = source["generation"]["provenance"]
        name = provenance.get("parent_candidate")
        if not name or digest(toolkit.candidate_path(name) / "proposal.json") != provenance["parent_proposal_sha256"]:
            break
        if name == revision["parentCandidate"]:
            base = toolkit.candidate_path(name) / "source_model.json"
            if (digest(base) == revision["parentSourceSha256"]
                    and _read(base)["generation"]["proposal_sha256"] == provenance["parent_proposal_sha256"]):
                return
            raise ValueError("The admitted revision parent changed")
    raise ValueError("Finish a new child of the delivered revision parent " + revision["parentCandidate"])


async def finish_north_revision(run, client):
    """Carry the parent's assessed scope across an explicit compass-only change."""
    from scripts.tool_scripts.bim_agent_saved_result import result_metadata
    from src.dsh_runtime.handoff import attest_local_finish
    manifest = _read(run / "inputs.json")
    parent = manifest["model_revision"]["parentCandidate"]
    original = _read(run / parent / "source_model.json")
    result = await client.call_tool("revise_bim", {"candidate": parent, "operations_json": "[]"})
    created = result_metadata(result)
    candidate = created.get("candidate")
    if result.get("isError") or not candidate or not created.get("source_geometry_ready"):
        raise ValueError("Could not prepare the north adjustment: " + str(created.get("error", "no saved candidate")))
    source = _read(run / candidate / "source_model.json")
    if (original["source_geometry_sha256"] != source["source_geometry_sha256"]
            or source["coordinate_system"].get("north_axis", {}).get("value_deg") !=
            (-manifest["model_revision"]["draft"]["northAngle"]) % 360):
        raise ValueError("North adjustment changed geometry or failed to preserve the confirmed bearing")
    summary = ("Retains the delivered parent's assessed building extent. The verified source geometry is "
               "identical; this revision applies the user-confirmed true north bearing.")
    if manifest.get("mesh_input"):
        review = {"mesh_review": {"coverage": "whole_building", "summary": summary, "overlay_ids": []}}
    else:
        review = {"drawing_review": {"coverage": "whole_building", "summary": summary,
                                    "unresolved": source["generation"].get("unresolved", [])}}
    finished = await client.call_tool("finish_bim", {"candidate": candidate, "reviews_json": json.dumps(review)})
    result = result_metadata(finished)
    proof = attest_local_finish(run, candidate, result)
    if finished.get("isError") or not proof:
        raise ValueError("Could not attest the north adjustment: " + str(result.get("error", "incomplete handoff")))
    return {"ended": time.time(), "exit_code": 0, "status": "delivered", "delivered": True,
            "model_requests": 0, "tool_calls_reserved": 2, "proof": {**proof, "observed_in": "local north revision"}}
