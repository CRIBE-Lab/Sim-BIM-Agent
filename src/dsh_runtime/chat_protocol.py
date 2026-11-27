"""Bridge stateless DSH Responses items and a configured Chat Completions API."""
import hashlib
import json
import re

from src.agent_runtime.openai_subscription import _atomic_write, _default_protector


class CallExtensions:
    """Private provider metadata bound to the exact tool call, independent of DSH's transcript."""
    def __init__(self, directory):
        self.directory = directory
        self.protector = _default_protector()

    def save(self, call, extra):
        if not isinstance(extra, dict):
            raise ValueError("invalid chat tool extension")
        raw = json.dumps({"call": call, "extra_content": extra}, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
        digest = hashlib.sha256(raw).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.directory / digest, self.protector.protect(raw))
        return digest

    def load(self, digest, call):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid chat tool extension reference")
        raw = self.protector.unprotect((self.directory / digest).read_bytes())
        data = json.loads(raw)
        if hashlib.sha256(raw).hexdigest() != digest or data["call"] != call:
            raise ValueError("chat tool extension does not match this call")
        return data["extra_content"]


def content_parts(content):
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if part["type"] in {"input_text", "output_text"}:
            parts.append({"type": "text", "text": part["text"]})
        elif part["type"] == "input_image":
            parts.append({"type": "image_url", "image_url": {"url": part["image_url"]}})
        else:
            raise ValueError("unsupported chat content part")
    if all(p["type"] == "text" for p in parts):
        return "\n".join(p["text"] for p in parts)
    return parts


def chat_request(body, output_tokens, extensions=None, output_parameter="max_tokens"):
    messages, images, assistant = [], [], {}
    provider_ids = {}
    for item in body["input"]:
        kind, role = item.get("type"), item.get("role")
        if images and kind != "function_call_output":
            messages.append({"role": "user", "content": images})
            images = []
        if kind == "reasoning":
            reasoning = "".join(p["text"] for p in item.get("content", ()) if p["type"] == "reasoning_text")
            if reasoning:
                key = "reasoning" if item["id"].startswith("reason_") else "reasoning_content"
                assistant[key] = reasoning
        elif kind == "function_call":
            provider_id = item.get("chat_original_call_id", item["call_id"])
            provider_ids[item["call_id"]] = provider_id
            call = {"type": "function", "id": provider_id,
                    "function": {"name": item["name"], "arguments": item["arguments"]}}
            if "chat_extension_sha256" in item:
                call["extra_content"] = extensions.load(item["chat_extension_sha256"], call)
            assistant.setdefault("tool_calls", []).append(call)
        elif role == "assistant":
            assistant["content"] = content_parts(item.get("content", ""))
        else:
            if assistant:
                messages.append({"role": "assistant", "content": None, **assistant})
                assistant = {}
            if kind == "function_call_output":
                provider_id = provider_ids[item["call_id"]]
                output = content_parts(item["output"])
                if isinstance(output, list):
                    # Chat expects a text tool receipt. Supply its images as a user
                    # attachment after all parallel tool receipts, retaining call IDs.
                    images.extend([{"type": "text", "text": "Images from tool call " + provider_id},
                                   *(p for p in output if p["type"] == "image_url")])
                    output = "\n".join(p["text"] for p in output if p["type"] == "text")
                messages.append({"role": "tool", "tool_call_id": provider_id, "content": output})
            elif role in {"system", "developer", "user"}:
                messages.append({"role": "system" if role == "developer" else role,
                                 "content": content_parts(item["content"])})
            else:
                raise ValueError("unsupported DSH input item for Chat Completions")
    if assistant:
        messages.append({"role": "assistant", "content": None, **assistant})
    if images:
        messages.append({"role": "user", "content": images})
    if output_parameter not in ("max_tokens", "max_completion_tokens"):
        raise ValueError("invalid Chat Completions output limit field")
    request = {"model": body["model"], "messages": messages, output_parameter: output_tokens, "stream": False}
    if body.get("tools"):
        request["tools"] = [{"type": "function", "function": {
            key: tool[key] for key in ("name", "description", "parameters") if key in tool}} for tool in body["tools"]]
    if body.get("tool_choice"):
        request["tool_choice"] = body["tool_choice"]
    return request


