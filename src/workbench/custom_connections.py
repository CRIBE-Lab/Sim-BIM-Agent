"""User-supplied OpenAI-compatible connections and their frozen run credentials."""
from __future__ import annotations

import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid

import httpx

from src.agent_runtime.openai_subscription import _atomic_write, _default_protector
from src.agent_runtime.providers import is_custom_provider
from .storage import InputError, app_data, locked, read_json, save_json


def custom_connections_directory() -> Path:
    return app_data() / "custom-connections"


def _active(directory):
    path = directory / "connections.json"
    return read_json(path) if path.exists() else {}


def _entry(rows, provider, revision=None):
    if not is_custom_provider(provider) or provider not in rows:
        raise InputError("连接不存在。", "Connection not found.", 404)
    row = rows[provider]
    if revision is not None and revision != row["revision"]:
        raise InputError("连接已更改，请刷新后重试。", "The connection changed. Refresh and try again.", 409)
    return row


def _blob(directory, provider, revision, *, frozen=False):
    if not is_custom_provider(provider) or not isinstance(revision, str) or not re.fullmatch("[0-9a-f]{32}", revision):
        raise InputError("无效的连接版本。", "Invalid connection revision.")
    return directory / ("frozen" if frozen else "current") / provider / (revision + ".bin")


def _open(directory, provider, revision, *, frozen=False):
    raw = _blob(directory, provider, revision, frozen=frozen).read_bytes()
    data = json.loads(_default_protector().unprotect(raw))
    if data["provider"] != provider or data["revision"] != revision:
        raise ValueError("Stored credential does not match the requested connection revision")
    if "output_token_parameter" not in data:
        data["output_token_parameter"] = _output_parameter(data["base_url"], "auto")
    return data


def _output_parameter(url, setting):
    if setting == "auto":
        host = urlsplit(url).hostname.rstrip(".")
        return "max_completion_tokens" if host == "api.openai.com" else "max_tokens"
    if setting not in ("max_tokens", "max_completion_tokens"):
        raise InputError("请选择有效的输出上限参数。", "Select a valid output token parameter.")
    return setting


def custom_connection(provider_id: str, *, directory: Path | None = None, revision: str | None = None) -> dict:
    """For the local transport only. Never serialize this result into a run or UI response."""
    directory = Path(directory) if directory is not None else custom_connections_directory()
    if revision is not None:
        return _open(directory, provider_id, revision, frozen=True)
    with locked(directory):
        row = _entry(_active(directory), provider_id)
        return _open(directory, provider_id, row["revision"])


def freeze_custom_connection(provider_id: str, *, expected_revision: str | None = None) -> str:
    """Pin the exact endpoint, credentials and limits for running or resuming a job."""
    directory = custom_connections_directory()
    with locked(directory):
        row = _entry(_active(directory), provider_id, expected_revision)
        revision = row["revision"]
        target = _blob(directory, provider_id, revision, frozen=True)
        if not target.exists():
            _atomic_write(target, _blob(directory, provider_id, revision).read_bytes())
        return revision


def _local_host(host):
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host or host.endswith((".localhost", ".local"))
    return addr.is_private or addr.is_loopback or addr.is_link_local


def base_url(value):
    error = InputError("请输入 HTTPS API Base URL 或本地 HTTP 地址，且不要在地址中包含凭据、查询参数或片段。",
                       "Enter an HTTPS API base URL or a local HTTP URL without credentials, query parameters or fragments.")
    if not isinstance(value, str):
        raise error
    url = value.strip().rstrip("/")
    if not url or any(c.isspace() or ord(c) < 32 or c in "\\?#" for c in url):
        raise error
    try:
        parts = urlsplit(url)
        # Accessing port also rejects malformed and out-of-range ports.
        port = parts.port
        if (not parts.hostname or parts.username is not None or parts.password is not None or port == 0
                or not (parts.scheme == "https" or (parts.scheme == "http" and _local_host(parts.hostname)))):
            raise error
        return str(httpx.URL(url)).rstrip("/")
    except (ValueError, httpx.InvalidURL):
        raise error from None


def _origin(url):
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and all(c.isprintable() for c in value)


def _configuration(body, previous):
    previous = previous or {}
    label = body.get("label", previous.get("label"))
    if not _text(label):
        raise InputError("请输入连接名称。", "Enter a connection name.")
    protocol = body.get("protocol", previous.get("protocol", "openai-chat"))
    if protocol != "openai-chat":
        raise InputError("暂不支持此 API 协议。", "This API protocol is not supported.")
    url = base_url(body.get("baseUrl", previous.get("base_url")))
    key = body.get("apiKey", "")
    clear = body.get("clearApiKey", False)
    if (not isinstance(key, str) or type(clear) is not bool or (key.strip() and clear)
            or (key.strip() and (not key.strip().isascii() or not key.strip().isprintable()))):
        raise InputError("无效的 API Key。", "Invalid API key.")
    key = key.strip()
    if not key and not clear and previous.get("api_key"):
        if _origin(url) != _origin(previous["base_url"]):
            raise InputError("更换服务器时，请重新输入 API Key 或清除已保存的密钥。",
                             "When changing servers, enter the API key again or clear the saved key.")
        key = previous["api_key"]
    context = body.get("contextWindow", previous.get("context_window", 128000))
    output = body.get("outputTokens", previous.get("output_tokens", 8192))
    if type(context) is not int or type(output) is not int or not 0 < output < context:
        raise InputError("令牌数须为正整数，输出上限须小于上下文窗口。", "Use positive token limits with output below the context window.")
    manual = body.get("modelIds", previous.get("model_ids", []))
    if not isinstance(manual, list) or not all(_text(m) for m in manual):
        raise InputError("手动型号须为非空的模型 ID。", "Manual models must have nonempty IDs.")
    return {"label": label.strip(), "base_url": url, "api_key": key, "protocol": protocol,
            "context_window": context, "output_tokens": output, "model_ids": list(dict.fromkeys(m.strip() for m in manual))}


