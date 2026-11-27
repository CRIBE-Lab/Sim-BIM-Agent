"""Incremental, read-only view of native Harness, observer and supervisor journals."""
from __future__ import annotations

import base64
import json
import math
import re

from src.run_trace.event_data import arguments, timestamp, tool_feedback
from src.run_trace.privacy import public_value
from .runs import RUN_ID, run_dir, status_at
from .storage import InputError, inside, read_json


_ATTEMPT = re.compile(r"attempt-([0-9]{3,})$")
_BATCH = 128
_TEXT_LIMIT = 4000


def _when(value):
    value = timestamp(value)
    # Some providers use an integer Unix millisecond stamp. The UI uses seconds.
    return value / 1000 if value is not None and value > 10**11 else value


def _text(value, limit=_TEXT_LIMIT):
    if not isinstance(value, str):
        return None
    return public_value(value)[:limit]


def _fields(data, names):
    """Only copy scalar values of known, public domain fields."""
    if not isinstance(data, dict):
        return {}
    return {k: (_text(v, 180) if isinstance(v, str) else v)
            for k, v in data.items() if k in names and isinstance(v, (str, bool, int, float))
            and (not isinstance(v, float) or math.isfinite(v))}


def _compact(**fields):
    return {k: v for k, v in fields.items() if v is not None and v != {} and v != []}


def _project_harness(row, attempt, position, state):
    phase = row.get("phase") if row.get("type") == "status" else row.get("type")
    if phase == "turn_start":
        state[2:] = [0, 0]
    if phase in {"turn_start", "step_start", "step_end", "turn_end"}:
        for field, index in (("turn", 1), ("step", 2)):
            if type(row.get(field)) is int:
                state[index] = row[field]
    _, turn, step, received = state
    base = dict(source="harness", attempt=attempt, turn=turn, step=step, time=_when(row.get("timestamp")))
    prefix = f"h{attempt}:{turn}"
    if phase in {"step_start", "step_end"}:
        usage = _fields(row.get("usage"), ("inputTokens", "outputTokens", "totalTokens",
                                          "input_tokens", "output_tokens", "total_tokens"))
        if phase == "step_start":
            state[3] = 0
            status = "running"
        else:
            # DeepSeek also emits step_end with no usage or response on a transport timeout.
            state[3] = int(bool(received) or any(type(n) in (int, float) and n > 0 for n in usage.values()))
            status = "completed" if state[3] else "recorded"
        return _compact(**base, id=f"{prefix}:{step}", kind="step",
                        status=status, result=usage)
    if phase in {"tool_call", "tool_result"}:
        call = _text(row.get("callId"), 240)
        if not call:
            return None
        identity = f"{prefix}:{step}:tool:{call}"
        if phase == "tool_call":
            state[3] = 1
            data = arguments(row.get("input"))
            args = _fields(data, ("name", "topic", "candidate", "view_id", "image", "height_m", "width_m",
                                  "azimuth_degrees", "elevation_degrees", "axis", "task_id"))
            tasks = data.get("tasks") if isinstance(data, dict) else None
            workers = [_text(t["task_id"], 160) for t in tasks if isinstance(t, dict)
                       and isinstance(t.get("task_id"), str)] if isinstance(tasks, list) else []
            return _compact(**base, id=identity, kind="tool", tool=_text(row.get("tool"), 180), call_id=call,
                            args=args, workers=workers, status="running")
        feedback = tool_feedback(row.get("result"))
        result = _fields(feedback, ("stage", "candidate", "status", "source_geometry_ready", "selection_origin"))
        if row.get("truncated") is True:
            result["truncated"] = True
        counts = _fields(feedback.get("counts"), ("zones", "spaces", "surfaces", "boundaries", "openings", "windows", "floors"))
        if counts:
            result["counts"] = counts
        failed = bool(feedback.get("error")) or feedback.get("status") in ("error", "failed", "rejected")
        reported = row.get("status")
        status = "failed" if failed or reported in ("error", "failed", "rejected") else (
            "completed" if reported == "completed" else "recorded")
        return _compact(**base, id=identity, kind="tool", call_id=call, result=result, status=status)
    if phase == "text" and row.get("text"):
        state[3] = 1
        return _compact(**base, id=f"{prefix}:text:{position}", kind="message", text=_text(row["text"]))
    if phase in {"session", "turn_end"}:
        status = "running"
        if phase == "turn_end":
            reason = row.get("reason")
            details = reason if isinstance(reason, dict) else {}
            ended = details.get("kind")
            if details.get("error") or ended in ("error", "abort", "aborted", "cancelled", "timeout"):
                status = "failed"
            else:
                status = "completed" if ended == "completed" else "recorded"
        return _compact(**base, id=f"h{attempt}:session:{position}", kind="session", action=phase, status=status)
    return None