def chat_events(reply, model, turn, extensions=None):
    """Return complete tool actions only after validating the entire provider reply."""
    choice, = reply["choices"]
    msg, finish = choice["message"], choice["finish_reason"]
    if msg.get("role") != "assistant" or finish not in {"stop", "tool_calls", "length", "content_filter"}:
        raise ValueError("unsupported Chat Completions response")
    ident = hashlib.sha256(f"{turn}/{reply.get('id', '')}".encode()).hexdigest()[:24]
    response = {"id": "resp_" + ident, "object": "response", "created_at": reply.get("created", 0),
                "model": reply.get("model") or model, "status": "in_progress", "output": []}
    output = []
    events = [("response.created", {"response": dict(response)})]
    if finish in {"stop", "tool_calls"}:
        for key in ("reasoning_content", "reasoning"):
            if isinstance(msg.get(key), str) and msg[key]:
                output.append({"type": "reasoning", "id": ("reason_" if key == "reasoning" else "rs_") + ident,
                    "summary": [], "content": [{"type": "reasoning_text", "text": msg[key]}]})
                break
        if msg.get("content"):
            if not isinstance(msg["content"], str):
                raise ValueError("Chat Completions assistant content must be text")
            output.append({"type": "message", "id": "msg_" + ident, "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": msg["content"], "annotations": []}]})
        seen = set()
        for i, call in enumerate(msg.get("tool_calls") or []):
            if call["type"] != "function" or not isinstance(call.get("id"), str) or not call["id"] or call["id"] in seen:
                raise ValueError("invalid or duplicate Chat Completions tool call")
            name, args = call["function"]["name"], call["function"]["arguments"]
            if not isinstance(name, str) or not name or not isinstance(json.loads(args), dict):
                raise ValueError("invalid Chat Completions function")
            seen.add(call["id"])
            item = {"type": "function_call", "id": f"fc_{ident}_{i}", "call_id": call["id"],
                    "name": name, "arguments": args, "status": "completed"}
            if not re.fullmatch(r"call_[A-Za-z0-9_-]{1,58}", call["id"]):
                item.update(chat_original_call_id=call["id"], call_id=f"call_{ident}_{i}")
            if "extra_content" in call:
                original = {"type": "function", "id": call["id"], "function": {"name": name, "arguments": args}}
                item["chat_extension_sha256"] = extensions.save(original, call["extra_content"])
            output.append(item)
    for i, item in enumerate(output):
        common = {"output_index": i, "item_id": item["id"]}
        events.append(("response.output_item.added", {"output_index": i, "item": {
            **item, **({"arguments": ""} if item["type"] == "function_call" else {"content": []})}}))
        if item["type"] == "function_call":
            events.append(("response.function_call_arguments.delta", {**common, "delta": item["arguments"]}))
            events.append(("response.function_call_arguments.done", {**common, "arguments": item["arguments"]}))
        elif item["type"] == "message":
            events.append(("response.content_part.added", {**common, "content_index": 0,
                           "part": {"type": "output_text", "text": "", "annotations": []}}))
            events.append(("response.output_text.delta", {**common, "content_index": 0, "delta": item["content"][0]["text"]}))
        events.append(("response.output_item.done", {"output_index": i, "item": item}))
    status = "incomplete" if finish in {"length", "content_filter"} else "completed"
    response.update(status=status, output=output)
    if status == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens" if finish == "length" else "content_filter"}
    usage = reply.get("usage")
    if isinstance(usage, dict):
        response["usage"] = {dest: usage[source] for source, dest in (
            ("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"), ("total_tokens", "total_tokens"),
            ("completion_tokens_details", "output_tokens_details"), ("prompt_tokens_details", "input_tokens_details")
        ) if source in usage}
    events.append(("response." + status, {"response": response}))
    for i, (name, values) in enumerate(events):
        yield name, json.dumps({"type": name, "sequence_number": i, **values}, ensure_ascii=False)
