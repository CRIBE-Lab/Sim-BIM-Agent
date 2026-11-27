"""Build a new, independently deliverable run from a verified workbench result."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import tempfile
import uuid

from src.agent.viewer_publication import VIEWER_FILENAME
from src.agent_runtime.providers import is_custom_provider
from . import runs
from .storage import InputError, checked_name, inside, locked, read_json, save_json


def _digest(file):
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _check_draft(value):
    invalid = InputError("修改内容无效，请重新确认批注和北向。", "Invalid edits. Review your comments and north setting.")
    if not isinstance(value, dict):
        raise invalid
    notes, instructions, angle = value.get("notes"), value.get("instructions"), value.get("northAngle")
    if not isinstance(notes, list) or not isinstance(instructions, str):
        raise invalid
    if angle is not None and not (type(angle) in (float, int) and 0 <= angle < 360):
        raise invalid
    if not notes and not instructions.strip() and angle is None:
        raise InputError("请先添加一项修改。", "Add an edit before submitting.")
    ids = set()
    for note in notes:
        if (not isinstance(note, dict) or not isinstance(note.get("id"), str) or not note["id"]
                or note["id"] in ids or not isinstance(note.get("text"), str) or not note["text"].strip()
                or not isinstance(note.get("targets"), list) or not note["targets"]):
            raise invalid
        ids.add(note["id"])
        for target in note["targets"]:
            if (not isinstance(target, dict) or target.get("kind") not in ("zone", "floor", "surface")
                    or not all(isinstance(target.get(k), str) for k in ("id", "label", "zone", "surface"))
                    or not target["id"] or not isinstance(target.get("point"), list) or len(target["point"]) != 3
                    or not all(type(n) in (float, int) and -1e308 <= n <= 1e308 for n in target["point"])):
                raise invalid
    return {"notes": notes, "instructions": instructions.strip(), "northAngle": angle}


def _check_targets(draft, model):
    groups = {k: {v["id"] for v in model[t]} for k, t in (("floor", "floors"), ("zone", "spaces"),
                                                             ("surface", "boundaries"))}
    groups["surface"].update(o["id"] for o in model["openings"])
    for note in draft["notes"]:
        for t in note["targets"]:
            if t["id"] not in groups[t["kind"]]:
                raise InputError("所选构件不属于这个模型版本，请重新点选。",
                                 "The selected object is missing from this model version. Select it again.", 409)


def _known_presentation(parent, candidate, model_id, actual, original):
    # Some shipped viewers were redrawn without changing the signed model.
    records = inside(parent, "delivery-revisions")
    for path in records.glob("*.json"):
        record = read_json(inside(records, path.name))
        if (record.get("schema") == "sim-bim-presentation-revision.v1" and record.get("run") == parent.name
                and record.get("candidate") == candidate and record.get("source_model_identity") == model_id
                and record.get("original_finish", {}).get("viewer_sha256") == original
                and record.get("presentation", {}).get("sha256") == actual):
            return True
    return False


def _verified_parent(parent, model_id, edits):
    status_path = inside(parent, "status.json")
    if not status_path.is_file() or runs.status_at(parent)["status"] != "completed":
        raise InputError("请选择已交付的模型。", "Choose a delivered model.", 409)
    invalid = InputError("原版本的交付文件不完整或已改变，无法基于它修订。",
                         "The parent delivery is incomplete or its files have changed.", 409)
    terminal_path = inside(parent, "production/control/terminal.json")
    if not terminal_path.is_file():
        raise invalid
    terminal = read_json(terminal_path)
    proof = terminal.get("proof") or {}
    candidate = proof.get("candidate")
    if (terminal.get("delivered") is not True or proof.get("origin") != "domain_tool"
            or proof.get("tool_name") != "finish_bim" or not isinstance(candidate, str)
            or not re.fullmatch(r"candidate_[0-9]+", candidate)):
        raise invalid
    bim = inside(parent, "production/private/dsh/work/bim")
    hashes = proof.get("artifact_sha256", {})
    if "inputs.json" in hashes:
        manifest = inside(bim, "inputs.json")
        if not manifest.is_file() or _digest(manifest) != hashes["inputs.json"]:
            raise invalid
    source = inside(bim, candidate + "/source_model.json")
    files = ["source_model.json", "sim_inspection.json"]
    if candidate + "/mesh_review.json" in hashes:
        files.append("mesh_review.json")
    for name in files:
        relative = candidate + "/" + name
        artifact = inside(bim, relative)
        if not artifact.is_file() or _digest(artifact) != hashes.get(relative):
            raise invalid
    model = read_json(source)
    if not isinstance(model_id, str) or not re.fullmatch(r"[0-9a-f]{64}", model_id) or model["source_model_sha256"] != model_id:
        raise InputError("模型版本已改变，请重新打开并确认修改。",
                         "The model version has changed. Reopen it and review the edits.", 409)
    _check_targets(edits, model)
    viewer = inside(parent, VIEWER_FILENAME)
    original = hashes.get(candidate + "/viewer.html")
    if not isinstance(original, str) or not viewer.is_file():
        raise invalid
    actual = _digest(viewer)
    if actual != original and not _known_presentation(parent, candidate, model_id, actual, original):
        raise invalid
    if not edits["notes"] and not edits["instructions"] and edits["northAngle"] is not None:
        from src.agent.model_revision import north_axis_degrees
        if north_axis_degrees(model) == (-edits["northAngle"]) % 360:
            raise InputError("当前模型已经使用这个北向，无需创建新版本。",
                             "The model already uses this north bearing. No new version is needed.")
    return bim, {"parentRun": parent.name, "parentCandidate": candidate, "parentModelId": model_id,
                 "parentSourceSha256": hashes[candidate + "/source_model.json"], "parentViewerSha256": actual,
                 "draft": edits}


def _freeze_inputs(parent, prepared, original):
    # Re-read only materials submitted for the parent run, independent of changes to the workspace draft.
    groups = {"mesh": "mesh", "image": "images", "declaration": "declarations"}
    expected = []
    for block in original["blocks"]:
        for asset in block["assets"]:
            filename = checked_name(asset["file"])
            checked_name(asset["name"])
            if asset["path"] != f"inputs/{groups[asset['kind']]}/{filename}":
                raise InputError("原始素材记录无效。", "Invalid frozen material reference.", 409)
            expected.append(asset["sha256"])
    materials, blocks = runs.freeze(parent, prepared, original)
    if [asset["sha256"] for block in blocks for asset in block["assets"]] != expected:
        raise InputError("原版本的冻结素材已经改变，请恢复原文件。",
                         "The parent run's frozen materials have changed. Restore the originals.", 409)
    return materials, blocks


def _existing(root, request_id, parent, model_id, edits):
    for entry in inside(root, "runs").glob("run-*"):
        if not runs.RUN_ID.fullmatch(entry.name):
            continue
        directory = runs.run_dir(root, entry.name)
        path = inside(directory, "input.json")
        if not path.is_file():
            continue
        saved = read_json(path)
        if saved.get("editRequestId") != request_id:
            continue
        if (saved.get("parentRunId") != parent.name or saved.get("parentModelId") != model_id
                or saved.get("edits") != edits):
            raise InputError("这个提交编号已用于其他修改。", "This submission ID was used for other edits.", 409)
        return runs.status_at(directory)
    return None


def begin_revision(root, body, connection_view):
    from src.agent.model_revision import freeze_revision_parent
    if body.get("workspaceTag") != runs.workspace_tag(root):
        raise InputError("当前工作区已切换，请重新打开原模型。", "The workspace changed. Reopen the original model.", 409)
    try:
        request_id = str(uuid.UUID(body["requestId"]))
    except (KeyError, AttributeError, ValueError, TypeError) as exc:
        raise InputError("修改请求编号无效。", "Invalid edit request ID.") from exc
    parent = runs.run_dir(root, body.get("parentRunId"))
    edits = _check_draft(body.get("draft"))
    model_id = body.get("parentModelId")
    with locked(root):
        prior = _existing(root, request_id, parent, model_id, edits)
        if prior:
            return prior
        bim, revision = _verified_parent(parent, model_id, edits)
        original = read_json(inside(parent, "input.json"))
        parent_case = read_json(inside(parent, "case.json"))["cases"][0]
        local = not edits["notes"] and not edits["instructions"]
        executable = parent_case.get("dsh_executable") if local else runs.workbench_executable()
        if not local:
            catalog = connection_view(original["locale"])
            selected = {route["provider"] for route in [original["coordinator"], *original["workers"]]}
            current_revisions = {row["provider"]: row["revision"] for row in catalog
                                 if row["provider"] in selected and is_custom_provider(row["provider"])}
            connections = runs.model_connections(original, catalog, current_revisions)
        directory = runs.new_run_directory(root)
        with tempfile.TemporaryDirectory(dir=directory.parent, prefix=".preparing-edit-") as staging:
            prepared = Path(staging) / "run"
            prepared.mkdir()
            materials, blocks = _freeze_inputs(parent, prepared, original)
            try:
                revision["snapshotSha256"] = freeze_revision_parent(
                    bim, prepared / "inputs/revision-parent", candidate=revision["parentCandidate"],
                    source_sha256=revision["parentSourceSha256"])
            except (ValueError, KeyError, OSError) as exc:
                raise InputError("无法读取原版本的建模记录。请检查原运行文件是否完整。",
                                 "The parent modelling records could not be read. Check the original run files.", 409) from exc
            # Retain the parent's model choices and run policy. Each model-backed edit pins its own transport.
            case = {key: parent_case[key] for key in (
                "provider", "model", "workers", "worker", "reasoning_effort", "output_tokens", "context_tokens",
                "image_history_batches", "max_candidates", "limits", "connection_revisions") if key in parent_case}
            if not local and current_revisions:
                configured = runs.configuration(directory, original, materials, blocks, executable=executable,
                                                connections=connections)["cases"][0]
                case["connection_revisions"] = configured["connection_revisions"]
                if case["provider"] in current_revisions:
                    case.update(context_tokens=configured["context_tokens"], output_tokens=configured["output_tokens"])
                if any(worker["provider"] in current_revisions for worker in original["workers"]):
                    case["worker"] = {**case.get("worker", {}), "output_tokens": configured["worker"]["output_tokens"]}
            case.update(case_id=directory.name, mode="orchestrator", runtime="dsh", output="production",
                        materials=materials, blocks=blocks, locale=original["locale"], scope=original["taskBrief"],
                        thickness_candidates_mm=original.get("thicknessCandidatesMm", {}), revision=revision)
            if executable:
                case["dsh_executable"] = str(executable)
            save_json(prepared / "case.json", {"schema_version": 1, "approval_required_before_launch": True,
                "maximum_seconds_per_case": case["limits"]["seconds"], "batch_id": directory.name,
                "input_root": str(directory / "inputs"), "run_root": str(directory), "cases": [case]})
            snapshot = read_json(prepared / "input.json")
            snapshot.update(parentRunId=parent.name, parentModelId=model_id, editRequestId=request_id, edits=edits)
            save_json(prepared / "input.json", snapshot)
            started = {"id": directory.name, "status": "running", "parentRunId": parent.name, "message": ""}
            save_json(prepared / "status.json", started)
            prepared.rename(directory)
        return runs.launch_prepared(directory, original["locale"], started)
