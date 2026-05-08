from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Any


class ResearchBus:
    """Pub/sub thread-safe entre o pesquisador (sync, em thread) e o WebSocket (async)."""

    def __init__(self, history: int = 500, max_logs: int = 250, max_results: int = 80) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[asyncio.Queue] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self.history: deque = deque(maxlen=history)
        self.max_logs = max_logs
        self.max_results = max_results
        self.state: dict[str, Any] = self._empty_state()

    def _empty_state(self) -> dict[str, Any]:
        return {
            "status": "idle",
            "mode": "grid",
            "ran_at": None,
            "started_ts": None,
            "duration_s": 0.0,
            "totals": {"tested": 0, "approved": 0, "pruned": 0, "errors": 0},
            "total_jobs": 0,
            "current_jobs": [],
            "params": {},
            "results": [],
            "best": None,
            "logs": [],
            # discovery state
            "discovery": {
                "active": False,
                "round": 0,
                "stats": {
                    "candidates_generated": 0,
                    "filtered_in": 0,
                    "filtered_out": 0,
                    "validated": 0,
                    "approved": 0,
                    "rejected": 0,
                    "errors": 0,
                },
                "templates": [],
                "risk_profiles": [],
                "fee_levels": [],
                "candidates_per_round": 0,
                "current_candidates": [],
                "validating": [],
                "recent_decisions": [],
                "catalog_stats": {
                    "rounds": 0, "total": 0, "approved": 0, "rejected": 0,
                    "per_template": {},
                },
                "approved_top": [],
                "scenarios_progress": {},
            },
        }

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2048)
        with self._lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                pass

    def emit(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._update_state(event)
            self.history.append(event)
            subs = list(self._subscribers)
            loop = self._loop
        if loop is None:
            return
        for queue in subs:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                continue

    def snapshot(self) -> dict[str, Any]:
        import copy
        with self._lock:
            return copy.deepcopy(self.state)

    def _update_state(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "run_started":
            self.state = self._empty_state()
            self.state.update(
                status="running",
                ran_at=event.get("ran_at"),
                started_ts=event.get("ts"),
                total_jobs=event.get("total_jobs", 0),
                params=event.get("params", {}),
            )
        elif kind == "job_started":
            self.state["current_jobs"].append({
                "strategy": event.get("strategy"),
                "asset": event.get("asset"),
                "params_str": event.get("params", ""),
                "started_ts": event.get("ts"),
            })
        elif kind == "job_done":
            res = event.get("result", {})
            self._remove_current_job(res)
            self.state["totals"]["tested"] += 1
            if res.get("error"):
                self.state["totals"]["errors"] += 1
            elif res.get("passed"):
                self.state["totals"]["approved"] += 1
            else:
                self.state["totals"]["pruned"] += 1
            self._merge_result(res)
        elif kind == "log":
            self.state["logs"].append({
                "ts": event.get("ts"),
                "level": event.get("level", "info"),
                "msg": event.get("msg", ""),
            })
            if len(self.state["logs"]) > self.max_logs:
                self.state["logs"] = self.state["logs"][-self.max_logs:]
        elif kind == "run_finished":
            self.state["status"] = "finished"
            self.state["duration_s"] = event.get("duration_s", self.state["duration_s"])
            self.state["current_jobs"] = []
        elif kind == "run_failed":
            self.state["status"] = "error"
            self.state["current_jobs"] = []
        elif kind == "run_stopped":
            self.state["status"] = "stopped"
            self.state["current_jobs"] = []

        # ---------- DISCOVERY EVENTS ----------
        elif kind == "discovery_started":
            disc = self.state["discovery"]
            disc["active"] = True
            disc["round"] = 0
            disc["templates"] = event.get("templates", [])
            disc["risk_profiles"] = event.get("risk_profiles", [])
            disc["fee_levels"] = event.get("fee_levels", [])
            disc["candidates_per_round"] = event.get("candidates_per_round", 0)
            disc["current_candidates"] = []
            disc["validating"] = []
            disc["recent_decisions"] = []
            disc["scenarios_progress"] = {}
            self.state["mode"] = "discovery"
            self.state["status"] = "running"
            self.state["ran_at"] = event.get("ran_at")
            self.state["started_ts"] = event.get("ts")
        elif kind == "discovery_finished":
            disc = self.state["discovery"]
            disc["active"] = False
            disc["stats"] = event.get("stats", disc["stats"])
            disc["catalog_stats"] = event.get("catalog_stats", disc["catalog_stats"])
            self.state["status"] = "stopped" if event.get("stopped_by_user") else "finished"
        elif kind == "round_started":
            self.state["discovery"]["round"] = event.get("round", 0)
            self.state["discovery"]["current_candidates"] = []
            self.state["discovery"]["validating"] = []
            self.state["discovery"]["scenarios_progress"] = {}
        elif kind == "round_finished":
            self.state["discovery"]["stats"] = event.get("stats", self.state["discovery"]["stats"])
            self.state["discovery"]["catalog_stats"] = event.get(
                "catalog_stats", self.state["discovery"]["catalog_stats"]
            )
        elif kind == "candidate_generated":
            disc = self.state["discovery"]
            disc["stats"]["candidates_generated"] = disc["stats"].get("candidates_generated", 0) + 1
            cand = {
                "id": event.get("id"),
                "template": event.get("template"),
                "params": event.get("params"),
                "risk": event.get("risk"),
                "label": event.get("label"),
                "ts": event.get("ts"),
                "stage": "generated",
            }
            disc["current_candidates"].append(cand)
            if len(disc["current_candidates"]) > 30:
                disc["current_candidates"] = disc["current_candidates"][-30:]
        elif kind == "candidate_quick":
            disc = self.state["discovery"]
            target = event.get("id")
            for cand in disc["current_candidates"]:
                if cand["id"] == target:
                    cand["stage"] = "quick_passed" if event.get("passed_quick") else "quick_failed"
                    cand["quick"] = event.get("metric")
                    break
            if event.get("passed_quick"):
                disc["stats"]["filtered_in"] = disc["stats"].get("filtered_in", 0) + 1
            else:
                disc["stats"]["filtered_out"] = disc["stats"].get("filtered_out", 0) + 1
        elif kind == "validation_started":
            disc = self.state["discovery"]
            disc["validating"].append({
                "id": event.get("id"),
                "template": event.get("template"),
                "scenarios_planned": event.get("scenarios_planned", 0),
                "scenarios_done": 0,
                "ts": event.get("ts"),
            })
            disc["scenarios_progress"][event.get("id")] = []
        elif kind == "scenario_done":
            disc = self.state["discovery"]
            sid = event.get("id")
            metric = event.get("metric") or {}
            for entry in disc["validating"]:
                if entry["id"] == sid:
                    entry["scenarios_done"] = entry.get("scenarios_done", 0) + 1
                    break
            progress = disc["scenarios_progress"].setdefault(sid, [])
            progress.append(metric)
        elif kind == "validation_done":
            disc = self.state["discovery"]
            sid = event.get("id")
            disc["validating"] = [v for v in disc["validating"] if v["id"] != sid]
            disc["scenarios_progress"].pop(sid, None)
            decision = {
                "id": sid,
                "approved": bool(event.get("approved")),
                "score": event.get("score", 0.0),
                "summary": event.get("summary", {}),
                "criteria_checks": event.get("criteria_checks", []),
                "rejection_reason": event.get("rejection_reason"),
                "entry": event.get("entry"),
                "ts": event.get("ts"),
            }
            disc["recent_decisions"].insert(0, decision)
            if len(disc["recent_decisions"]) > 30:
                disc["recent_decisions"] = disc["recent_decisions"][:30]
            if event.get("approved"):
                disc["stats"]["approved"] = disc["stats"].get("approved", 0) + 1
            else:
                disc["stats"]["rejected"] = disc["stats"].get("rejected", 0) + 1
            disc["stats"]["validated"] = disc["stats"].get("validated", 0) + 1
            for cand in disc["current_candidates"]:
                if cand["id"] == sid:
                    cand["stage"] = "approved" if event.get("approved") else "rejected"
                    cand["score"] = event.get("score", 0.0)
                    break
        elif kind == "candidate_rejected":
            disc = self.state["discovery"]
            sid = event.get("id")
            for cand in disc["current_candidates"]:
                if cand["id"] == sid:
                    cand["stage"] = "rejected"
                    cand["reason"] = event.get("reason")
                    break
        elif kind == "approved_top":
            self.state["discovery"]["approved_top"] = event.get("entries", [])
        elif kind == "catalog_stats":
            self.state["discovery"]["catalog_stats"] = event.get("stats", self.state["discovery"]["catalog_stats"])

    def _remove_current_job(self, res: dict[str, Any]) -> None:
        key = (res.get("strategy"), res.get("asset"), res.get("params_str", ""))
        for index, job in enumerate(self.state["current_jobs"]):
            existing = (job.get("strategy"), job.get("asset"), job.get("params_str", ""))
            if existing == key:
                self.state["current_jobs"].pop(index)
                return

    def _merge_result(self, result: dict[str, Any]) -> None:
        results = self.state["results"]
        key = (result.get("strategy"), result.get("asset"), result.get("params_str", ""))
        replaced = False
        for index, item in enumerate(results):
            existing = (item.get("strategy"), item.get("asset"), item.get("params_str", ""))
            if existing == key:
                results[index] = result
                replaced = True
                break
        if not replaced:
            results.append(result)
        results.sort(key=lambda item: item.get("score", 0.0), reverse=True)
        self.state["results"] = results[: self.max_results]
        self.state["best"] = self.state["results"][0] if self.state["results"] else None
