"""Workspace-owned material copies and an optimistic, atomic project draft."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import tempfile
import uuid

import portalocker

from scripts.tool_scripts.bim_agent_inputs import read_building_input
from src.agent.bim_inputs import validate_png
from src.agent.geometry.thickness_options import normalize_candidates
from src.agent_runtime.providers import is_custom_provider
from src.agent_runtime.run_paths import inside_git_checkout

ROOT = Path(__file__).resolve().parents[2]
LOCALES = ("zh-CN", "en")
LEGACY_DEFAULT_BRIEFS = {
    "根据所附资料生成整栋建筑的轻量 BIM 和可交互 HTML。保留有依据的楼层、空间、边界、门窗和连接关系，并注明推断及待确认之处。",
    "Build a lightweight BIM and interactive HTML viewer for the whole building using the attached materials. Preserve supported floors, spaces, boundaries, openings and connections. Identify assumptions and unresolved questions.",
    "根据提供的建筑资料生成可查看的完整轻量 BIM。保留可观察的楼层、空间、边界、开口和连接，注明推断及尚未解决的问题。",
    "Generate a complete, viewable lightweight BIM from the supplied building materials. Preserve the observable floors, spaces, boundaries, openings and connections. Identify assumptions and unresolved questions.",
}
KINDS = {".png": "image", ".glb": "mesh", ".json": "declaration"}


class InputError(ValueError):
    def __init__(self, zh: str, en: str, status=400):
        super().__init__(en)
        self.messages = {"en": en, "zh-CN": zh}
        self.status = status


def app_data():
    override = os.environ.get("SIM_BIM_WORKBENCH_HOME")
    if override:
        return Path(override).resolve()
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local/share") / "Sim-BIM-Agent/workbench"


def read_json(path: Path):
    return json.loads(path.read_bytes())


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as stream:
        pending = Path(stream.name)
        try:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            pending.unlink(missing_ok=True)
            raise
    try:
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def inside(root: Path, relative: str):
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root.resolve()):
        raise InputError("文件路径超出工作区。", "The path is outside the workspace.")
    return path


def workspace_path(value):
    if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
        raise InputError("请选择工作区文件夹。", "Choose a workspace folder.")
    root = Path(value).resolve()
    if root == Path(root.anchor) or root.is_relative_to(ROOT) or (root.exists() and not root.is_dir()):
        raise InputError("请选择程序目录之外的独立文件夹。", "Choose a dedicated folder outside the application.")
    if inside_git_checkout(root):
        raise InputError("请选择 Git 仓库之外的工作区文件夹。", "Choose a workspace folder outside Git repositories.")
    return root


def new_project(locale):
    return {"schema": "sim-bim-workbench.v2", "revision": 0, "locale": locale, "blocks": [],
            "taskBrief": "", "coordinator": None, "workers": [], "thicknessCandidatesMm": {}}


def legacy_kickoff(value):
    first, _, rest = value.partition("\n")
    if first.removesuffix("\r") in LEGACY_DEFAULT_BRIEFS:
        return rest.lstrip("\r\n")
    return value


def read_project(root):
    data = read_json(inside(root, "project.json"))
    if not isinstance(data, dict) or data.get("schema") not in ("sim-bim-workbench.v1", "sim-bim-workbench.v2"):
        raise InputError("该目录中的 project.json 属于其他项目。", "This folder already contains a different project.json.")
    invalid = InputError("工作区的 project.json 已损坏。", "The workspace project.json is damaged.")
    try:
        data["thicknessCandidatesMm"] = normalize_candidates(data.get("thicknessCandidatesMm"))
    except ValueError as exc:
        raise invalid from exc
    if (data.get("locale") not in LOCALES or type(data.get("revision")) is not int
            or not isinstance(data.get("blocks"), list) or not isinstance(data.get("taskBrief"), str)
            or not isinstance(data.get("workers"), list) or "coordinator" not in data):
        raise invalid
    if data["schema"] == "sim-bim-workbench.v1":
        data["taskBrief"] = legacy_kickoff(data["taskBrief"])
        data["schema"] = "sim-bim-workbench.v2"
    for block in data["blocks"]:
        if (not isinstance(block, dict) or not isinstance(block.get("id"), str) or not isinstance(block.get("assets"), list)
                or not isinstance(block.get("caption"), str) or Path(block["id"]).name != block["id"]):
            raise invalid
        for item in block["assets"]:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("id", "name", "kind", "path")):
                raise invalid
            parts = Path(item["path"]).parts
            if (len(parts) != 4 or parts[:2] != ("materials", block["id"]) or not parts[2].isdigit()
                    or parts[3] != checked_name(item["name"]) or KINDS[Path(parts[3]).suffix.lower()] != item["kind"]):
                raise invalid
    return data


@contextlib.contextmanager
def locked(root):
    root.mkdir(exist_ok=True, parents=True)
    with portalocker.Lock(inside(root, ".workbench.lock"), timeout=15):
        yield


def open_project(root, locale):
    with locked(root):
        path = inside(root, "project.json")
        if path.exists():
            return read_project(root)
        project = new_project(locale)
        save_json(path, project)
        return project


def revision_check(project, revision):
    if revision != project["revision"]:
        raise InputError("草稿已更新，请刷新后重试。", "The draft changed. Refresh and try again.", 409)


def persist(root, project):
    project["revision"] += 1
    save_json(inside(root, "project.json"), project)
    return project


def client_project(data):
    return {**data, "blocks": [{**b, "assets": [
        {k: a[k] for k in ("id", "name", "kind")} | {"url": "/api/assets/" + a["id"]}
        for a in b["assets"]]} for b in data["blocks"]]}


def str_field(value):
    if not isinstance(value, str):
        raise InputError("文字格式无效。", "Expected text.")
    return value


def ordered(available, keys):
    if not isinstance(keys, list) or any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys) or not set(keys) <= available.keys():
        raise InputError("素材顺序无效，请刷新。", "Invalid material order. Refresh the page.")
    return [available[k] for k in keys]


def selection(value):
    if (not isinstance(value, dict)
            or (value.get("provider") not in ("chatgpt-subscription", "deepseek") and not is_custom_provider(value.get("provider")))
            or not isinstance(value.get("model"), str) or not value["model"].strip()):
        raise InputError("请选择有效模型。", "Select a valid model.")
    return {"provider": value["provider"], "model": value["model"]}


def update_project(root, payload):
    with locked(root):
        data = read_project(root)
        revision_check(data, payload.get("revision"))
        blocks = {b["id"]: b for b in data["blocks"]}
        rows = payload.get("blocks")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise InputError("素材栏格式无效。", "Invalid material library.")
        selected = ordered(blocks, [row.get("id") for row in rows])
        for block, row in zip(selected, rows, strict=True):
            block["caption"] = str_field(row.get("caption", ""))
            block["assets"] = ordered({a["id"]: a for a in block["assets"]}, row.get("assetIds"))
            if not block["assets"] and not block["caption"].strip():
                raise InputError("请为素材组添加文件或填写说明。", "Add a file or a caption to the block.")
        data["blocks"] = selected
        data["taskBrief"] = str_field(payload.get("taskBrief"))
        if payload.get("schema") != "sim-bim-workbench.v2":
            data["taskBrief"] = legacy_kickoff(data["taskBrief"])
        if "thicknessCandidatesMm" in payload:
            try:
                data["thicknessCandidatesMm"] = normalize_candidates(payload["thicknessCandidatesMm"])
            except ValueError as exc:
                raise InputError("请按组输入大于零的毫米数值。", str(exc)) from exc
        data["coordinator"] = selection(payload["coordinator"]) if payload.get("coordinator") else None
        workers = payload.get("workers", [])
        if not isinstance(workers, list):
            raise InputError("worker 配置无效。", "Invalid worker selection.")
        data["workers"] = [selection(w) for w in workers]
        return persist(root, data)


def checked_name(name):
    if (not name or name in (".", "..") or PureWindowsPath(name).name != name or Path(name).name != name
            or PureWindowsPath(name).is_reserved() or name[-1] in ". " or any(ord(c) < 32 or c in '<>:"|?*' for c in name)):
        raise InputError("文件名无效。", "Invalid file name.")
    if Path(name).suffix.lower() not in KINDS:
        raise InputError("支持 PNG、GLB 和建筑说明 JSON。", "Supported files are PNG, GLB and building JSON.")
    return name


def validate_asset(path, kind):
    try:
        if kind == "image":
            validate_png(path)
        elif kind == "declaration":
            read_building_input(path)
        else:
            with path.open("rb") as stream:
                header = stream.read(12)
            if (header[:4] != b"glTF" or int.from_bytes(header[4:8], "little") != 2
                    or int.from_bytes(header[8:12], "little") != path.stat().st_size):
                raise ValueError("invalid GLB")
    except (ValueError, OSError) as exc:
        raise InputError(f"请检查文件 {path.name} 的内容和格式。", f"Check the contents and format of {path.name}.") from exc


def add_block(root, revision, caption, uploads, block_id=None, order=None):
    with locked(root):
        data = read_project(root)
        revision_check(data, revision)
        old = next((b for b in data["blocks"] if b["id"] == block_id), None)
        if block_id and not old:
            raise InputError("素材组已不存在。", "The block no longer exists.", 409)
        block_id = block_id or "block-" + uuid.uuid4().hex[:12]
        folder = inside(root, "materials/" + block_id)
        folder.parent.mkdir(parents=True, exist_ok=True)
        slots = folder.iterdir() if folder.is_dir() else ()
        next_slot = 1 + max((int(p.name) for p in slots if p.name.isdecimal()), default=0)
        available = {a["id"]: a for a in old["assets"]} if old else {}
        new_slots = []
        with tempfile.TemporaryDirectory(dir=folder.parent, prefix=".import-") as pending:
            for i, (name, stream) in enumerate(uploads):
                name = checked_name(name)
                kind = KINDS[Path(name).suffix.lower()]
                slot = f"{next_slot + i:02}"
                dest = Path(pending) / slot / name
                dest.parent.mkdir()
                with dest.open("xb") as target:
                    shutil.copyfileobj(stream, target, 1024 * 1024)
                validate_asset(dest, kind)
                record = {"id": uuid.uuid4().hex, "name": name, "kind": kind,
                          "path": (folder / slot / name).relative_to(root).as_posix()}
                available[f"@{i}"] = record
                new_slots.append(slot)
            keys = order if order is not None else list(available)
            assets = ordered(available, keys)
            if (not assets and not str_field(caption).strip()) or any(f"@{i}" not in keys for i in range(len(uploads))):
                raise InputError("请为素材组添加文件或填写说明。", "Add a file or a caption to the block.")
            block = {"id": block_id, "caption": str_field(caption), "assets": assets}
            if old is None:
                data["blocks"].append(block)
            else:
                data["blocks"][data["blocks"].index(old)] = block
            promoted = []
            try:
                folder.mkdir(exist_ok=old is not None)
                for slot in new_slots:
                    target = folder / slot
                    (Path(pending) / slot).rename(target)
                    promoted.append(target)
                return persist(root, data)
            except BaseException:
                for target in promoted:
                    shutil.rmtree(target)
                if old is None and folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
                raise
