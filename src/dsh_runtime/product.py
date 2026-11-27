"""Launch a unified product case with the installed DeepSeek Harness."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys

from src.agent.runtime_configuration import (
    ROOT, load_configuration, orchestrator_worker, resolve_case_input, resolve_supplemental_inputs, selected_case,
)
from src.agent.bim_inputs import admitted_png_paths
from src.agent.viewer_publication import publish_attested_viewer
from src.agent_runtime.estimation import get_model_profile
from src.agent_runtime.providers import is_custom_provider
from src.agent_runtime.openai_subscription import SubscriptionCredentials
from src.agent_runtime.run_paths import resolve_run_output
from . import launcher
from .model_routes import ApiCredential, model_connection


def _windows_installation():
    import winreg
    uninstall = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, uninstall) as keys:
                for index in range(winreg.QueryInfoKey(keys)[0]):
                    try:
                        with winreg.OpenKey(keys, winreg.EnumKey(keys, index)) as entry:
                            if winreg.QueryValueEx(entry, "DisplayName")[0] == "DeepSeek Harness":
                                path = Path(winreg.QueryValueEx(entry, "InstallLocation")[0]) / "DeepSeek Harness.exe"
                                if path.is_file() and (path.parent / "resources/app.asar").is_file():
                                    return path
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def installed_executable(configured: str | None) -> Path:
    explicit = configured or os.environ.get("SIM_BIM_DSH_EXECUTABLE")
    if explicit:
        executable = Path(explicit).expanduser().resolve()
    else:
        cli = shutil.which("dsh.cmd")
        executable = None
        if cli:
            for parent in Path(cli).resolve().parents:
                candidate = parent / "DeepSeek Harness.exe"
                if candidate.is_file() and (parent / "resources/app.asar").is_file():
                    executable = candidate
                    break
        if os.name == "nt" and executable is None:
            executable = _windows_installation()
        if executable is None:
            raise ValueError("set dsh_executable to the installed DeepSeek Harness.exe or add dsh.cmd to PATH")
    if not executable.is_file() or not (executable.parent / "resources/app.asar").is_file():
        raise ValueError(f"DeepSeek Harness desktop installation missing at {executable}")
    return executable


def host_config(case, auth_dir, *, resume_revisions=None):
    limits = case["limits"]
    coordinator_limits = {"wall_seconds": limits["seconds"], "model_calls": limits["model_calls"],
                          "tool_calls": limits["tool_calls"], "candidates": case["max_candidates"]}
    model = case["model"]
    provider = case.get("provider", "chatgpt-subscription")
    choices = case.get("workers") or []
    selected = {p for p in (provider, *(r["provider"] for r in choices)) if is_custom_provider(p)}
    revisions = case.get("connection_revisions", resume_revisions) or {}
    if not isinstance(revisions, dict) or set(revisions) - selected:
        raise ValueError("connection revisions must refer to selected custom model providers")
    specs = {}
    if selected:
        from src.workbench.connections import custom_connection, custom_connections_directory, freeze_custom_connection
        revisions = {p: revisions[p] if p in revisions else freeze_custom_connection(p) for p in sorted(selected)}
        specs = {p: custom_connection(p, revision=r) for p, r in revisions.items()}
    context_window = specs[provider]["context_window"] if provider in specs else get_model_profile(model, strict=True).context_window_tokens
    config = {"runtime": "dsh", "model": model, "reasoning_effort": "high",
              "image_kind": case["image_kind"], "floor_images": case.get("floor_plan_images"),
              "chatgpt_auth_dir": str(auth_dir), "limits": coordinator_limits,
              "dsh_executable": str(installed_executable(case.get("dsh_executable"))),
              "context_window": case.get("context_tokens") or context_window,
              "image_history_batches": case.get("image_history_batches")}
    if specs:
        config.update(connection_revisions=revisions, custom_directory=str(custom_connections_directory()))
    if provider in specs:
        config.update(provider=provider, output_tokens=specs[provider]["output_tokens"],
                      base_url=specs[provider]["base_url"],
                      output_parameter=specs[provider].get("output_token_parameter", "max_tokens"))
    if provider == "deepseek":
        config.update(provider=provider, output_tokens=case["output_tokens"])
    worker = orchestrator_worker(case)
    if worker is not None:
        default_output = case["output_tokens"]
        if provider in specs and choices and "output_tokens" not in worker:
            from src.agent_runtime.output_limits import default_output_tokens
            default_output = max((default_output_tokens(r["model"]) for r in choices
                                  if not is_custom_provider(r["provider"])), default=specs[provider]["output_tokens"])
        pool = {"model": choices[0]["model"] if choices else worker.get("model", model),
                "output_tokens": worker.get("output_tokens", default_output),
                "reasoning_effort": worker.get("reasoning_effort", "high"),
                "max_concurrent": worker.get("max_concurrent", 1)}
        if worker.get("background"):
            pool["background"] = True
        if choices:
            pool["routes"] = choices
        for key in ("model_calls", "tool_calls"):
            pool[key] = max(1, limits[key] // 4)
            coordinator_limits[key] -= pool[key]
        config["worker"] = pool
    if case.get("credentials_file") and (provider == "deepseek" or any(
            route["provider"] == "deepseek" for route in case.get("workers", []))):
        config["credentials_file"] = str((ROOT / Path(case["credentials_file"]).expanduser()).resolve())
    return config


def verify_resume_inputs(manifest, material_paths):
    if manifest.get("thickness_candidates_mm", {}) != material_paths.get("thickness_candidates_mm", {}):
        raise ValueError("configured thickness candidates differ from the admitted DSH run")
    for name in ("blocks", "locale"):
        if manifest.get(name) != material_paths.get(name):
            raise ValueError(f"configured {name} differs from the admitted DSH run")
    images = material_paths["images"]
    hashes = {p.name: launcher.digest(p) for p in admitted_png_paths(images)} if images else {}
    expected = {name: data["sha256"] for name, data in manifest["images"].items()}
    if hashes != expected:
        raise ValueError("configured images differ from the admitted DSH run")
    for field, saved, digest_key in (("mesh", "mesh_input", "sha256"),
                                     ("building_input", "building_input", "raw_sha256")):
        path = material_paths[field]
        actual = launcher.digest(path) if path else None
        if actual != (manifest.get(saved) or {}).get(digest_key):
            raise ValueError(f"configured {field} differs from the admitted DSH run")


def publish_result(output: Path, terminal: dict, directory: Path) -> None:
    if terminal.get("delivered") is not True:
        raise ValueError("The run stopped without a delivered model")
    viewer = publish_attested_viewer(output / "private/dsh/work/bim", terminal.get("proof"), directory)
    print(json.dumps({"status": "delivered", "viewer": str(viewer)}, ensure_ascii=False))


def matches_frozen_scope(case, frozen_scope):
    if frozen_scope == case["scope"]:
        return True
    # Older configurations with no scope prefixed any admitted image notes with this exact text.
    legacy_default = "Deliver a simulation-ready Sim-BIM for the supplied building. Use the detail supported by the evidence."
    return case.get("_scope_was_omitted") is True and frozen_scope == legacy_default + case["scope"]


def _local_north(output, public, case, materials):
    from src.agent.model_revision import finish_north_revision
    from src.agent.runtime_entry import prepare_inputs
    from src.agent.runtime_tools import frozen_bim_client
    if output.exists():
        raise ValueError(f"run directory already exists: {output}")
    run, _, _ = prepare_inputs(output / "private/dsh/work", **materials, scope=case["scope"],
        image_kind=case["image_kind"], floor_plan_images=case.get("floor_plan_images"),
        max_candidates=case["max_candidates"], orchestrator=True)
    async def execute():
        async with frozen_bim_client(run, repository_root=ROOT) as client:
            return await finish_north_revision(run, client)
    terminal = asyncio.run(execute())
    launcher.relay.save(output / "control/terminal.json", terminal)
    publish_result(output, terminal, public)
    return 0


def launch(case, *, resume=False, delivery_directory=None):
    if case.get("mode") != "orchestrator" or case.get("runtime") != "dsh":
        raise ValueError("select an orchestrator case using the dsh runtime")
    run_root = case.get("run_root")
    output = resolve_run_output(Path(case["output"]) if run_root else ROOT / case["output"],
                                repository_root=ROOT, run_root=run_root)
    public = delivery_directory or output
    primary = resolve_case_input(case)
    auxiliary, declaration = resolve_supplemental_inputs(case)
    mesh = primary if case["input_kind"] == "mesh" else None
    materials = {"images": auxiliary if mesh else primary, "mesh": mesh, "building_input": declaration}
    materials.update({key: case[key] for key in ("blocks", "locale", "thickness_candidates_mm") if key in case})
    revision = case.get("revision")
    if revision is not None:
        inputs_root = Path(case["input_root"]).resolve()
        directory = inputs_root / "revision-parent"
        if directory.resolve().parent != inputs_root or directory.is_symlink() or directory.is_junction():
            raise ValueError("Revision parent must be inside the frozen inputs")
        materials.update(revision=revision, revision_directory=directory)
        draft = revision["draft"]
        if draft["northAngle"] is not None and not draft["notes"] and not draft["instructions"].strip():
            if resume:
                terminal = launcher.read(output / "control/terminal.json")
                publish_result(output, terminal, public)
                return 0
            return _local_north(output, public, case, materials)
    credentials = SubscriptionCredentials(Path(case["chatgpt_auth_dir"]) if case.get("chatgpt_auth_dir") else None)
    frozen_config = launcher.read(output / "config.json") if resume else {}
    config = host_config(case, credentials.directory, resume_revisions=frozen_config.get("connection_revisions"))
    if revision is not None:
        config["revision"] = revision
    if resume:
        if frozen_config != config or not matches_frozen_scope(case, (output / "shared/task.txt").read_text(encoding="utf-8")):
            raise ValueError("requested DSH configuration or task differs from the frozen run")
        verify_resume_inputs(launcher.read(output / "private/dsh/work/bim/inputs.json"), materials)
        terminal_path = output / "control/terminal.json"
        terminal = launcher.read(terminal_path) if terminal_path.is_file() else {}
        if terminal.get("delivered") is True:
            # A storage failure after finish can retry just the hash-bound publication.
            publish_result(output, terminal, public)
            return 0
    elif output.exists():
        raise ValueError(f"run directory already exists: {output}")
    provider = case["provider"]
    pool = config.get("worker", {})
    routes = [{"provider": provider, "model": case["model"]}]
    if pool:
        routes.extend(pool.get("routes", [{"provider": provider, "model": pool["model"]}]))
    connections = {}
    for route in routes:
        name = route["provider"]
        if name not in connections:
            connections[name] = model_connection(name, route["model"], auth_dir=credentials.directory,
                                                 credentials_file=config.get("credentials_file"),
                                                 revision=config.get("connection_revisions", {}).get(name))
    subscription = connections.get("chatgpt-subscription")
    if subscription:
        catalog = {m.slug for m in asyncio.run(subscription.subscription.list_models())}
        if any(r["model"] not in catalog for r in routes if r["provider"] == "chatgpt-subscription"):
            raise ValueError("selected model is not available in this ChatGPT subscription catalog")
    connection = connections[provider]
    if not resume:
        output.mkdir(parents=True, exist_ok=False)
        (output / "shared").mkdir()
        (output / "shared/task.txt").write_text(case["scope"], encoding="utf-8")
        (output / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
        launcher.prepare(output, material_paths=materials)
    exit_code = launcher.run(output, resume, credential=connection.subscription or ApiCredential(connection),
                             image_history_batches=case.get("image_history_batches"), print_terminal=False)
    if exit_code == 0:
        publish_result(output, launcher.read(output / "control/terminal.json"), public)
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("configuration", type=Path)
    parser.add_argument("--case", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    case = selected_case(load_configuration(args.configuration), args.case)
    try:
        return launch(case, resume=args.resume)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
