"""Drain opted-in observer tasks while DSH is still connected at ordinary finish."""
import json
import time

from src.utils import file_lock


def observer_closeout(case, deadline):
    directory = case / "private/workers"
    path = directory / "worker_state.json"
    if not path.exists():
        return {"status": "unused", "in_flight": []}
    while True:
        state = json.loads(path.read_bytes())
        active = state["background_in_flight"]
        if not active:
            return {"status": "settled", "in_flight": [],
                    "tasks": {row["task_id"]: row["status"] for row in state["tasks"]}}
        if deadline is not None and time.time() >= deadline:
            return {"status": "deadline", "in_flight": active}
        # A dead MCP cannot settle a pending task. Probe the existing journal
        # writer lock so an interrupted case does not wait for a stale PID.
        with (directory / "writer.lock").open("rb") as lock:
            try:
                file_lock.flock(lock, file_lock.LOCK_EX | file_lock.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                file_lock.flock(lock, file_lock.LOCK_UN)
                latest = json.loads(path.read_bytes())
                if latest["background_in_flight"]:
                    return {"status": "interrupted", "in_flight": latest["background_in_flight"]}
                continue
        time.sleep(0.2)
