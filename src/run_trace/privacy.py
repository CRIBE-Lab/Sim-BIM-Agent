"""Keep local trace exports useful without copying credentials or private reasoning."""

import json
import re


_PRIVATE_KEYS = {"analysis", "reasoning_content", "reasoning_details", "reasoning_text", "thinking_text",
                 "thinking_content", "encrypted_content", "thought", "thoughts", "reasoning_signature",
                 "thinking_signature", "thought_signature", "signature", "redacted_thinking"}
_SECRET = re.compile(r"^(?:authorization|proxy_authorization|cookie|set_cookie|password|secret|"
                     r"(?:[\w]+_)?(?:api_key|apikey|api_secret|access_token|refresh_token|id_token|auth_token))$", re.I)
_DATA = re.compile(r"data:[^\s,;]*;base64,[a-zA-Z0-9+/=_-]+", re.I)
_AUTH = re.compile(r"\b(?:Bearer|Basic)\s+[a-z0-9_./+~=-]+", re.I)
_API_KEY = re.compile(r"\bsk-[a-zA-Z0-9_-]{14,}")
_CONFIG_SECRET = re.compile(r"""(?i)\b([\w-]*(?:api[_-]?key|(?:access|refresh|auth|id)[_-]?token|password|secret))"""
                            r"""(["']?\s*[:=]\s*["']?)[^\s,;"'}]+""")
_THOUGHT_TAG = re.compile(r"<(think|thinking|reasoning)\b[^>]*>.*?(?:</\1\s*>|$)", re.I | re.S)


def public_value(value):
    if isinstance(value, dict):
        if value.get("type") in ("thinking", "redacted_thinking", "reasoning", "reasoning_text"):
            return {"type": value["type"], "omitted": True}
        result = {}
        for key, item in value.items():
            normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower().replace("-", "_")
            if _SECRET.fullmatch(normalized):
                result[key] = "[凭据已隐藏]"
            elif normalized in _PRIVATE_KEYS or (normalized in ("reasoning", "thinking") and
                    isinstance(item, str) and item not in ("high", "medium", "low", "enabled", "disabled", "none")):
                result[key] = "[推理原文已省略]"
            elif normalized in ("data", "base64", "b64_json") and isinstance(item, str) and len(item) > 256 and re.fullmatch(r"[A-Za-z0-9+/=\r\n]+", item):
                result[key] = "[二进制数据已省略]"
            else:
                result[key] = public_value(item)
        return result
    if isinstance(value, list):
        return [public_value(item) for item in value]
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                parsed, split = json.JSONDecoder().raw_decode(stripped)
            except (ValueError, RecursionError):
                pass
            else:
                safe = public_value(parsed)
                tail = stripped[split:]
                remainder = public_value(tail) if tail else ""
                if safe != parsed or tail != remainder:
                    return json.dumps(safe, ensure_ascii=False) + remainder
        value = _THOUGHT_TAG.sub("[推理原文已省略]", value)
        value = _DATA.sub("[内嵌媒体]", value)
        value = _AUTH.sub("[凭据已隐藏]", value)
        value = _API_KEY.sub("[凭据已隐藏]", value)
        return _CONFIG_SECRET.sub(r"\1\2[凭据已隐藏]", value)
    return value
