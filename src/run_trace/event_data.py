"""Timeline rows and visible parts of stored messages."""
from datetime import datetime
import json
import math


def event(identity, kind, title, *, start=None, end=None, turn=None, status="recorded",
          summary="", **detail):
    return dict(id=identity, kind=kind, title=title, summary=summary, start=start,
                end=end, turn=turn, status=status, detail=detail)


def timestamp(value):
    if isinstance(value, dict):
        value = value.get("value") if value.get("kind") == "known" else None
    if type(value) in (float, int) and math.isfinite(value):
        return value
    if isinstance(value, str):
        try:
            date = datetime.fromisoformat(value)
            return date.timestamp() if date.tzinfo is not None else None
        except (ValueError, OverflowError, OSError):
            pass
    return None


def json_lines(path):
    """A torn last write can be observed while the recorder is still running."""
    with path.open("rb") as log:
        for number, line in enumerate(log, 1):
            if line.strip():
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError) as exc:
                    if not line.endswith(b"\n"):
                        yield {"_incomplete_line": number}
                        return
                    raise ValueError(f"{path.name}, line {number}: invalid JSON") from exc
                if not isinstance(row, dict):
                    raise ValueError(f"{path.name}, line {number}: expected a JSON object")
                yield row


def visible(value):
    """Discard private model blocks before any result leaves the reader."""
    if isinstance(value, list):
        return [visible(x) for x in value if not (isinstance(x, dict)
                and x.get("type") in {"thinking", "reasoning", "redacted_thinking", "analysis"})]
    if isinstance(value, dict):
        hidden = {"reasoning_content", "reasoning", "thinking", "analysis", "thought",
                  "encrypted_content", "thinkingSignature", "reasoningSignature"}
        result = {k: visible(v) for k, v in value.items() if k not in hidden}
        if value.get("thinking") in ({"type": "enabled"}, {"type": "disabled"}):
            result["thinking"] = value["thinking"]
        return result
    return value


def arguments(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def tool_feedback(result):
    """Read machine feedback, including MCP responses that carry JSON as text."""
    if isinstance(result, dict):
        meta = result.get("structuredContent")
        if isinstance(meta, dict):
            result = meta
        else:
            result = result.get("content", result)
    if isinstance(result, list):
        for block in result:
            if isinstance(block, dict) and block.get("type") == "text":
                value = arguments(block.get("text"))
                if isinstance(value, dict):
                    result = value
                    break
    if isinstance(result, str):
        result = arguments(result)
    if not isinstance(result, dict):
        return {}
    return {key: result[key] for key in
            ("status", "stage", "error", "source_geometry_ready", "candidate", "selection_origin", "counts")
            if key in result}


def tool_summary(feedback, status):
    error = feedback.get("error")
    if error:
        return str(error)
    if feedback.get("source_geometry_ready") is True and feedback.get("candidate"):
        return f"已保存几何候选 {feedback['candidate']}"
    return {"failed": "工具返回错误", "completed": "工具已返回", "unknown": "执行结果未知"}[status]