def _project_worker(row):
    payload = row.get("payload") or {}
    task = _text(row.get("task_id"), 160)
    parent = row.get("parent_task") or {}
    if not task or not parent.get("task_id"):
        return  # The observer pool itself is bookkeeping, not a selected worker.
    base = dict(source="worker", task=task, parent=_text(parent["task_id"], 160),
                time=_when(row.get("occurred_at")))
    kind = payload.get("event_type")
    if kind == "run_lifecycle":
        action, reason = payload.get("action"), payload.get("reason")
        status = ("completed" if reason == "completed" else "failed") if action == "stop" else (
            "failed" if action in ("failure", "timeout", "cancel") else "running")
        model_failure = payload.get("model_failure") or {}
        request = _text(model_failure.get("request_event_id"), 240)
        if request and status == "failed":
            yield _compact(**base, id=f"w:{task}:step:{request}", kind="step", status="failed")
        # The UI merges updates for the same task; clear obsolete failure text on recovery.
        yield _compact(**base, id=f"w:{task}", kind="worker", action=_text(action, 80),
                       status=status, text=_text(reason, 200) if status == "failed" else "")
    if kind in {"adapter_request", "model_response"}:
        request = row.get("event_id") if kind == "adapter_request" else payload.get("request_event_id")
        remote = (payload.get("versions") or {}).get("remote_model") or {}
        visible = payload.get("visible_text")
        text = _text("\n".join(v for v in visible if isinstance(v, str))) if isinstance(visible, list) else None
        yield _compact(**base, id=f"w:{task}:step:{request}", kind="step",
                       model=_text(remote.get("remote_alias"), 160), text=text or None,
                       status="running" if kind == "adapter_request" else "completed")
    if kind in {"tool_invocation", "tool_execution"}:
        call = _text(payload.get("call_id"), 240)
        if call:
            outcomes = {"succeeded": "completed", "failed": "failed"}
            status = "running" if kind == "tool_invocation" else outcomes.get(payload.get("outcome"), "recorded")
            yield _compact(**base, id=f"w:{task}:tool:{call}", kind="tool", call_id=call,
                           tool=_text(payload.get("tool_name"), 180), status=status)


def _project_retry(row, position):
    action = row.get("event")
    if action in {"automatic_request_recovery", "automatic_delivery_continuation"}:
        return _compact(source="runtime", id=f"retry:{position}", kind="retry", action=action,
                        attempt=row.get("attempt", 0) + 1, time=_when(row.get("time")),
                        result=_fields(row, ("prior_turn", "backoff_seconds", "after_turn")))


def _decode(cursor, identity):
    if not cursor:
        return {}
    try:
        if len(cursor) > 16384:
            raise ValueError
        run, states = json.loads(base64.urlsafe_b64decode(cursor))
        if run != identity or not isinstance(states, dict) or not all(
            isinstance(s, list) and len(s) in (3, 4) and all(type(n) is int and 0 <= n < 2**60 for n in s)
            for s in states.values()
        ):
            raise ValueError
        return {key: s if len(s) == 4 else [*s, 0] for key, s in states.items()}
    except (TypeError, UnicodeError, ValueError):
        raise InputError("运行记录位置无效，请重新打开。", "Invalid trace cursor. Reopen the run.") from None


def _journals(directory):
    session = inside(directory, "production/private/dsh/session")
    yield "1", inside(directory, "production/private/dsh/session/events.jsonl")
    attempts = [(int(m[1]), path) for path in session.glob("attempt-*") if (m := _ATTEMPT.fullmatch(path.name))]
    for attempt, path in sorted(attempts):
        yield str(attempt), inside(directory, str((path / "events.jsonl").relative_to(directory)))
    yield "worker", inside(directory, "production/private/workers/events.jsonl")
    yield "retry", inside(directory, "production/control/relay.jsonl")


def _page(directory, cursor):
    states = _decode(cursor, directory.name)
    events, more = [], False
    for source, path in _journals(directory):
        if not path.is_file():
            continue
        state = states.setdefault(source, [0, 0, 0, 0])
        with path.open("rb") as file:
            file.seek(state[0])
            for _ in range(_BATCH):
                position = file.tell()
                raw = file.readline()
                if not raw.endswith(b"\n"):
                    break  # Keep the last byte offset until the native writer completes its row.
                state[0] = file.tell()
                row = json.loads(raw)
                if source == "worker":
                    events.extend(_project_worker(row))
                    continue
                if source == "retry":
                    event = _project_retry(row, position)
                else:
                    event = _project_harness(row, int(source), position, state)
                if event:
                    if (event.get("action") == "turn_end" and event["status"] == "failed"
                            and state[2] and not state[3]):
                        events.append({"source": "harness", "attempt": int(source), "turn": state[1],
                                       "step": state[2], "id": f"h{source}:{state[1]}:{state[2]}",
                                       "kind": "step", "status": "failed"})
                    events.append(event)
            else:
                more |= bool(file.peek(1))
    encoded = json.dumps([directory.name, states], separators=(",", ":")).encode()
    return events, base64.urlsafe_b64encode(encoded).decode(), more


def trace_page(directory, cursor=None):
    input_path = inside(directory, "input.json")
    status_path = inside(directory, "status.json")
    if not status_path.is_file() or not input_path.is_file():
        raise InputError("未找到运行。", "Run not found.", 404)
    status = status_at(directory)
    inputs = read_json(input_path)
    meta = _compact(parentRunId=inputs.get("parentRunId"), workspace_name=directory.parent.parent.name, locale=inputs.get("locale"),
                    started_at=input_path.stat().st_mtime, model=(inputs.get("coordinator") or {}).get("model"),
                    ended_at=status_path.stat().st_mtime if status["status"] != "running" else None)
    events, cursor, more = _page(directory, cursor)
    return dict(run=status, meta=meta, events=events, cursor=cursor, more=more)


def run_history(root):
    items = []
    for entry in inside(root, "runs").glob("run-*"):
        if not RUN_ID.fullmatch(entry.name):
            continue
        directory = run_dir(root, entry.name)
        input_path = inside(directory, "input.json")
        if inside(directory, "status.json").is_file() and input_path.is_file():
            result = status_at(directory)
            items.append(_compact(id=directory.name, status=result["status"], started_at=input_path.stat().st_mtime,
                                  parentRunId=read_json(input_path).get("parentRunId")))
    return sorted(items, key=lambda r: r["started_at"], reverse=True)