def save_custom(body, *, provider=None):
    directory = custom_connections_directory()
    with locked(directory):
        rows = _active(directory)
        old = _entry(rows, provider, body.get("revision")) if provider is not None else None
        provider = provider or ("custom-" + uuid.uuid4().hex)
        previous = _open(directory, provider, old["revision"]) if old else None
        data = _configuration(body, previous)
        setting = body.get("outputTokenParameter", (old or {}).get("outputTokenParameter", "auto"))
        data["output_token_parameter"] = _output_parameter(data["base_url"], setting)
        revision = uuid.uuid4().hex
        data.update(provider=provider, revision=revision)
        blob = _blob(directory, provider, revision)
        _atomic_write(blob, _default_protector().protect(json.dumps(data, ensure_ascii=False).encode()))
        same_route = previous and all(previous[k] == data[k] for k in ("base_url", "api_key", "protocol"))
        rows[provider] = {"provider": provider, "revision": revision, "label": data["label"],
                          "baseUrl": data["base_url"], "protocol": data["protocol"], "hasApiKey": bool(data["api_key"]),
                          "contextWindow": data["context_window"], "outputTokens": data["output_tokens"],
                          "outputTokenParameter": setting,
                          "modelIds": data["model_ids"], "discovered": old["discovered"] if same_route else [],
                          "error": {}}
        try:
            save_json(directory / "connections.json", rows)
        except BaseException:
            blob.unlink(missing_ok=True)
            raise
        if old:
            _blob(directory, provider, old["revision"]).unlink(missing_ok=True)
        return provider, revision


def delete_custom(provider, revision=None):
    directory = custom_connections_directory()
    with locked(directory):
        rows = _active(directory)
        row = _entry(rows, provider, revision)
        del rows[provider]
        save_json(directory / "connections.json", rows)
        _blob(directory, provider, row["revision"]).unlink(missing_ok=True)


def discover_custom(connection):
    headers = {"Accept": "application/json"}
    if connection["api_key"]:
        headers["Authorization"] = "Bearer " + connection["api_key"]
    # Local routes connect directly even on machines with a system HTTPS proxy.
    host = urlsplit(connection["base_url"]).hostname
    with httpx.Client(timeout=12, trust_env=not _local_host(host), follow_redirects=True) as client:
        response = client.get(connection["base_url"] + "/models", headers=headers)
        response.raise_for_status()
        body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise ValueError("Unexpected model catalog format")
    models = {}
    for row in body["data"]:
        if not isinstance(row, dict) or not _text(row.get("id")):
            continue
        identity = row["id"].strip()
        label = row.get("display_name") or row.get("name")
        models[identity] = {"id": identity, "name": label.strip() if _text(label) else identity}
    return list(models.values())


def refresh_custom(provider, revision=None):
    directory = custom_connections_directory()
    connection = custom_connection(provider, directory=directory)
    if revision is not None and connection["revision"] != revision:
        raise InputError("连接已更改，请刷新后重试。", "The connection changed. Refresh and try again.", 409)
    error = {}
    try:
        discovered = discover_custom(connection)
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        discovered = None
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403}:
            error = {"zh-CN": "模型目录访问被拒绝，请检查凭据和权限。", "en": "Model catalog access was denied. Check your credentials and permissions."}
        else:
            error = {"zh-CN": "无法拉取模型目录。可检查 Base URL 或手动填写模型 ID。", "en": "Could not fetch the catalog. Check the base URL or enter model IDs manually."}
    with locked(directory):
        rows = _active(directory)
        row = _entry(rows, provider, connection["revision"])
        row["error"] = error
        if discovered is not None:
            row["discovered"] = discovered
        save_json(directory / "connections.json", rows)


def custom_catalog(locale, busy):
    rows = _active(custom_connections_directory())
    entries = []
    for provider, row in rows.items():
        models = {m["id"]: m for m in row["discovered"]}
        for model in row["modelIds"]:
            models.setdefault(model, {"id": model, "name": model})
        public = {k: v for k, v in row.items() if k not in {"discovered", "error"}}
        public.setdefault("outputTokenParameter", "auto")
        entries.append({**public, "authType": "custom", "custom": True, "connected": bool(models),
                        "models": list(models.values()), "busy": provider in busy, "message": row["error"].get(locale, "")})
    return entries
