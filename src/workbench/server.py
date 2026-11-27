"""A loopback-only workbench for the installed Sim-BIM application."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
from urllib.parse import urlsplit
import webbrowser

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from src.agent.viewer_publication import VIEWER_FILENAME
from src.agent.geometry.thickness_options import PRESETS_MM

from .connections import Connections
from .revisions import begin_revision
from .runs import OUTPUT_FILE, begin, latest, run_dir, status_at, workspace_tag
from .trace import run_history, trace_page
from .storage import (LOCALES, ROOT, InputError, add_block, app_data, client_project, inside, locked, new_project,
                      open_project, persist, read_json, read_project, revision_check, save_json, update_project, workspace_path)

STATIC = Path(__file__).with_name("static")


def installation_id():
    # Browser annotations belong to this origin across restarts and updates.
    roots = (ROOT, app_data().resolve())
    return hashlib.sha256(b"\0".join(os.fsencode(os.path.normcase(path)) for path in roots)).hexdigest()


def native_folder(initial):
    if os.name == "nt":
        script = """
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.ShowNewFolderButton = $true
$dialog.SelectedPath = $env:SIM_BIM_INITIAL_FOLDER
$window = New-Object System.Windows.Forms.Form
$window.TopMost = $true
try { if ($dialog.ShowDialog($window) -eq 'OK') { [Console]::Write($dialog.SelectedPath) } }
finally { $dialog.Dispose(); $window.Dispose() }
"""
        picker = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command", script],
            capture_output=True, env={**os.environ, "SIM_BIM_INITIAL_FOLDER": initial or ""},
            creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        return picker.stdout.decode("utf-8-sig").strip()
    from tkinter import Tk, filedialog
    window = Tk()
    window.withdraw()
    try:
        return filedialog.askdirectory(parent=window, initialdir=initial or str(Path.home()))
    finally:
        window.destroy()


class Workbench:
    def __init__(self):
        # Keep the installed app's last workspace separate from a development checkout.
        name = "desktop-preferences.json" if (ROOT / "release-manifest.json").is_file() else "preferences.json"
        self.preferences = app_data() / name
        try:
            saved = read_json(self.preferences)
        except (ValueError, OSError):
            saved = {}
        self.prefs = {"locale": "zh-CN", "workspace": None}
        if isinstance(saved, dict) and saved.get("locale") in LOCALES:
            self.prefs.update(locale=saved["locale"], workspace=saved.get("workspace"))
        self.connections = Connections()
        self.guard = threading.RLock()

    def root(self, submitted=None):
        active = self.prefs.get("workspace")
        if not active:
            raise InputError("请先选择工作区。", "Select a workspace first.")
        if submitted is not None and submitted != active:
            raise InputError("工作区已切换，请刷新。", "The workspace has changed. Refresh the page.", 409)
        return workspace_path(active)

    def snapshot(self):
        with self.guard:
            locale = self.prefs["locale"]
            root, error = None, None
            project = new_project(locale)
            run = None
            try:
                if self.prefs.get("workspace"):
                    root = self.root()
                    project = read_project(root)
                    run = latest(root)
            except (ValueError, OSError, KeyError, TypeError):
                root = None
                project = new_project(locale)
                error = ("上次工作区无法读取，可重新选择文件夹。" if locale == "zh-CN"
                         else "The previous workspace could not be read. Choose a workspace folder.")
            return {"workspace": str(root) if root else None, "locale": locale, "workspaceError": error,
                    "project": client_project(project), "connections": self.connections.view(locale), "run": run,
                    "thicknessPresetsMm": PRESETS_MM}

    def select(self, body):
        path = native_folder(self.prefs.get("workspace")) if body.get("browse") else body.get("path")
        if not path and body.get("browse"):
            return self.snapshot()
        root = workspace_path(path)
        with self.guard:
            project = open_project(root, self.prefs["locale"])
            self.prefs.update(workspace=str(root), locale=project["locale"])
            save_json(self.preferences, self.prefs)
            return self.snapshot()

    def language(self, locale, submitted, revision):
        if locale not in LOCALES:
            raise InputError("语言选项无效。", "Invalid language.")
        with self.guard:
            if submitted != self.snapshot()["workspace"]:
                raise InputError("工作区已切换，请刷新。", "The workspace has changed. Refresh the page.", 409)
            if submitted:
                root = self.root()
                if (root / "project.json").exists():
                    with locked(root):
                        data = read_project(root)
                        revision_check(data, revision)
                        if data["locale"] != locale:
                            data["locale"] = locale
                            persist(root, data)
            self.prefs["locale"] = locale
            save_json(self.preferences, self.prefs)
            return self.snapshot()


def create_app(workbench=None, *, warm=True):
    service = workbench or Workbench()

    @asynccontextmanager
    async def lifespan(_):
        if warm:
            service.connections.warm()
        yield

    async def home(_):
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    async def instance(_):
        return JSONResponse({"product": "Sim-BIM Agent", "installation": installation_id()})

    async def state(_):
        return JSONResponse(await run_in_threadpool(service.snapshot))

    async def api(request: Request):
        action = request.path_params["action"]
        if action == "blocks":
            async with request.form() as form:
                files = [(v.filename, v.file) for v in form.getlist("files") if isinstance(v, UploadFile)]
                root = service.root(form["workspace"])
                order = json.loads(form["assetOrder"]) if "assetOrder" in form else None
                project = await run_in_threadpool(add_block, root, int(form["revision"]), form.get("caption", ""),
                                                  files, form.get("blockId"), order)
                return JSONResponse({"project": client_project(project)})
        body = await request.json()
        if not isinstance(body, dict):
            raise InputError("请求格式无效。", "Invalid request.")
        if action == "workspace":
            response = await run_in_threadpool(service.select, body)
        elif action == "preferences":
            response = await run_in_threadpool(service.language, body.get("locale"), body.get("workspace"), body.get("revision"))
        elif action == "project":
            project = await run_in_threadpool(update_project, service.root(body["workspace"]), body)
            response = {"project": client_project(project)}
        elif action == "connections":
            await run_in_threadpool(service.connections.manage, body)
            response = {"connections": service.connections.view(service.prefs["locale"])}
        elif action == "revisions":
            result = await run_in_threadpool(begin_revision, service.root(), body, service.connections.view)
            response = {"run": result}
        elif action == "runs":
            result = await run_in_threadpool(begin, service.root(body["workspace"]), body.get("revision"),
                                            service.connections.view(service.prefs["locale"]),
                                            body.get("connectionRevisions", {}))
            response = {"run": result}
        else:
            raise InputError("未找到。", "Not found.", 404)
        return JSONResponse(response)

    async def run_status(request):
        path = run_dir(service.root(), request.query_params.get("id"))
        if not (path / "status.json").is_file():
            raise InputError("未找到运行。", "Run not found.", 404)
        return JSONResponse({"run": await run_in_threadpool(status_at, path)})

    async def run_trace(request):
        path = run_dir(service.root(), request.query_params.get("id"))
        return JSONResponse(await run_in_threadpool(trace_page, path, request.query_params.get("cursor")))

    async def history(_):
        return JSONResponse({"runs": await run_in_threadpool(run_history, service.root())})

    async def asset(request):
        root = service.root()
        identity = request.path_params["asset_id"]
        for block in read_project(root)["blocks"]:
            for item in block["assets"]:
                if item["id"] == identity:
                    return FileResponse(inside(root, item["path"]), headers={"X-Content-Type-Options": "nosniff"})
        raise InputError("未找到素材。", "Material not found.", 404)

    async def output(request):
        root = service.root()
        p = request.path_params
        if p["tag"] != workspace_tag(root):
            raise InputError("未找到结果。", "Result not found.", 404)
        directory = run_dir(root, p["run_id"])
        headers = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox allow-scripts allow-downloads allow-forms; form-action 'none'"}
        if p["file"] == VIEWER_FILENAME:
            file = inside(directory, VIEWER_FILENAME)
            if not file.is_file():
                raise InputError("未找到结果。", "Result not found.", 404)
            download = request.query_params.get("download") == "1"
            headers["Cache-Control"] = "no-store"
            return FileResponse(file, media_type="text/html", headers=headers,
                                filename=VIEWER_FILENAME if download else None)
        # Previously issued reports have relative links into their original run.
        # New runs expose only the single public HTML, including while still running.
        status_path = inside(directory, "status.json")
        status = read_json(status_path) if status_path.is_file() else {}
        if (status.get("status") != "completed" or not status.get("report")
                or (directory / VIEWER_FILENAME).exists()):
            raise InputError("未找到结果。", "Result not found.", 404)
        bim = inside(directory, "production/private/dsh/work/bim")
        if not OUTPUT_FILE.fullmatch(p["file"]):
            report = bim / "delivery.json"
            overlays = []
            if p["file"].startswith("image_overlays/") and p["file"].endswith(".png") and report.is_file():
                overlays = read_json(report).get("source_image_feedback", {}).get("current_source_projections", [])
            if not any(row.get("overlay_image") == p["file"] for row in overlays):
                raise InputError("未找到结果。", "Result not found.", 404)
        file = inside(bim, p["file"])
        if not file.is_file():
            raise InputError("未找到结果。", "Result not found.", 404)
        return FileResponse(file, headers=headers)

    async def input_error(_, exc):
        code = "revision_conflict" if exc.status == 409 and ("draft" in str(exc).lower() or "workspace" in str(exc).lower()) else "input_error"
        return JSONResponse({"error": exc.messages[service.prefs["locale"]], "code": code}, status_code=exc.status)

    async def malformed(_, exc):
        return JSONResponse({"error": "请求格式无效。" if service.prefs["locale"] == "zh-CN" else "Invalid request."}, status_code=400)

    app = Starlette(lifespan=lifespan, exception_handlers={InputError: input_error, ValueError: malformed, KeyError: malformed},
        routes=[Route("/", home), Route("/api/instance", instance), Route("/api/state", state), Route("/api/run", run_status),
                Route("/api/run/trace", run_trace), Route("/api/runs", history),
                Route("/api/assets/{asset_id}", asset), Route("/api/{action}", api, methods=["POST"]),
                Route("/results/{tag}/{run_id}/{file:path}", output),
                Mount("/static", app=StaticFiles(directory=STATIC))])

    async def local_only(request, call_next):
        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        if (host.split(":")[0] not in {"127.0.0.1", "localhost"} or
                (request.method != "GET" and ((origin and urlsplit(origin).netloc != host)
                 or request.headers.get("sec-fetch-site") == "cross-site"))):
            return JSONResponse({"error": "Local requests only."}, status_code=403)
        # HTML forms can reach localhost from other sites. Require an origin for multipart writes.
        if request.method == "POST" and (request.url.path == "/api/blocks" and not origin or
                request.url.path != "/api/blocks" and not request.headers.get("content-type", "").startswith("application/json")):
            return JSONResponse({"error": "Invalid request type."}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/api/") or request.url.path in {
                "/static/viewer.html", "/static/viewer-draft.js", "/static/viewer-host.js"}:
            response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=local_only)
    return app


def _running_installation(port, identity):
    connection = HTTPConnection("127.0.0.1", port, timeout=4)
    try:
        connection.request("GET", "/api/instance")
        response = connection.getresponse()
        return (response.status == 200 and
                json.loads(response.read(512)) == {"product": "Sim-BIM Agent", "installation": identity})
    except (OSError, HTTPException, ValueError):
        return False
    finally:
        connection.close()


def serve(port=None, open_browser=True):
    identity = installation_id()
    if port is None:
        port = 20000 + int(identity[:8], 16) % 20000
    if not 0 <= port <= 65535:
        raise ValueError("Choose a local port between 0 and 65535.")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if os.name != "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as exc:
            if not port or not _running_installation(port, identity):
                raise ValueError(f"Cannot open local port {port}. Close the application using it and reopen Sim-BIM Agent.") from exc
            url = f"http://127.0.0.1:{port}/"
            print(url, flush=True)
            if open_browser:
                webbrowser.open(url)
            return 0
        listener.listen()
        url = f"http://127.0.0.1:{listener.getsockname()[1]}/"
        print(url, flush=True)
        if open_browser:
            threading.Timer(0.45, webbrowser.open, args=[url]).start()
        server = uvicorn.Server(uvicorn.Config(create_app(), access_log=False, log_level="warning"))
        server.run(sockets=[listener])
    return 0
