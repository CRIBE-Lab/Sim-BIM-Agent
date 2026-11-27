"""Freeze a draft and launch the same DSH product case used by the CLI."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

import portalocker
from src.agent.viewer_publication import VIEWER_FILENAME
from src.agent_runtime.output_limits import default_output_tokens
from src.agent_runtime.providers import is_custom_provider
from .custom_connections import freeze_custom_connection
from .storage import (ROOT, InputError, inside, locked, read_json, read_project,
                      revision_check, save_json)

RUN_ID = re.compile(r"run-\d{8}-\d{6}-[a-f0-9]{8}\Z")
OUTPUT_FILE = re.compile(r"(?:delivery\.(?:html|json)|candidate_\d+/(?:viewer\.html|(?:source_model|report|proposal)\.json|[^/\\:]+\.(?:glb|png)))\Z")


def workspace_tag(root):
    return hashlib.sha256(os.fsencode(str(root))).hexdigest()[:16]


def run_dir(root, identity):
    if not isinstance(identity, str) or not RUN_ID.fullmatch(identity):
        raise InputError("未找到运行。", "Run not found.", 404)
    return inside(root, "runs/" + identity)


def freeze(root, target, draft):
    materials, blocks = {}, []
    if not draft["coordinator"]:
        raise InputError("请选择主控模型。", "Select a coordinator model.")
    files = [a for b in draft["blocks"] for a in b["assets"]]
    if not any(a["kind"] in ("image", "mesh") for a in files):
        raise InputError("请至少添加一张 PNG 或一个 GLB。", "Add at least one PNG or GLB.")
    if sum(a["kind"] == "mesh" for a in files) > 1:
        raise InputError("一次建模支持一个 GLB，请选定本次使用的网格。", "Choose one GLB for this run.")
    counts = Counter(a["name"].casefold() for a in files)
    used = set(counts)
    declarations = []
    for block_number, b in enumerate(draft["blocks"], start=1):
        assets = []
        for file_number, a in enumerate(b["assets"], start=1):
            original = a["name"]
            alias = original
            if counts[original.casefold()] > 1:
                alias = a["id"] + "-" + original
                while alias.casefold() in used:
                    alias = "_" + alias
                used.add(alias.casefold())
            group = {"image": "images", "mesh": "mesh", "declaration": "declarations"}[a["kind"]]
            rel = Path("inputs") / group / alias
            dest = inside(target, str(rel))
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(inside(root, a["path"]), dest)
            with dest.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            assets.append({"id": a["id"], "number": file_number, "name": original, "kind": a["kind"],
                           "file": alias, "path": rel.as_posix(), "sha256": digest})
            if a["kind"] == "image":
                materials["images"] = "images"
            elif a["kind"] == "mesh":
                materials["mesh"] = (Path(group) / alias).as_posix()
            else:
                declarations.append((b["id"], alias, dest))
        blocks.append({"id": b["id"], "number": block_number, "caption": b["caption"], "assets": assets})
    if len(declarations) == 1:
        materials["building_input"] = "declarations/" + declarations[0][1]
    elif declarations:
        save_json(target / "inputs/building.json", {"declarations": [
            {"block": bid, "file": name, "content": read_json(path)} for bid, name, path in declarations]})
        materials["building_input"] = "building.json"
    save_json(target / "input.json", {"schema": "sim-bim-run.v1", "revision": draft["revision"],
              "locale": draft["locale"], "blocks": blocks, "taskBrief": draft["taskBrief"],
              "thicknessCandidatesMm": draft.get("thicknessCandidatesMm", {}),
              "coordinator": draft["coordinator"], "workers": draft["workers"]})
    return materials, blocks


def configuration(directory, draft, materials, blocks, *, executable, connections=None):
    route = draft["coordinator"]
    revisions = {}
    custom = {}
    for choice in [route, *draft["workers"]]:
        provider = choice["provider"]
        if is_custom_provider(provider) and provider not in revisions:
            entry = (connections or {}).get(provider)
            if not entry:
                raise InputError("模型连接已更新，请刷新后重试。", "The model connection changed. Refresh and try again.", 409)
            revisions[provider] = freeze_custom_connection(provider, expected_revision=entry["revision"])
            custom[provider] = entry
    case = {"case_id": directory.name, "mode": "orchestrator", "runtime": "dsh", "output": "production",
            "dsh_executable": str(executable),
            "materials": materials, "blocks": blocks, "locale": draft["locale"],
            "thickness_candidates_mm": draft.get("thicknessCandidatesMm", {}),
            "scope": draft["taskBrief"].strip(),
            **route, "workers": draft["workers"], "reasoning_effort": "high",
            "output_tokens": default_output_tokens(route["model"]), "image_history_batches": 3,
            "max_candidates": 256,
            "limits": {"model_calls": 100, "tool_calls": 500, "seconds": 5400, "tokens": None, "time_budget_mode": "wall"}}
    if revisions:
        case["connection_revisions"] = revisions
    if route["provider"] in custom:
        entry = custom[route["provider"]]
        case.update(context_tokens=entry["contextWindow"], output_tokens=entry["outputTokens"])
    if draft["workers"]:
        builtin_limits = [default_output_tokens(w["model"]) for w in draft["workers"] if w["provider"] not in custom]
        case["worker"] = {"output_tokens": max(builtin_limits or [custom[w["provider"]]["outputTokens"] for w in draft["workers"]])}
    return {"schema_version": 1, "approval_required_before_launch": True, "maximum_seconds_per_case": 5400,
            "batch_id": directory.name, "input_root": str(directory / "inputs"),
            "run_root": str(directory), "cases": [case]}


def model_connections(draft, catalog, connection_revisions):
    allowed = {(c["provider"], m["id"]) for c in catalog if c["connected"] for m in c["models"]}
    routes = [r for r in [draft["coordinator"], *draft["workers"]] if r]
    selected_custom = {r["provider"] for r in routes if is_custom_provider(r["provider"])}
    indexed = {entry["provider"]: entry for entry in catalog}
    if (not isinstance(connection_revisions, dict) or set(connection_revisions) != selected_custom
            or any(p not in indexed or indexed[p]["revision"] != connection_revisions[p] for p in selected_custom)):
        raise InputError("所选模型连接已更改，请刷新并确认后重试。",
                         "A selected model connection changed. Refresh and review it before running.", 409)
    for route in routes:
        if (route["provider"], route["model"]) not in allowed:
            raise InputError("该模型当前不可用，请刷新连接。", "This model is unavailable. Refresh the connection.")
    return indexed


def workbench_executable():
    from src.dsh_runtime.product import installed_executable
    try:
        return installed_executable(None)
    except (ValueError, OSError) as exc:
        raise InputError("找不到建模所需的 DeepSeek Harness。请安装或修复其 Windows 桌面版，再次点击运行。",
                         "DeepSeek Harness could not be found. Install or repair its Windows desktop app, then run again.",
                         503) from exc


def new_run_directory(root):
    identity = time.strftime("run-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    directory = run_dir(root, identity)
    directory.parent.mkdir(exist_ok=True)
    return directory


def launch_prepared(directory, locale, started):
    try:
        with (directory / "run.log").open("ab") as log:
            subprocess.Popen([sys.executable, "-B", "-m", "src.workbench.runs", str(directory)],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT)},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        status = {**started, "status": "failed", "message": "启动失败。" if locale == "zh-CN" else "Could not start the run."}
        save_json(directory / "status.json", status)
        return status
    return started


def begin(root, revision, catalog, connection_revisions):
    with locked(root):
        draft = read_project(root)
        revision_check(draft, revision)
        indexed = model_connections(draft, catalog, connection_revisions)
        previous = latest(root, builds_only=True)
        if previous and previous["status"] == "running" and previous.get("revision") == revision:
            return previous
        executable = workbench_executable()
        directory = new_run_directory(root)
        with tempfile.TemporaryDirectory(dir=directory.parent, prefix=".preparing-") as staging:
            prepared = Path(staging) / "run"
            prepared.mkdir()
            materials, blocks = freeze(root, prepared, draft)
            save_json(prepared / "case.json", configuration(directory, draft, materials, blocks,
                      executable=executable, connections=indexed))
            started = {"id": directory.name, "status": "running", "revision": revision, "message": ""}
            save_json(prepared / "status.json", started)
            prepared.rename(directory)
        return launch_prepared(directory, draft["locale"], started)


def status_at(directory):
    data = read_json(directory / "status.json")
    if data["status"] == "running" and time.time() - (directory / "status.json").stat().st_mtime > 30:
        # The supervisor owns this lock throughout the run, including while the UI is closed.
        try:
            with portalocker.Lock(directory / ".running", timeout=0):
                data = read_json(directory / "status.json")
                if data["status"] != "running":
                    return data
                data = {**data, "status": "failed",
                        "message": "运行已中断。" if read_json(directory / "input.json")["locale"] == "zh-CN" else "The run was interrupted."}
                save_json(directory / "status.json", data)
        except portalocker.exceptions.LockException:
            pass
    return data


def latest(root, *, builds_only=False):
    runs = inside(root, "runs")
    if not runs.is_dir():
        return None
    paths = [d for d in runs.iterdir() if RUN_ID.fullmatch(d.name) and (d / "status.json").is_file()]
    for path in sorted(paths, key=lambda d: (d / "input.json").stat().st_mtime_ns, reverse=True):
        if not builds_only or not read_json(path / "input.json").get("parentRunId"):
            return status_at(path)
    return None


def execute(directory):
    from src.agent.runtime_configuration import load_configuration, selected_case
    from src.dsh_runtime.product import launch
    identity = directory.name
    inputs = read_json(directory / "input.json")
    locale = inputs["locale"]
    with portalocker.Lock(directory / ".running", timeout=0):
        try:
            case = selected_case(load_configuration(directory / "case.json"), identity)
            if launch(case, delivery_directory=directory) != 0:
                raise ValueError("The model run stopped before delivery.")
            if not inside(directory, VIEWER_FILENAME).is_file():
                raise ValueError("No delivered viewer found")
            viewer = f"/results/{workspace_tag(directory.parent.parent)}/{identity}/{VIEWER_FILENAME}"
            result = {"id": identity, "status": "completed", "message": "", "viewer": viewer, "download": viewer + "?download=1"}
        except Exception as exc:
            traceback.print_exc()
            message = "运行未完成，请查看工作区本次运行的 run.log。" if locale == "zh-CN" else "Run did not complete. See run.log in the run folder."
            if locale == "en" and isinstance(exc, ValueError):
                message = f"{exc} See run.log for details."
            if "parentRunId" in inputs:
                from src.agent.model_revision import UNCHANGED_REVISION
                if inside(directory, "production/private/dsh/work/bim/" + UNCHANGED_REVISION).is_file():
                    message = ("最近一次交付对比未发现相对父版本的可见变化。本次没有交付新版本。"
                               if locale == "zh-CN" else "The last delivery comparison found no visible change "
                               "from the parent. This run did not deliver a new version.")
            result = {"id": identity, "status": "failed", "message": message}
        if "parentRunId" in inputs:
            result["parentRunId"] = inputs["parentRunId"]
        save_json(directory / "status.json", result)
    return int(result["status"] != "completed")


if __name__ == "__main__":
    raise SystemExit(execute(Path(sys.argv[1]).resolve()))
