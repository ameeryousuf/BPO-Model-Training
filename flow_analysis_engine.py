"""Flow analysis engine: graph construction, path enumeration, and process metrics."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TaskNode:
    task_id: int
    order: int
    name: str
    proc_time: float
    wait_time: float
    rework_time: float
    is_subprocess_slot: bool
    activity_type: str = "basic"
    value_classification: str = "VA"
    is_periodic: bool = False
    is_batch: bool = False
    job_tasks: list = field(default_factory=list)
    next_task_id: Optional[int] = None
    next_gateway_id: Optional[int] = None
    connects_to_end: bool = False


@dataclass
class GatewayBranch:
    branch_id: int
    gateway_pk_id: int
    condition: str
    probability: float
    target_task_id: Optional[int]
    target_gateway_id: Optional[int]
    connect_to_end: bool
    end_event_name: Optional[str]
    is_default: bool = False


@dataclass
class GatewayNode:
    gateway_pk_id: int
    gateway_type: str
    name: str
    after_task_id: Optional[int]
    after_gateway_id: Optional[int]
    branches: list
    converge_at_task_id: Optional[int] = None
    converge_at_gateway_id: Optional[int] = None
    converge_to_end: bool = False


class GraphBuilder:
    def __init__(self, record: dict):
        self.record = record
        self.tasks: dict[int, TaskNode] = {}
        self.gateways: dict[int, GatewayNode] = {}
        self._build()

    def _build(self):
        for pt in self.record.get("process_task", []):
            task = pt.get("task") or {}
            if pt.get("task_id") is None and pt.get("child_process_id") is not None:
                node = TaskNode(
                    task_id=-abs(pt["child_process_id"]), order=pt["order"],
                    name=f"[Sub-process {pt['child_process_id']}]",
                    proc_time=0, wait_time=0, rework_time=0, is_subprocess_slot=True,
                )
                self.tasks[node.task_id] = node
                continue

            node = TaskNode(
                task_id=task["task_id"], order=pt["order"], name=task.get("task_name", ""),
                proc_time=task.get("expected_process_time") or 0,
                wait_time=task.get("expected_waiting_time") or 0,
                rework_time=task.get("expected_rework_time") or 0,
                is_subprocess_slot=False,
                activity_type=task.get("_activity_type", "basic"),
                value_classification=pt.get("value_classification", "VA"),
                is_periodic=task.get("_is_periodic", False),
                is_batch=task.get("_is_batch", False),
                job_tasks=task.get("jobTasks", []),
                next_task_id=task.get("_next_task_id"),
                next_gateway_id=task.get("_next_gateway_id"),
                connects_to_end=bool(task.get("_connects_to_end")),
            )
            self.tasks[node.task_id] = node

        for gw in self.record.get("gateways", []):
            branches = [
                GatewayBranch(
                    branch_id=b.get("id", i), gateway_pk_id=gw["gateway_pk_id"],
                    condition=b.get("condition") or "", probability=b.get("probability") or 0.0,
                    target_task_id=b.get("target_task_id"), target_gateway_id=b.get("target_gateway_id"),
                    connect_to_end=bool(b.get("connect_to_end")) or b.get("end_event_name") is not None,
                    end_event_name=b.get("end_event_name"), is_default=bool(b.get("is_default")),
                )
                for i, b in enumerate(gw.get("branches", []))
            ]
            self.gateways[gw["gateway_pk_id"]] = GatewayNode(
                gateway_pk_id=gw["gateway_pk_id"], gateway_type=gw.get("gateway_type", "EXCLUSIVE"),
                name=gw.get("name", ""), after_task_id=gw.get("after_task_id"),
                after_gateway_id=gw.get("after_gateway_id"), branches=branches,
                converge_at_task_id=gw.get("converge_at_task_id"),
                converge_at_gateway_id=gw.get("converge_at_gateway_id"),
                converge_to_end=bool(gw.get("converge_to_end")),
            )

    def find_start(self):
        targeted = {b.target_gateway_id for gw in self.gateways.values() for b in gw.branches
                    if b.target_gateway_id is not None}
        targeted |= {gw.after_gateway_id for gw in self.gateways.values() if gw.after_gateway_id is not None}

        for gw in self.gateways.values():
            if gw.after_task_id is None and gw.after_gateway_id is None and gw.gateway_pk_id not in targeted:
                return ("gateway", gw.gateway_pk_id)

        ordered = sorted(self.tasks.values(), key=lambda t: t.order)
        if not ordered:
            raise ValueError("Process has no tasks and no qualifying start gateway.")
        return ("task", ordered[0].task_id)

    def gateway_after_task(self, task_id: int) -> Optional[GatewayNode]:
        return next((gw for gw in self.gateways.values() if gw.after_task_id == task_id), None)

    def real_next(self, task_id: int):
        t = self.tasks.get(task_id)
        if t is None:
            return None
        if t.next_task_id is not None:
            return ("task", t.next_task_id)
        if t.next_gateway_id is not None:
            return ("gateway", t.next_gateway_id)
        if t.connects_to_end:
            return ("end", None)
        return None


class PathEnumerator:
    def __init__(self, gb: GraphBuilder):
        self.gb = gb

    def enumerate_structured_paths(self) -> list[dict]:
        raw_paths: list[dict] = []
        self._expand(self.gb.find_start(), [], 1.0, raw_paths, 0, None)
        total = sum(p["probability"] for p in raw_paths) or 1.0
        for p in raw_paths:
            p["probability"] /= total
        return raw_paths

    def _resolve_target(self, target_task_id, target_gateway_id, connect_to_end, end_event_name):
        if target_task_id is not None:
            return ("task", target_task_id)
        if target_gateway_id is not None:
            return ("gateway", target_gateway_id)
        return ("end", end_event_name or "End")

    def _convergence_ref(self, gw: GatewayNode):
        if gw.converge_at_task_id is not None:
            return ("task", gw.converge_at_task_id)
        if gw.converge_at_gateway_id is not None:
            return ("gateway", gw.converge_at_gateway_id)
        return ("end", "End")

    def _expand(self, node_ref, segments, probability, raw_paths, depth, stop_ref):
        if depth > 200:
            raise RuntimeError("Path enumeration exceeded max depth (possible cycle).")
        if stop_ref is not None and node_ref == stop_ref:
            return segments

        kind, ident = node_ref

        if kind == "end":
            if stop_ref is not None:
                return segments
            raw_paths.append({"segments": segments, "probability": probability})
            return None

        if kind == "task":
            segments = segments + [{"type": "task", "task_id": ident}]
            gw = self.gb.gateway_after_task(ident)
            if gw is not None:
                return self._enter_gateway(gw, segments, probability, raw_paths, depth, stop_ref)
            nxt = self.gb.real_next(ident)
            if nxt is not None and nxt[0] != "end":
                return self._expand(nxt, segments, probability, raw_paths, depth + 1, stop_ref)
            if stop_ref is not None:
                return segments
            raw_paths.append({"segments": segments, "probability": probability})
            return None

        if kind == "gateway":
            return self._enter_gateway(self.gb.gateways[ident], segments, probability, raw_paths, depth, stop_ref)

        raise ValueError(f"Unknown node kind: {kind}")

    def _enter_gateway(self, gw: GatewayNode, segments, probability, raw_paths, depth, stop_ref):
        if gw.gateway_type in ("EXCLUSIVE", "EVENT_BASED"):
            if stop_ref is not None:
                raise NotImplementedError(
                    f"Nested {gw.gateway_type} gateway inside a parallel/inclusive branch is unsupported."
                )
            total = sum(b.probability for b in gw.branches) or 1.0
            n = len(gw.branches) or 1
            for b in gw.branches:
                p = (b.probability / total) if total else (1.0 / n)
                target = self._resolve_target(b.target_task_id, b.target_gateway_id, b.connect_to_end, b.end_event_name)
                labeled = segments + [{"type": "branch", "gateway_pk_id": gw.gateway_pk_id, "condition": b.condition}]
                self._expand(target, labeled, probability * p, raw_paths, depth + 1, stop_ref)
            return None

        if gw.gateway_type == "PARALLEL":
            convergence = self._convergence_ref(gw)
            branches = []
            for b in gw.branches:
                target = self._resolve_target(b.target_task_id, b.target_gateway_id, b.connect_to_end, b.end_event_name)
                sub = self._expand(target, [], 1.0, raw_paths, depth + 1, stop_ref=convergence)
                branches.append(sub or [])
            merged = segments + [{"type": "parallel", "gateway_pk_id": gw.gateway_pk_id, "branches": branches}]
            return self._expand(convergence, merged, probability, raw_paths, depth + 1, stop_ref)

        if gw.gateway_type == "INCLUSIVE":
            n = len(gw.branches)
            convergence = self._convergence_ref(gw)
            for mask in range(1, 2 ** n):
                active = {i for i in range(n) if (mask >> i) & 1}
                subset_prob = 1.0
                branches = []
                for i, b in enumerate(gw.branches):
                    if i in active:
                        subset_prob *= b.probability
                        target = self._resolve_target(b.target_task_id, b.target_gateway_id,
                                                        b.connect_to_end, b.end_event_name)
                        sub = self._expand(target, [], 1.0, raw_paths, depth + 1, stop_ref=convergence)
                        branches.append(sub or [])
                    else:
                        subset_prob *= (1 - b.probability)
                merged = segments + [{"type": "inclusive_subset", "gateway_pk_id": gw.gateway_pk_id, "branches": branches}]
                self._expand(convergence, merged, probability * subset_prob, raw_paths, depth + 1, stop_ref)
            return None

        raise ValueError(f"Unknown gateway type: {gw.gateway_type}")

    def segment_list_metrics(self, seg_list, tasks: dict[int, TaskNode]):
        task_ids: list[int] = []
        pt = wt = rt = cost = 0.0

        for seg in seg_list:
            if seg["type"] == "task":
                t = tasks[seg["task_id"]]
                proc_hours = (t.proc_time + t.rework_time) / 60.0
                c = sum(
                    proc_hours * ((jt.get("job") or {}).get("hourlyRate", 0) or 0)
                    * ((jt.get("time_allocation_percentage") or 0) / 100.0)
                    for jt in t.job_tasks
                )
                pt += t.proc_time
                wt += t.wait_time
                rt += t.rework_time
                cost += c
                task_ids.append(seg["task_id"])
            elif seg["type"] == "branch":
                continue
            elif seg["type"] in ("parallel", "inclusive_subset"):
                results = [self.segment_list_metrics(b, tasks) for b in seg["branches"]]
                for ids, *_ in results:
                    task_ids.extend(ids)
                durations = [bp + bw + br for _, bp, bw, br, _ in results]
                if durations:
                    i = durations.index(max(durations))
                    _, bp, bw, br, _ = results[i]
                    pt += bp
                    wt += bw
                    rt += br
                cost += sum(bc for *_, bc in results)
            else:
                raise ValueError(f"Unknown segment type: {seg['type']}")

        return task_ids, pt, wt, rt, cost


class FlowAnalysisService:
    def __init__(self, record: dict, currency: str = "USD", rng_seed: Optional[int] = None):
        self.record = record
        self.currency = currency
        self.gb = GraphBuilder(record)
        self.pe = PathEnumerator(self.gb)
        self._rng = random.Random(rng_seed)

    def analyze(self, n_flexibility_substitutable: Optional[int] = None,
                n_parallel_tasks: Optional[int] = None, mc_iterations: int = 10_000) -> dict:
        raw_paths = self.pe.enumerate_structured_paths()
        if not raw_paths:
            raise ValueError("No paths could be enumerated for this process.")

        paths = []
        for p in raw_paths:
            ids, pt, wt, rt, cost = self.pe.segment_list_metrics(p["segments"], self.gb.tasks)
            paths.append({"probability": p["probability"], "task_ids": ids,
                          "pt": pt, "wt": wt, "rt": rt, "cost": cost, "duration": pt + wt + rt})

        e_ct = sum(m["probability"] * m["duration"] for m in paths)
        e_pt = sum(m["probability"] * m["pt"] for m in paths)
        e_wt = sum(m["probability"] * m["wt"] for m in paths)
        e_rt = sum(m["probability"] * m["rt"] for m in paths)
        e_cost = sum(m["probability"] * m["cost"] for m in paths)
        cte = (e_pt / e_ct * 100) if e_ct else 0.0

        e_nva = self._expected_nva_time(paths)
        rework_share = (e_rt / (e_pt + e_rt)) if (e_pt + e_rt) else 0.0
        nva_share = (e_nva / (e_pt + e_rt)) if (e_pt + e_rt) else 0.0
        quality = 100 * (1 - min(1, 0.5 * rework_share + 0.5 * nva_share))

        n_tasks = len([t for t in self.gb.tasks.values() if not t.is_subprocess_slot])
        n_parallel = n_parallel_tasks if n_parallel_tasks is not None else self._count_parallel_tasks()
        n_sub = n_flexibility_substitutable if n_flexibility_substitutable is not None else self._count_substitutable()
        flexibility = 100 * min(1, 0.6 * (n_parallel / n_tasks if n_tasks else 0)
                                  + 0.4 * (n_sub / n_tasks if n_tasks else 0))

        return {
            "process": {
                "process_id": self.record.get("process_id"),
                "process_name": self.record.get("process_name"),
                "process_code": self.record.get("process_code"),
            },
            "method": "flow_analysis_structured",
            "currency": self.currency,
            "cycle_time_minutes": round(e_ct, 2),
            "processing_time_minutes": round(e_pt, 2),
            "waiting_time_minutes": round(e_wt, 2),
            "rework_time_minutes": round(e_rt, 2),
            "cycle_time_efficiency_percent": round(cte, 1),
            "labor_cost_per_case": round(e_cost, 2),
            "quality_score": round(quality, 1),
            "flexibility_score": round(flexibility, 1),
            "data_quality": {"task_count": n_tasks, "paths_evaluated": len(paths)},
            "simulation": self._monte_carlo(paths, mc_iterations),
            "_paths": paths,
        }

    def _expected_nva_time(self, paths):
        nva_ids = {tid for tid, t in self.gb.tasks.items() if t.value_classification == "NVA"}
        if not nva_ids:
            return 0.0
        return sum(
            m["probability"] * sum(self.gb.tasks[tid].proc_time + self.gb.tasks[tid].rework_time
                                    for tid in m["task_ids"] if tid in nva_ids)
            for m in paths
        )

    def _count_parallel_tasks(self) -> int:
        ids = set()
        for gw in self.gb.gateways.values():
            if gw.gateway_type != "PARALLEL":
                continue
            convergence = self.pe._convergence_ref(gw)
            for b in gw.branches:
                target = self.pe._resolve_target(b.target_task_id, b.target_gateway_id, b.connect_to_end, b.end_event_name)
                segs = self.pe._expand(target, [], 1.0, [], 0, stop_ref=convergence) or []
                ids.update(s["task_id"] for s in segs if s["type"] == "task")
        return len(ids)

    def _count_substitutable(self) -> int:
        return sum(1 for t in self.gb.tasks.values() if len(t.job_tasks) > 1)

    def _pert_sample(self, expected: float, optimistic: Optional[float] = None, pessimistic: Optional[float] = None) -> float:
        a = optimistic if optimistic is not None else expected * 0.8
        b = pessimistic if pessimistic is not None else expected * 1.2
        if b <= a:
            return expected
        alpha = max(1 + 4 * (expected - a) / (b - a), 1e-6)
        beta = max(1 + 4 * (b - expected) / (b - a), 1e-6)
        return a + self._rng.betavariate(alpha, beta) * (b - a)

    def _monte_carlo(self, paths, iterations: int) -> dict:
        probs = [m["probability"] for m in paths]
        durations, costs = [], []
        for _ in range(iterations):
            path = self._rng.choices(paths, weights=probs, k=1)[0]
            total_d = total_c = 0.0
            for tid in path["task_ids"]:
                t = self.gb.tasks[tid]
                active = t.proc_time + t.rework_time
                sampled = self._pert_sample(active) if active > 0 else 0.0
                total_d += sampled + t.wait_time
                hours = sampled / 60.0
                total_c += sum(
                    hours * ((jt.get("job") or {}).get("hourlyRate", 0) or 0)
                    * ((jt.get("time_allocation_percentage") or 0) / 100.0)
                    for jt in t.job_tasks
                )
            durations.append(total_d)
            costs.append(total_c)

        def pct(vals, p):
            s = sorted(vals)
            k = (len(s) - 1) * (p / 100)
            f, c = int(k), min(int(k) + 1, len(s) - 1)
            return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)

        return {
            "iterations": iterations,
            "cycle_time_minutes": {"p50": round(pct(durations, 50), 2), "p90": round(pct(durations, 90), 2),
                                    "p95": round(pct(durations, 95), 2)},
            "labor_cost_usd": {"p50": round(pct(costs, 50), 2), "p90": round(pct(costs, 90), 2),
                               "p95": round(pct(costs, 95), 2)},
        }
