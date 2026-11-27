"""Resolve the two product routes without sharing model credentials with DSH."""
from pathlib import Path

from src.agent_runtime.connections import ConnectionDescriptor, ResolvedConnection, resolve_connection
from src.agent_runtime.providers import CHATGPT_SUBSCRIPTION, DEEPSEEK, validate_provider_model, is_custom_provider


def model_connection(provider, model, *, auth_dir=None, credentials_file=None, protected_key=None,
                     custom_directory=None, revision=None):
    validate_provider_model(provider, model)
    if is_custom_provider(provider):
        from src.workbench.connections import custom_connection
        spec = custom_connection(provider, directory=custom_directory, revision=revision)
        return ResolvedConnection(ConnectionDescriptor(route_id=provider, base_url=spec["base_url"],
            billing_mode="provider_defined", adapter_kind="openai_chat_completions"), api_key=spec["api_key"] or "")
    if provider == CHATGPT_SUBSCRIPTION:
        return resolve_connection(provider, None, chatgpt_auth_dir=auth_dir)
    if provider != DEEPSEEK:
        raise ValueError("unsupported DSH model provider")
    if credentials_file:
        return resolve_connection(provider, Path(credentials_file), model=model)
    from src.workbench.connections import official_deepseek_key
    key = official_deepseek_key(Path(protected_key) if protected_key else None)
    if not key:
        raise ValueError("DeepSeek API key is missing")
    return ResolvedConnection(ConnectionDescriptor(route_id=provider, base_url="https://api.deepseek.com",
                              billing_mode="metered", adapter_kind="openai_chat_completions"), api_key=key)


class ApiCredential:
    def __init__(self, connection):
        self._key = connection.api_key
        self.descriptor = connection.descriptor

    async def get_access_token(self):
        return self._key
