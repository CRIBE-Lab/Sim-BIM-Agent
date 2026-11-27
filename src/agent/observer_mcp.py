"""Expose the native building observers to an external BIM coordinator over stdio."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from scripts.tool_scripts.bim_agent_feedback import mcp_json_result
from src.agent.runtime_entry import ROOT, runtime_model_profile
from src.agent.runtime_mesh_workers import MeshWorkerTools, observer_tool_specs
from src.agent_runtime.adapter import ScriptedAdapter
from src.agent_runtime.connections import validate_connection_model
from src.dsh_runtime.model_routes import model_connection
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.output_limits import validate_output_limit
from src.agent_runtime.providers import CHATGPT_SUBSCRIPTION, provider_parameters, is_custom_provider
from src.agent_runtime.store import EventStore
from src.harness_contracts import RunLifecyclePayload


def worker_profile(args, provider, model):
    custom = is_custom_provider(provider)
    revision = args.connection_revisions.get(provider)
    output = args.output_tokens
    if custom:
        from src.workbench.connections import custom_connection
        if not revision:
            raise ValueError("custom worker requires a frozen connection")
        spec = custom_connection(provider, directory=args.custom_directory, revision=revision)
        output = spec["output_tokens"]
    else:
        runtime_model_profile(provider, model)
        validate_output_limit(model if provider != "scripted" else "scripted-model", output)
    parameters = provider_parameters(provider, model=model, reasoning_effort=args.reasoning_effort,
                                     output_tokens=output)
    if custom:
        limit_field = spec.get("output_token_parameter", "max_tokens")
        if limit_field not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("invalid custom worker output limit field")
        parameters = {limit_field: output}
    if provider == "scripted":
        if args.script is None:
            raise ValueError("scripted workers require --script with per-task replies")
        fixture = args.script.read_bytes()
        replies = json.loads(fixture)
        route = {"route_id": "offline-scripted", "model": model,
                 "fixture_sha256": hashlib.sha256(fixture).hexdigest()}

        def adapter(task_id, child):
            offset = sum(event.payload.event_type == "adapter_request" for event in child.events)
            return ScriptedAdapter(replies[task_id][offset:])
    else:
        if args.script:
            raise ValueError("--script requires the scripted provider")
        auth_directory = args.chatgpt_auth_dir or os.environ.get("SIM_BIM_CHATGPT_AUTH_DIR")
        connection = model_connection(provider, model, auth_dir=auth_directory,
                                      credentials_file=args.credentials_file, protected_key=args.protected_key,
                                      custom_directory=args.custom_directory, revision=revision)
        route = connection.descriptor.model_route(model)
        if custom:
            route.update(connection_revision=revision, context_window=spec["context_window"])
        catalog_lock = asyncio.Lock()
        verified = False

        async def adapter(task_id, child):
            nonlocal verified
            async with catalog_lock:
                if not verified:
                    await validate_connection_model(connection, model)
                    verified = True
            return connection.create_adapter()

    route["host"] = "dsh_observer_pool"
    if args.background:
        route["background"] = True
    return {"adapter_factory": adapter, "parameters": parameters, "route": route}


@contextmanager
def open_observers(args):
    run = args.run.resolve()
    raw_manifest = (run / "inputs.json").read_bytes()
    manifest = json.loads(raw_manifest)
    limits = RunLimits(model_calls=args.model_calls, tool_calls=args.tool_calls, seconds=args.seconds,
                       time_budget_mode="wall", tokens=args.tokens)
    choices = args.routes or [{"provider": args.provider, "model": args.model}]
    profiles = {}
    for row in choices:
        profile = worker_profile(args, row["provider"], row["model"])
        key = f"{profile['route']['route_id']}/{row['model']}"
        if key in profiles:
            raise ValueError("duplicate worker model choice")
        profiles[key] = profile
    first = next(iter(profiles.values()))
    store_path = args.store.resolve()
    with EventStore(store_path, run_id=store_path.name, task_id="observer_pool",
                    budget_limit=limits.ledger_limit()) as store:
        tools = MeshWorkerTools(SimpleNamespace(run_directory=run), store=store, root=ROOT,
            **first, model=first["route"]["model"], limits=limits, start_epoch=manifest["started_epoch"],
            max_concurrent=args.max_concurrent, multimodal=True, worker_profiles=profiles if args.routes else None)
        config = {"run_directory": str(run), "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
                  "configuration": tools._configuration()}
        if store.events:
            origin = store.events[0]
            expected = next((ref.blob for ref in origin.source_refs if ref.source_id == "observer-pool"), None)
            if expected is None or json.loads(store.get_bytes(expected)) != config:
                raise ValueError("worker pool inputs, routing or budgets changed on resume")
        else:
            store.append(RunLifecyclePayload(action="start", reason="external coordinator observer pool"),
                         source_refs=(store.source("observer-pool", config),))
        yield tools


async def serve(args):
    if os.name == "nt":
        # Import geometry dependencies before MCP's blocking stdin reader takes
        # the Windows CRT lock also needed by their Fortran initialization.
        import scipy.signal
    with open_observers(args) as workers:
        from src.agent.background_observers import BackgroundObservers
        background = BackgroundObservers(workers) if args.background else None
        if background:
            background.publish()
        server = Server("sim-bim-workers")
        specs = {tool["name"]: types.Tool(**tool) for tool in observer_tool_specs(
            dsh=True, background=args.background, routes=workers.worker_routes)}

        @server.list_tools()
        async def list_tools():
            return list(specs.values())

        @server.call_tool()
        async def call_tool(name, arguments):
            if name not in specs:
                raise ValueError("unknown observer tool")
            raw = await (background or workers).call_tool(name, arguments)
            display = workers.project_tool_result(name, raw)
            # JSON + image blocks remain ordinary MCP results for both transports.
            if background and name == "delegate_workers" and arguments.get("wait", True):
                background.publish()
            elif not background:
                workers.store.write_json("worker_state.json", workers.state())
            return mcp_json_result(types.CallToolResult.model_validate(display))

        try:
            async with stdio_server() as (reader, writer):
                await server.run(reader, writer, server.create_initialization_options())
        finally:
            if background:
                await background.drain()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", required=True, type=Path, help="admitted parent BIM workspace")
    p.add_argument("--store", required=True, type=Path, help="persistent worker-pool journal directory")
    p.add_argument("--provider", default=CHATGPT_SUBSCRIPTION)
    p.add_argument("--custom-directory", type=Path)
    p.add_argument("--connection-revisions", type=json.loads, default={})
    p.add_argument("--model", required=True)
    p.add_argument("--chatgpt-auth-dir", type=Path)
    p.add_argument("--routes", type=json.loads, help="ordered model choices frozen by the launcher")
    p.add_argument("--credentials-file", type=Path, help="explicit DeepSeek CLI credentials file")
    p.add_argument("--protected-key", type=Path, help="protected local DeepSeek credential selected by the workbench")
    p.add_argument("--output-tokens", type=int, default=32000)
    p.add_argument("--reasoning-effort")
    p.add_argument("--max-concurrent", type=int, required=True)
    p.add_argument("--background", action="store_true", help="allow the coordinator to start bounded tasks without waiting")
    p.add_argument("--model-calls", type=int, required=True)
    p.add_argument("--tool-calls", type=int, required=True)
    p.add_argument("--seconds", type=float)
    p.add_argument("--tokens", type=int)
    p.add_argument("--script", type=Path, help="offline per-task response fixture")
    return p


def prepare_worker_network(environment):
    # DSH applies this sentinel to its own host. Only the worker MCP process
    # uses its explicitly configured model connection, with normal TLS checks.
    for key, value in tuple(environment.items()):
        if key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"} and value == "http://127.0.0.1:1":
            del environment[key]
        if key.upper() == "NO_PROXY":
            # DSH includes both IPv6 loopback spellings. HTTPX wants a bare
            # address here and adds brackets itself when building its URL mounts.
            hosts = [host.strip() for host in value.split(",")]
            if "[::1]" in hosts:
                environment[key] = ",".join(dict.fromkeys("::1" if host == "[::1]" else host for host in hosts))


if __name__ == "__main__":
    prepare_worker_network(os.environ)
    asyncio.run(serve(parser().parse_args()))
