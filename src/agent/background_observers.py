"""Optional nonblocking dispatch in the DSH observer MCP's single event loop."""
import asyncio

from .runtime_mesh_workers import _envelope


class BackgroundObservers:
    def __init__(self, workers):
        self.workers = workers
        self.pending = {}
        self.last_attempt = {}

    def snapshot(self):
        state = self.workers.state()
        for row in state["tasks"]:
            key = row["task_id"]
            if key in self.pending:
                row["status"] = "in_flight"
            elif key in self.last_attempt and row["status"] == "incomplete":
                row.update(self.last_attempt[key])
        return {**state, "background_in_flight": sorted(self.pending)}

    def publish(self):
        self.workers.store.write_json("worker_state.json", self.snapshot())

    async def _run(self, task, admission):
        key = task["task_id"]
        try:
            result = await self.workers._dispatch(task, admission=admission)
            self.last_attempt[key] = {k: result[k] for k in ("status", "reason", "error_type", "recovery") if k in result}
            return result
        finally:
            self.pending.pop(key, None)
            self.publish()

    def _submit(self, task):
        key = task["task_id"]
        child = self.workers._child(key)
        try:
            record = self.workers._admit_task(child, task)
            if "packet" not in record:
                return record
            if self.workers._anchored(child, "mesh-worker-outcome") is not None:
                return self.workers._read(key)[0]
            if key not in self.pending:
                self.last_attempt.pop(key, None)
                self.pending[key] = asyncio.create_task(self._run(task, (child, record)))
        except (ValueError, FileNotFoundError) as exc:
            return {"task_id": key, "status": "rejected", "reason": str(exc)}
        return {"task_id": key, "status": "in_flight", "source_basis": record["packet"].get("source_snapshot")}

    async def call_tool(self, name, args):
        # MCP validates the enabled public schema before entering this callback.
        if name == "delegate_workers" and not args.get("wait", True):
            ids = [task["task_id"] for task in args["tasks"]]
            if len(set(ids)) != len(ids) or self.workers.store.task_id in ids:
                return _envelope({"status": "rejected", "reason": "task IDs must be unique and distinct from the coordinator"}, error=True)
            try:
                results = [self._submit(task) for task in args["tasks"]]
            finally:
                self.publish()
            return _envelope({"status": "submitted", "results": results})
        if name == "read_worker":
            key = args["task_id"]
            job = self.pending.get(key)
            if job is not None:
                if not args.get("wait"):
                    return _envelope({"task_id": key, "status": "in_flight", "result": None})
                await asyncio.shield(job)
        if name == "worker_state":
            return _envelope(self.snapshot())
        raw = await self.workers.call_tool(name, {k: v for k, v in args.items() if k != "wait"})
        if name == "read_worker" and raw["structuredContent"].get("status") == "incomplete":
            attempt = self.last_attempt.get(args["task_id"])
            if attempt:
                raw["structuredContent"]["last_background_attempt"] = attempt
        return raw

    async def drain(self):
        if self.pending:
            await asyncio.gather(*(asyncio.shield(job) for job in list(self.pending.values())))
