"""Read-only evidence for restarting DSH and recognizing an actual BIM handoff.

These checks never execute a tool, choose a candidate, or repair a delivery.
Native receipts establish which operations finished; hashes establish what is
still on disk. Neither a process exit code nor a remembered proof is sufficient.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import time

from src.agent.runtime_tools import repeatability_for
from .handoff import attest_local_finish

_EXCLUDED = {".harness_tmp", "__pycache__"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BIM_PREFIX = "mcp__sim_bim__"


def _unlinked(path):
    """Reject links, including Windows directory junctions, before resolving."""
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("linked_evidence_path")
    return path


def _root(run):
    root = _unlinked(run).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("run_is_not_a_directory")
    return root


def _relative(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("invalid_relative_path")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
        raise ValueError("invalid_relative_path")
    return path


def work_snapshot(run):
    """Hash every durable run file, excluding only runtime/cache directories."""
    root = _root(run)
    result = {}

    def visit(directory):
        for path in sorted(directory.iterdir()):
            _unlinked(path)
            if not path.resolve(strict=True).is_relative_to(root):
                raise ValueError("evidence_path_escaped_run")
            mode = path.stat().st_mode
            if stat.S_ISDIR(mode):
                if path.name not in _EXCLUDED:
                    visit(path)
            elif stat.S_ISREG(mode):
                result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                raise ValueError("unsupported_evidence_file")

    visit(root)
    return result


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


_DECODER = json.JSONDecoder(object_pairs_hook=_object)


def _native_events(paths):
    if isinstance(paths, (str, Path)):
        paths = [paths]
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ValueError("native_events_missing")
    rows = []
    for path in paths:
        raw = _unlinked(path).read_bytes()
        attempt = []
        for line in raw.decode("utf-8-sig").splitlines():
            if not line.strip():
                continue
            row = json.loads(line, object_pairs_hook=_object)
            if not isinstance(row, dict):
                raise ValueError("native_event_not_an_object")
            attempt.append(row)
        if not attempt:
            raise ValueError("native_events_empty")
        rows.extend(attempt)
    return rows


def _tool_kind(name):
    if name == "mcp__sim_bim_context__read_context_image":
        return "read_context_image", "read_only"
    if name in {"mcp__sim_bim_workers__read_worker", "mcp__sim_bim_workers__worker_state"}:
        return name.split("__")[-1], "read_only"
    if name == "mcp__sim_bim_workers__delegate_workers":
        return "delegate_workers", "idempotent_write"
    if not isinstance(name, str):
        raise ValueError("unknown_native_tool")
    bare = name.removeprefix(_BIM_PREFIX)
    return bare, repeatability_for(bare)


def _phase(row):
    return row.get("phase") if row.get("type") == "status" else row.get("type")


def _closed_tools(rows):
    calls, ordered, provider_ids = {}, [], set()
    for index, row in enumerate(rows):
        phase = _phase(row)
        if phase not in {"tool_call", "tool_result"}:
            continue
        cid = row.get("callId")
        if not isinstance(cid, str) or not cid:
            raise ValueError("native_tool_id_missing")
        if phase == "tool_call":
            provider_id = _provider_call_id(cid)
            if cid in calls or provider_id in provider_ids:
                raise ValueError("duplicate_native_tool_call")
            name, repeatability = _tool_kind(row.get("tool"))
            entry = {"call": row, "name": name, "repeatability": repeatability,
                     "index": index, "result": None}
            calls[cid] = entry
            provider_ids.add(provider_id)
            ordered.append(entry)
        else:
            if cid not in calls:
                raise ValueError("orphan_native_tool_result")
            if calls[cid]["result"] is not None:
                raise ValueError("duplicate_native_tool_result")
            if row.get("status") not in {"completed", "error", "failed"} or "result" not in row:
                raise ValueError("native_tool_result_incomplete")
            calls[cid]["result"] = row
            calls[cid]["result_index"] = index
    if any(entry["result"] is None for entry in ordered):
        raise ValueError("native_tool_result_missing")
    return ordered


def _aborted(reason):
    if isinstance(reason, str):
        token = re.sub(r"[^a-z]", "", reason.lower())
        return any(term in token for term in
                   ("abort", "cancel", "interrupt", "sigint", "sigterm", "killed"))
    if isinstance(reason, dict):
        return any(_aborted(value) for key, value in reason.items()
                   if key in {"kind", "code", "name", "type", "reason", "error", "cause"})
    return False


def verify_recovery_state(run, checkpoint_snapshot, events_path):
    """Permit recovery only with closed receipts and an unchanged checkpoint.

    A caller deciding to start a *new* turn may separately checkpoint completed
    work. This function never replaces the checkpoint of a pending request.
    """
    try:
        rows = _native_events(events_path)
        tools = _closed_tools(rows)
        ends = [(i, row) for i, row in enumerate(rows) if _phase(row) == "turn_end"]
        if not ends:
            return {"safe_to_resume": False, "reason": "native_turn_end_missing", "closed_tools": True}
        end_index, end = ends[-1]
        if any(_phase(row) in {"step_start", "tool_call", "tool_result"} for row in rows[end_index + 1:]):
            return {"safe_to_resume": False, "reason": "native_turn_not_closed", "closed_tools": True}
        if _aborted(end.get("reason")):
            return {"safe_to_resume": False, "reason": "native_turn_aborted", "closed_tools": True}
        if not isinstance(checkpoint_snapshot, dict):
            raise ValueError("invalid_work_checkpoint")
        for name, digest in checkpoint_snapshot.items():
            _relative(name)
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ValueError("invalid_work_checkpoint")
        current = work_snapshot(run)
        if current != checkpoint_snapshot:
            return {"safe_to_resume": False, "reason": "work_snapshot_changed", "closed_tools": True}
        return {"safe_to_resume": True, "reason": "verified_unchanged_closed_tools",
                "closed_tools": True, "tool_count": len(tools), "file_count": len(current)}
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        return {"safe_to_resume": False, "reason": "invalid_recovery_evidence",
                "closed_tools": False, "error_type": type(error).__name__}


def _partial_object(text):
    """Read only complete top-level fields of a truncated JSON object prefix."""
    text = text.lstrip()
    if not text.startswith("{"):
        return {}
    values, offset = {}, 1
    while True:
        offset += len(text[offset:]) - len(text[offset:].lstrip())
        try:
            key, offset = _DECODER.raw_decode(text, offset)
            if not isinstance(key, str) or key in values:
                raise ValueError("ambiguous_truncated_result")
            offset += len(text[offset:]) - len(text[offset:].lstrip())
            if text[offset:offset + 1] != ":":
                break
            offset += 1
            offset += len(text[offset:]) - len(text[offset:].lstrip())
            value, offset = _DECODER.raw_decode(text, offset)
        except json.JSONDecodeError:
            break
        values[key] = value
        offset += len(text[offset:]) - len(text[offset:].lstrip())
        if text[offset:offset + 1] != ",":
            break
        offset += 1
    return values


def _finish_result(run, receipt):
    value = receipt["result"]
    if isinstance(value, str):
        try:
            value, _ = _DECODER.raw_decode(value.lstrip())
        except json.JSONDecodeError:
            if not receipt.get("truncated") or not value.lstrip().startswith("{"):
                raise ValueError("invalid_finish_result")
            value = _partial_object(value)
    if not isinstance(value, dict) or value.get("isError"):
        raise ValueError("invalid_finish_result")
    if "details_file" in value or "details_sha256" in value:
        name, digest = value.get("details_file"), value.get("details_sha256")
        relative = _relative(name)
        if len(relative.parts) != 2 or relative.parts[0] != "tool_reports":
            raise ValueError("invalid_finish_report_path")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError("invalid_finish_report_hash")
        path = _unlinked(Path(run) / name)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("finish_report_hash_mismatch")
        saved = json.loads(raw, object_pairs_hook=_object)
        if not isinstance(saved, dict) or saved.get("isError"):
            raise ValueError("invalid_finish_report")
        # Resolve exactly once. Never let a saved report redirect to another.
        return ({key: item for key, item in {**value, **saved}.items()
                 if key not in {"details_file", "details_sha256"}}, False)
    return value, bool(receipt.get("truncated"))


def _provider_call_id(cid):
    left, separator, right = cid.partition("|")
    return left if separator and left.startswith("call_") and right.startswith("fc_") and "|" not in right else cid


def attest_host_finish(run, events_path, minimum_floors=1, recorded_proof=None):
    """Re-attest the last actual writing tool across ordered native attempts.

    A relay proof is usable only for the *same* completed, truncated native
    finish result when its full report reference was lost from the display.
    Every path still passes fresh domain attestation against the current files.
    """
    try:
        tools = _closed_tools(_native_events(events_path))
        writes = [entry for entry in tools if entry["repeatability"] != "read_only"]
        if not writes or writes[-1]["name"] != "finish_bim":
            return None
        finish = writes[-1]
        # A write started earlier but completed after finish can invalidate it.
        if any(entry["result_index"] > finish["result_index"] for entry in writes[:-1]):
            return None
        receipt, call = finish["result"], finish["call"]
        if receipt.get("status") != "completed":
            return None
        arguments = call.get("input")
        if isinstance(arguments, str):
            arguments = json.loads(arguments, object_pairs_hook=_object)
        if not isinstance(arguments, dict):
            return None
        candidate = arguments.get("candidate")
        result, can_use_recorded = _finish_result(run, receipt)
        if "candidate" in result and result["candidate"] != candidate:
            return None
        if (not result.get("delivery_artifact_sha256") and can_use_recorded
                and isinstance(recorded_proof, dict)
                and recorded_proof.get("call_id") in {call["callId"], _provider_call_id(call["callId"])}
                and recorded_proof.get("candidate") == candidate
                and ("source_model_sha256" not in result or result["source_model_sha256"] == recorded_proof.get("source_model_sha256"))
                and recorded_proof.get("tool_name") == "finish_bim"
                and recorded_proof.get("origin") == "domain_tool"):
            result = {"candidate": candidate,
                      "source_model_sha256": recorded_proof.get("source_model_sha256"),
                      "delivery_artifact_sha256": recorded_proof.get("artifact_sha256")}
        before = work_snapshot(run)
        proof = attest_local_finish(run, candidate, result, minimum_floors)
        if not proof or work_snapshot(run) != before:
            return None
        return {**proof, "call_id": _provider_call_id(call["callId"]),
                "native_call_id": call["callId"], "observed_in": "DSH native tool_call/tool_result",
                "verified_epoch": time.time()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        return None
