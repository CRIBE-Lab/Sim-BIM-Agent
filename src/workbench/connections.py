"""Account catalogs and local credentials. Secrets are never included in UI state."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import threading

import httpx
from dotenv import dotenv_values

from src.agent_runtime.estimation import get_model_profile
from src.agent_runtime.openai_subscription import SubscriptionCredentials, _atomic_write, _default_protector
from src.agent_runtime.providers import validate_provider_model
from .storage import ROOT, InputError, app_data, read_json, save_json
from .custom_connections import (
    custom_catalog, custom_connection, custom_connections_directory, delete_custom,
    freeze_custom_connection, is_custom_provider, refresh_custom, save_custom,
)

PROVIDERS = {"chatgpt-subscription": ("ChatGPT", "oauth"), "deepseek": ("DeepSeek API", "apiKey")}


def protected_deepseek_key_path() -> Path:
    path = app_data() / "deepseek.bin"
    if not path.exists():
        key = os.environ.get("DEEPSEEK_API_KEY")
        base = os.environ.get("DEEPSEEK_BASE_URL")
        if not key and (ROOT / ".env").is_file():
            values = dotenv_values(ROOT / ".env", interpolate=False)
            key, base = values.get("DEEPSEEK_API_KEY"), values.get("DEEPSEEK_BASE_URL")
        if not key or (base and base.rstrip("/") not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}):
            raise InputError("请在设置中填写 DeepSeek 官方 API Key。", "Enter your official DeepSeek API key in Settings.")
        _atomic_write(path, _default_protector().protect(key.strip().encode()))
    return path


def official_deepseek_key(path: Path | None = None) -> str:
    return _default_protector().unprotect((path or protected_deepseek_key_path()).read_bytes()).decode()


async def discover(provider, credentials, *, key=None):
    if provider == "chatgpt-subscription":
        rows = [(m.slug, m.display_name) for m in await credentials.list_models()]
    else:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get("https://api.deepseek.com/models",
                headers={"Authorization": "Bearer " + (key or official_deepseek_key())})
            response.raise_for_status()
            rows = [(m["id"], m["id"]) for m in response.json()["data"] if isinstance(m.get("id"), str)]
    models = []
    for model, label in dict.fromkeys(rows):
        try:
            validate_provider_model(provider, model)
            get_model_profile(model, strict=True)
        except ValueError:
            continue
        models.append({"id": model, "name": label or model})
    return models


class Connections:
    def __init__(self):
        self.guard = threading.RLock()
        self.credentials = SubscriptionCredentials()
        self.cache = app_data() / "catalogs.json"
        try:
            saved = read_json(self.cache)
        except (ValueError, OSError):
            saved = {}
        self.models = saved if isinstance(saved, dict) else {}
        self.busy = set()
        self.errors = {}

    def configured(self, provider):
        if provider == "chatgpt-subscription":
            return self.credentials.status()["signed_in"]
        try:
            protected_deepseek_key_path()
            return True
        except InputError:
            return False

    def view(self, locale):
        with self.guard:
            return [{"provider": provider, "label": label, "authType": auth,
                     "connected": self.configured(provider) and provider in self.models,
                     "models": self.models.get(provider, []), "busy": provider in self.busy,
                     "message": self.errors.get(provider, {}).get(locale, "")}
                    for provider, (label, auth) in PROVIDERS.items()] + custom_catalog(locale, self.busy)

    def manage(self, body):
        """Apply an action from the local settings form; return state via view()."""
        provider = body.get("provider")
        action = body.get("action", "refresh")
        if action == "create":
            provider, revision = save_custom(body)
            return self._refresh_custom(provider, revision)
        if is_custom_provider(provider):
            if action == "update":
                return self._refresh_custom(provider, changes=body)
            if action == "delete":
                return delete_custom(provider, body.get("revision"))
            if action == "refresh":
                return self._refresh_custom(provider, body.get("revision"))
            raise InputError("无效的连接操作。", "Invalid connection action.")
        return self.refresh(provider, action, body.get("apiKey"))

    def _refresh_custom(self, provider, revision=None, *, changes=None):
        with self.guard:
            if provider in self.busy:
                raise InputError("正在更新连接。", "The connection is being updated.", 409)
            self.busy.add(provider)
        try:
            if changes is not None:
                _, revision = save_custom(changes, provider=provider)
            refresh_custom(provider, revision)
        finally:
            with self.guard:
                self.busy.discard(provider)

    def refresh(self, provider, action="refresh", api_key=None):
        if action == "refresh" and is_custom_provider(provider):
            return self._refresh_custom(provider)
        if not isinstance(provider, str) or provider not in PROVIDERS or action not in ("refresh", "login", "configure"):
            raise InputError("无效的连接操作。", "Invalid connection action.")
        if (action == "login" and provider != "chatgpt-subscription") or (action == "configure" and provider != "deepseek"):
            raise InputError("此连接不支持该操作。", "This action is unavailable for this connection.")
        if action == "configure" and (not isinstance(api_key, str) or not api_key.strip()):
            raise InputError("请输入 API Key。", "Enter an API key.")
        with self.guard:
            if provider in self.busy:
                raise InputError("正在更新连接。", "The connection is being updated.", 409)
            self.busy.add(provider)
            self.errors.pop(provider, None)
        def fetch():
            try:
                if action == "login":
                    asyncio.run(self.credentials.login())
                models = asyncio.run(discover(provider, self.credentials, key=api_key.strip() if action == "configure" else None))
                if action == "configure":
                    _atomic_write(app_data() / "deepseek.bin", _default_protector().protect(api_key.strip().encode()))
                with self.guard:
                    self.models[provider] = models
                    save_json(self.cache, self.models)
            except Exception as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                message = InputError("未能取得模型列表，请检查网络和连接设置。", "Could not get the model list. Check the connection and network.")
                if status in (401, 403):
                    message = InputError("账号认证失败，请检查凭据和权限。", "Authentication failed. Check your credentials and access.")
                with self.guard:
                    self.errors[provider] = message.messages
                    if action != "configure":
                        self.models.pop(provider, None)
                        save_json(self.cache, self.models)
                if action != "login":
                    raise message from exc
            finally:
                with self.guard:
                    self.busy.discard(provider)
        if action == "login":
            threading.Thread(target=fetch, daemon=True).start()
        else:
            fetch()

    def warm(self):
        providers = [provider for provider in PROVIDERS if self.configured(provider)]
        providers += [row["provider"] for row in custom_catalog("en", ())]
        for provider in providers:
            def refresh(p=provider):
                try:
                    self.refresh(p)
                except InputError:
                    pass
            threading.Thread(target=refresh, daemon=True).start()
