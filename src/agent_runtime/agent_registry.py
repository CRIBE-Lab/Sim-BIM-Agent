"""Read and verify the current immutable Sim-BIM runtime and domain releases.

Release registration belongs to scripts.dev.agent_versions. Running a product
never writes its identity or accepts an older release as the current program.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any

REGISTRY_RELATIVE_PATH = Path("src/agent_runtime/agent_versions.json")
RELEASE_MANIFEST_PATH = Path("release-manifest.json")
_SCHEMA = "agent-version-registry.v2"
_SKIP_PARTS = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache",
               ".venv", "node_modules", "tests", "testdata", "docs", "logs", "archive"}
_SKIP_SUFFIXES = {".pyc", ".pyo", ".tmp", ".log", ".bak"}
_SKIP_NAMES = {"readme.md", "license.md", "license", "affected_tests.py", "affected_tests_rules.yaml"}


class AgentVersionMismatch(ValueError):
    """The checkout differs from the selected immutable release."""


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def product_version_id(line, record, scope):
    """Content identity for a generated product snapshot, distinct from dev IDs."""
    encoded = (json.dumps({"scope": scope, "record": record}, ensure_ascii=False,
                          sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    return f"{line}-product-v1-" + hashlib.sha256(encoded).hexdigest()[:24]


def _registry_path(root, registry_path):
    override = os.environ.get("BIM_AGENT_REGISTRY_PATH") if registry_path is None else None
    if override is not None and not Path(override).is_absolute():
        raise ValueError("BIM_AGENT_REGISTRY_PATH must be absolute")
    return (Path(registry_path) if registry_path is not None else
            Path(override) if override else Path(root) / REGISTRY_RELATIVE_PATH).resolve()


def load_agent_registry(root: Path, registry_path: Path | None = None) -> dict[str, Any]:
    path = _registry_path(root, registry_path)
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise AgentVersionMismatch(f"version registry does not exist: {path}") from error
    if registry.get("schema_version") != _SCHEMA:
        raise AgentVersionMismatch("unsupported version registry schema")
    domains = registry.get("domain_versions", {})
    if registry.get("current_domain_version") not in domains:
        raise AgentVersionMismatch("registry has no valid current domain version")
    runtime_id = registry.get("current_runtime_version")
    if runtime_id is not None and runtime_id not in registry.get("runtime_versions", {}):
        raise AgentVersionMismatch("registry has no valid current runtime version")
    projection = registry.get("product_projection")
    if projection is not None:
        if (not isinstance(projection, dict) or projection.get("schema_version") != "sim-bim-product-projection.v1"
                or not re.fullmatch(r"[a-f0-9]{40}", projection.get("source_commit", ""))):
            raise AgentVersionMismatch("invalid product projection provenance")
        if registry.get("aliases") != {}:
            raise AgentVersionMismatch("product projection cannot alias historical versions")
        for line in ("runtime", "domain"):
            identity = registry[f"current_{line}_version"]
            if set(registry[f"{line}_versions"]) != {identity}:
                raise AgentVersionMismatch("product projection must contain only its current versions")
            record = registry[f"{line}_versions"][identity]
            if (record.get("source_commit") != projection["source_commit"]
                    or record.get("product_projection") != projection
                    or identity != product_version_id(line, record, registry["scope"])):
                raise AgentVersionMismatch(f"product {line} identity differs from its immutable projection")
    return registry


def _git(root, *args):
    return subprocess.check_output(["git", "--no-optional-locks", *args], cwd=root,
                                   text=True, encoding="utf-8").strip()


def source_commit(root):
    manifest = load_release_manifest(root)
    if manifest is not None:
        return manifest["source_commit"]
    try:
        return _git(root, "rev-parse", "HEAD")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def load_release_manifest(root: Path) -> dict[str, Any] | None:
    """Read release identity without rehashing payload; release_records is the gate.

    An absent manifest retains checkout behavior. A present but invalid manifest
    never falls back to Git, including in a checkout used to inspect a release.
    """
    path = Path(root) / RELEASE_MANIFEST_PATH
    if path.is_symlink():
        raise AgentVersionMismatch("release manifest must not be a symlink")
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise AgentVersionMismatch("release manifest cannot be read") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "sim-bim-release.v1":
        raise AgentVersionMismatch("unsupported release manifest schema")
    if not isinstance(manifest.get("source_commit"), str) or not re.fullmatch(
            r"[0-9a-f]{40}", manifest["source_commit"]):
        raise AgentVersionMismatch("release manifest needs a full source_commit")
    if any(not isinstance(manifest.get(f"{line}_version"), str)
           or not manifest[f"{line}_version"] for line in ("runtime", "domain")):
        raise AgentVersionMismatch("release manifest needs runtime/domain versions")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise AgentVersionMismatch("release manifest needs payload files")
    normalized = set()
    for relative, digest in files.items():
        candidate = PurePosixPath(relative)
        if (not relative or candidate.is_absolute() or ".." in candidate.parts
                or candidate.as_posix() != relative or "\\" in relative or ":" in relative
                or relative == RELEASE_MANIFEST_PATH.as_posix()
                or relative.casefold() in normalized):
            raise AgentVersionMismatch(f"invalid release payload path: {relative}")
        normalized.add(relative.casefold())
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AgentVersionMismatch(f"invalid release payload digest: {relative}")
    if REGISTRY_RELATIVE_PATH.as_posix() not in files:
        raise AgentVersionMismatch("release manifest must cover the version registry")
    return manifest


def _verify_release_payload(root, manifest, registry, *, registry_path):
    if _registry_path(root, registry_path) != root / REGISTRY_RELATIVE_PATH:
        raise AgentVersionMismatch("a release must use its bundled version registry")
    projection = registry.get("product_projection")
    if projection is not None:
        declared = manifest.get("projection")
        if (manifest["source_commit"] != projection["source_commit"] or not isinstance(declared, dict)
                or any(declared.get(key) != value for key, value in projection.items())):
            raise AgentVersionMismatch("release source identity differs from its product projection")
    covered = {}
    for line in ("runtime", "domain"):
        version = registry[f"current_{line}_version"]
        if manifest[f"{line}_version"] != version:
            raise AgentVersionMismatch(f"release manifest {line} version differs from registry")
        for relative, metadata in registry[f"{line}_versions"][version]["files"].items():
            if manifest["files"].get(relative) != metadata["sha256"]:
                raise AgentVersionMismatch(f"release manifest differs from registered file: {relative}")
            covered[relative] = metadata["sha256"]
    for relative, expected in manifest["files"].items():
        path = root / relative
        if not path.resolve().is_relative_to(root) or any(
                part.is_symlink() or part.is_junction()
                for part in (path, *path.parents) if part != root and part.is_relative_to(root)):
            raise AgentVersionMismatch(f"release payload contains a linked path: {relative}")
        # _verify_files already hashes registered members and checks membership.
        # Only the extra release resources need another payload hash here.
        if relative not in covered and (not path.is_file() or _sha256(path) != expected):
            raise AgentVersionMismatch(f"release payload file mismatch: {relative}")


def _kind(relative, line):
    if line == "runtime":
        return "runtime"
    if "guidance" in relative or Path(relative).suffix in {".md", ".txt"}:
        return "guidance"
    if Path(relative).name in {"bim_inputs.py", "bim_agent_inputs.py", "run_bim_agent.py"}:
        return "task_description"
    return "tool"


def discover_files(root, line, *, scope=None, registry_path=None):
    """Hash bytes AND membership; refuse links instead of following them."""
    root = Path(root).resolve()
    if scope is None:
        raise ValueError("An explicit runtime/domain file scope is required")
    excluded = {root / REGISTRY_RELATIVE_PATH, _registry_path(root, registry_path)}
    files = {}
    runtime_paths = [root / relative for relative in scope["runtime"]]
    for relative in scope[line]:
        base = root / relative
        if not base.resolve().is_relative_to(root):
            raise ValueError(f"version scope escapes checkout: {relative}")
        if not base.exists():
            continue
        if base.is_symlink() or base.is_junction():
            raise ValueError(f"version scope contains a symlink: {relative}")
        candidates = [base] if base.is_file() else _walk_files(base)
        for path in candidates:
            if (path in excluded or path.suffix in _SKIP_SUFFIXES or path.name.casefold() in _SKIP_NAMES
                    or any(p in _SKIP_PARTS for p in path.relative_to(root).parts)):
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError(f"version scope contains a symlink: {path}")
            if line == "domain" and any(path == p or path.is_relative_to(p) for p in runtime_paths):
                continue
            name = path.relative_to(root).as_posix()
            files[name] = {"kind": _kind(name, line), "sha256": _sha256(path)}
    return dict(sorted(files.items()))


def _walk_files(base):
    for directory, names, files in os.walk(base, followlinks=False):
        names[:] = [name for name in names if name not in _SKIP_PARTS]
        if any((Path(directory) / name).is_symlink() or (Path(directory) / name).is_junction() for name in names):
            raise ValueError(f"version scope contains a linked directory: {directory}")
        yield from (Path(directory) / name for name in files)


def _verify_files(root, record, *, line, version, registry, registry_path):
    expected = record.get("files")
    if not isinstance(expected, dict) or not expected:
        raise AgentVersionMismatch(f"version {version} has no registered files")
    if record.get("coverage") != "automatic_directories":
        raise AgentVersionMismatch("Current releases require complete file membership coverage")
    actual = discover_files(root, line, scope=registry["scope"], registry_path=registry_path)
    changed = sorted(name for name in expected.keys() | actual.keys()
                     if expected.get(name) != actual.get(name))
    if changed:
        raise AgentVersionMismatch(f"{line} version {version} file mismatch: {json.dumps(changed)}")


def release_records(root, *, registry_path=None, verify=True):
    root = Path(root).resolve()
    registry = load_agent_registry(root, registry_path)
    if registry["schema_version"] != _SCHEMA or not registry.get("current_runtime_version"):
        raise AgentVersionMismatch("runtime/domain registration is required before a new run")
    result = {}
    for line in ("runtime", "domain"):
        version = registry[f"current_{line}_version"]
        record = registry[f"{line}_versions"][version]
        if verify:
            _verify_files(root, record, line=line, version=version, registry=registry, registry_path=registry_path)
        result[line] = {"version_id": version, **record}
    if verify:
        manifest = load_release_manifest(root)
        if manifest is not None:
            _verify_release_payload(root, manifest, registry, registry_path=registry_path)
    return result


def agent_version_record(root: Path, version_id: str | None = None, *,
                         registry_path: Path | None = None, verify: bool = True) -> dict[str, Any]:
    """Return the current domain identity; historical releases cannot run here."""
    records = release_records(root, registry_path=registry_path, verify=verify)
    current = records["domain"]
    if version_id is not None and version_id != current["version_id"]:
        raise AgentVersionMismatch(f"Agent version is not current: {version_id}")
    return current
