from __future__ import annotations
import copy
import random
import statistics
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

# 1. Signavio shape-graph -> connectivity resolution

class ConnectivityResolutionError(Exception):
    pass


def _walk_shapes(shapes):
    for shape in shapes or []:
        stencil = (shape.get("stencil") or {}).get("id", "")
        yield shape.get("resourceId"), stencil, shape
        yield from _walk_shapes(shape.get("childShapes"))


def _build_registries(model: dict):
    shapes, flows = {}, {}
    for rid, stencil, shape in _walk_shapes(model.get("childShapes")):
        if rid is None:
            continue
        props = shape.get("properties") or {}
        name = (props.get("name") or "").strip()
        outgoing = [o.get("resourceId") for o in shape.get("outgoing", [])]

        if stencil == "Task":
            shapes[rid] = {"type": "Task", "stencil": stencil, "name": name, "properties": props, "outgoing": outgoing}
        elif "Gateway" in stencil:
            shapes[rid] = {"type": "Gateway", "stencil": stencil, "name": name, "properties": props, "outgoing": outgoing}
        elif stencil.startswith("Start"):
            shapes[rid] = {"type": "StartEvent", "stencil": stencil, "name": name, "properties": props, "outgoing": outgoing}
        elif stencil.startswith("End"):
            shapes[rid] = {"type": "EndEvent", "stencil": stencil, "name": name, "properties": props, "outgoing": outgoing}
        elif "Event" in stencil:
            shapes[rid] = {"type": "IntermediateEvent", "stencil": stencil, "name": name, "properties": props, "outgoing": outgoing}
        elif stencil == "SequenceFlow":
            flows[rid] = {"name": name, "target": (shape.get("target") or {}).get("resourceId")}

    return shapes, flows


def _classify_gateway_type(shape: dict) -> str:
    gt = (shape.get("properties") or {}).get("gatewaytype", "").upper()
    if gt == "XOR":
        return "EXCLUSIVE"
    if gt == "AND":
        return "PARALLEL"
    if gt == "OR":
        return "INCLUSIVE"
    stencil = shape.get("stencil", "")
    if "Exclusive" in stencil:
        return "EXCLUSIVE"
    if "Parallel" in stencil:
        return "PARALLEL"
    if "Inclusive" in stencil:
        return "INCLUSIVE"
    if "Eventbased" in stencil or "EventBased" in stencil:
        return "EVENT_BASED"
    return "EXCLUSIVE"


def resolve_connectivity(model: dict) -> dict:
    shapes, flows = _build_registries(model)

    def outgoing_edges(shape_id):
        shape = shapes.get(shape_id)
        if shape is None:
            return []
        return [(flows[f]["target"], flows[f].get("name") or "")
                for f in shape.get("outgoing", []) if f in flows and flows[f].get("target")]

    split_gateways, pass_through = set(), set()
    for shape_id, shape in shapes.items():
        if shape["type"] == "Gateway":
            (split_gateways if len(outgoing_edges(shape_id)) > 1 else pass_through).add(shape_id)
        elif shape["type"] == "IntermediateEvent":
            pass_through.add(shape_id)

    def skip_pass_through(shape_id, max_hops=30):
        current, hops = shape_id, 0
        while current in pass_through and hops < max_hops:
            edges = outgoing_edges(current)
            if not edges:
                return current
            current, _ = edges[0]
            hops += 1
        if hops >= max_hops:
            raise ConnectivityResolutionError(f"Pass-through chain from {shape_id} exceeded max hops (cycle?).")
        return current

    def classify(shape_id):
        real_id = skip_pass_through(shape_id)
        shape = shapes.get(real_id)
        if shape is None:
            raise ConnectivityResolutionError(f"Unresolvable shape reference: {real_id}")
        if shape["type"] == "Task":
            return ("task", real_id)
        if shape["type"] == "Gateway" and real_id in split_gateways:
            return ("gateway", real_id)
        if shape["type"] == "EndEvent":
            return ("end", shape.get("name") or "End")
        if real_id in pass_through:
            return ("end", "End")
        raise ConnectivityResolutionError(f"Shape {real_id} ({shape['type']}) is not a valid flow destination.")

    task_next = {}
    for shape_id, shape in shapes.items():
        if shape["type"] != "Task":
            continue
        edges = outgoing_edges(shape_id)
        task_next[shape_id] = ("end", "End") if not edges else classify(edges[0][0])

    gateway_branches = {
        gw_id: [{"label": label, **dict(zip(("kind", "target"), classify(target)))}
                for target, label in outgoing_edges(gw_id)]
        for gw_id in split_gateways
    }

    gateway_predecessor = {gw_id: None for gw_id in split_gateways}
    for shape_id, (kind, target) in task_next.items():
        if kind == "gateway":
            gateway_predecessor[target] = ("task", shape_id)
    for gw_id, branches in gateway_branches.items():
        for b in branches:
            if b["kind"] == "gateway":
                gateway_predecessor[b["target"]] = ("gateway", gw_id)

    def reachable_task_count(ref):
        seen, count = set(), 0

        def visit(node_ref):
            nonlocal count
            kind, ident = node_ref
            if kind == "end" or (kind, ident) in seen:
                return
            seen.add((kind, ident))
            if kind == "task":
                count += 1
                visit(task_next[ident])
            elif kind == "gateway":
                for b in gateway_branches[ident]:
                    if b["kind"] in ("task", "gateway"):
                        visit((b["kind"], b["target"]))

        visit(ref)
        return count

    start_events = [sid for sid, s in shapes.items() if s["type"] == "StartEvent"]
    if not start_events:
        raise ConnectivityResolutionError("No StartEvent found in diagram.")

    start_ref, best_count = None, -1
    for se in start_events:
        edges = outgoing_edges(se)
        if not edges:
            continue
        candidate = classify(edges[0][0])
        if candidate[0] == "end":
            continue
        count = reachable_task_count(candidate)
        if count > best_count:
            start_ref, best_count = candidate, count

    if start_ref is None:
        raise ConnectivityResolutionError("No StartEvent leads to any task.")

    def forward_sequence(ref, max_len=200):
        seq, seen = [], set()
        kind, ident = ref
        while len(seq) < max_len and (kind, ident) not in seen:
            seen.add((kind, ident))
            seq.append((kind, ident))
            if kind == "end":
                break
            if kind == "task":
                kind, ident = task_next[ident]
            else:
                break
        return seq

    gateway_convergence = {}
    for gw_id in split_gateways:
        gtype = _classify_gateway_type(shapes[gw_id])
        if gtype not in ("PARALLEL", "INCLUSIVE"):
            continue
        sequences = [forward_sequence((b["kind"], b["target"])) for b in gateway_branches[gw_id]]
        common = next((node for node in sequences[0] if all(node in s for s in sequences[1:])), None)
        if common is None:
            name = shapes[gw_id].get("name") or gw_id
            raise ConnectivityResolutionError(f"No common convergence point found for gateway '{name}'.")
        gateway_convergence[gw_id] = common

    return {
        "shapes": shapes, "split_gateways": split_gateways,
        "gateway_type_of": {gw_id: _classify_gateway_type(shapes[gw_id]) for gw_id in split_gateways},
        "start_ref": start_ref, "task_next": task_next,
        "gateway_predecessor": gateway_predecessor, "gateway_branches": gateway_branches,
        "gateway_convergence": gateway_convergence,
    }


def build_ordered_task_list(resolved: dict):
    order, seen = [], set()
    reachable_gateways = set()

    def visit(ref):
        kind, ident = ref
        if kind == "end" or (kind, ident) in seen:
            return
        seen.add((kind, ident))
        if kind == "task":
            order.append(ident)
            visit(resolved["task_next"][ident])
        elif kind == "gateway":
            reachable_gateways.add(ident)
            for b in resolved["gateway_branches"][ident]:
                if b["kind"] in ("task", "gateway"):
                    visit((b["kind"], b["target"]))

    visit(resolved["start_ref"])
    return order, reachable_gateways


# 2. Keyword-based activity classification

CONTROL_KEYWORDS = ("check", "verify", "validate", "inspect", "review", "audit", "confirm")
AUTHORIZE_KEYWORDS = ("approve", "authorize", "sign off", "sign-off", "accept", "reject")
COMMUNICATION_KEYWORDS = ("send", "notify", "inform", "contact", "call", "email", "receive", "request", "reply")
BATCH_KEYWORDS = ("batch", "consolidate", "aggregate", "compile")
PERIODIC_KEYWORDS = ("end of day", "eod", "daily", "weekly", "monthly", "periodic", "end of month")
VA_KEYWORDS = ("create", "prepare", "produce", "build", "design", "develop", "generate")
AUTOMATABLE_KEYWORDS = ("system", "automatically", "online", "portal", "generate", "auto")


def classify_activity_type(name: str) -> str:
    lower = name.lower()
    if any(k in lower for k in COMMUNICATION_KEYWORDS):
        return "communication"
    if any(k in lower for k in CONTROL_KEYWORDS):
        return "check"
    if any(k in lower for k in AUTHORIZE_KEYWORDS):
        return "authorize"
    if any(k in lower for k in BATCH_KEYWORDS):
        return "batch"
    return "basic"


def classify_value(name: str, activity_type: str) -> str:
    lower = name.lower()
    if activity_type in ("check", "authorize"):
        return "BVA"
    if any(k in lower for k in VA_KEYWORDS):
        return "VA"
    return "BVA"


def is_periodic(name: str) -> bool:
    return any(k in name.lower() for k in PERIODIC_KEYWORDS)


def is_batch(name: str, activity_type: str) -> bool:
    return activity_type == "batch"


def is_automated(name: str, activity_type: str) -> bool:
    lower = name.lower()
    return activity_type == "communication" and any(k in lower for k in AUTOMATABLE_KEYWORDS)


def derive(name: str) -> dict:
    activity_type = classify_activity_type(name)
    return {
        "activity_type": activity_type,
        "value_classification": classify_value(name, activity_type),
        "is_periodic": is_periodic(name),
        "is_batch": is_batch(name, activity_type),
    }


# 3. Synthetic enrichment + schema/XML builders

DEFAULT_JOB_TITLES = ("Process Owner", "Department Manager", "Analyst", "Coordinator",
                      "Specialist", "Clerk", "Supervisor")
SYNTH_SEED = 7
HOURLY_RATE_RANGE = (15, 120)
PROCESS_TIME_RANGE_MIN = (5, 240)
REWORK_TIME_FRACTION_RANGE = (0.05, 0.25)
DEFAULT_CURRENCY = "USD"


def _synth_job(rng: random.Random, job_id: int) -> dict:
    title = rng.choice(DEFAULT_JOB_TITLES)
    return {
        "job_id": job_id, "jobCode": f"SYN-J-{job_id}", "job_level_id": rng.randint(1, 6),
        "hourlyRate": rng.randint(*HOURLY_RATE_RANGE), "maxHoursPerDay": 8,
        "description": f"Synthetic role: {title}", "name": title,
        "capacity_buffer": str(rng.choice([5, 10, 15, 20])), "days_per_week": "5",
        "hours_per_day": "8", "currencyType": DEFAULT_CURRENCY,
    }


def _build_bpmn_xml(resolved: dict, process_name: str, process_code: str) -> str:
    ns = "http://www.omg.org/spec/BPMN/20100524/MODEL"
    ET.register_namespace("bpmn", ns)
    definitions = ET.Element(f"{{{ns}}}definitions", {
        "id": f"Definitions_{process_code}", "targetNamespace": "http://synthetic.local/bpmn",
    })
    process_el = ET.SubElement(definitions, f"{{{ns}}}process", {
        "id": f"Process_{process_code}", "name": process_name, "isExecutable": "false",
    })
    for rid, shape in resolved["shapes"].items():
        if shape["type"] == "Task":
            ET.SubElement(process_el, f"{{{ns}}}task", {"id": rid, "name": shape["name"]})
        elif shape["type"] in ("StartEvent", "EndEvent"):
            tag = "startEvent" if shape["type"] == "StartEvent" else "endEvent"
            ET.SubElement(process_el, f"{{{ns}}}{tag}", {"id": rid, "name": shape["name"]})
    for gw_id in resolved["split_gateways"]:
        gtype = resolved["gateway_type_of"][gw_id]
        tag = {"EXCLUSIVE": "exclusiveGateway", "PARALLEL": "parallelGateway",
               "INCLUSIVE": "inclusiveGateway", "EVENT_BASED": "eventBasedGateway"}[gtype]
        ET.SubElement(process_el, f"{{{ns}}}{tag}", {"id": gw_id, "name": resolved["shapes"][gw_id]["name"]})

    flow_idx = 0
    for rid, shape in resolved["shapes"].items():
        if shape["type"] == "Task":
            nxt = resolved["task_next"].get(rid)
            if nxt and nxt[0] in ("task", "gateway"):
                flow_idx += 1
                ET.SubElement(process_el, f"{{{ns}}}sequenceFlow",
                              {"id": f"Flow_{flow_idx}", "sourceRef": rid, "targetRef": nxt[1]})
    for gw_id in resolved["split_gateways"]:
        for b in resolved["gateway_branches"][gw_id]:
            if b["kind"] in ("task", "gateway"):
                flow_idx += 1
                ET.SubElement(process_el, f"{{{ns}}}sequenceFlow",
                              {"id": f"Flow_{flow_idx}", "sourceRef": gw_id, "targetRef": b["target"]})

    return ET.tostring(definitions, encoding="utf-8", xml_declaration=True).decode("utf-8")


def convert_to_schema(row: dict, process_id: int) -> dict:
    resolved = resolve_connectivity(row["model"])
    ordered_ids, reachable_gateways = build_ordered_task_list(resolved)

    task_id_of = {rid: i + 1 for i, rid in enumerate(ordered_ids)}
    gateway_pk_of = {rid: 1000 + i for i, rid in enumerate(sorted(reachable_gateways))}

    def kind_id(kind, ident):
        return task_id_of[ident] if kind == "task" else gateway_pk_of[ident] if kind == "gateway" else None

    process_code = f"SYN-P-{process_id}"
    rng = random.Random(SYNTH_SEED ^ process_id)

    process_tasks = []
    job_id = 1
    for order, rid in enumerate(ordered_ids, start=1):
        shape = resolved["shapes"][rid]
        task_id = task_id_of[rid]
        next_ref = resolved["task_next"][rid]
        attrs = derive(shape["name"])

        proc_time = rng.randint(*PROCESS_TIME_RANGE_MIN)
        rework_frac = round(rng.uniform(*REWORK_TIME_FRACTION_RANGE), 2)
        n_jobs = rng.choice([1, 1, 1, 2])
        job_tasks = []
        for _ in range(n_jobs):
            job = _synth_job(rng, job_id)
            job_tasks.append({
                "job_id": job["job_id"], "task_id": task_id, "role": rng.choice(["R", "A", "C", "I"]),
                "time_allocation_percentage": round(rng.uniform(1, 20), 2), "job": job,
            })
            job_id += 1

        process_tasks.append({
            "process_task_id": 6000 + order, "process_id": process_id, "task_id": task_id, "order": order,
            "child_process_id": None, "value_classification": attrs["value_classification"],
            "value_rationale": None, "bva_business_goal": None, "value_source": "derived",
            "task": {
                "task_id": task_id, "task_code": f"SYN-T-{process_id}-{order}",
                "task_company_id": None, "task_name": shape["name"] or f"Task {task_id}",
                "task_overview": "", "status_id": 1, "task_version": 0,
                "expected_process_time": proc_time, "expected_rework_time": round(proc_time * rework_frac),
                "expected_waiting_time": rng.choice([None, rng.randint(1, 30)]),
                "frequency_interval": 1,
                "frequency_period": "WEEK" if attrs["is_periodic"] else "DAY",
                "occurrences": "1", "jobTasks": job_tasks,
                "_activity_type": attrs["activity_type"], "_is_periodic": attrs["is_periodic"],
                "_is_batch": attrs["is_batch"],
                "_next_task_id": kind_id(*next_ref) if next_ref[0] == "task" else None,
                "_next_gateway_id": kind_id(*next_ref) if next_ref[0] == "gateway" else None,
                "_connects_to_end": next_ref[0] == "end",
            },
            "child_process": None,
        })

    gateways = []
    for gw_id in sorted(reachable_gateways):
        gw_pk = gateway_pk_of[gw_id]
        pred = resolved["gateway_predecessor"][gw_id]
        branches = resolved["gateway_branches"][gw_id]
        n = len(branches)
        raw_probs = [rng.random() + 0.1 for _ in range(n)]
        total = sum(raw_probs)
        probs = [round(p / total, 2) for p in raw_probs]

        branch_records = []
        for i, b in enumerate(branches):
            branch_records.append({
                "id": i + 1, "gateway_pk_id": gw_pk, "is_default": i == 0,
                "condition": b["label"] or f"branch_{i + 1}", "probability": probs[i],
                "target_task_id": kind_id(b["kind"], b["target"]) if b["kind"] == "task" else None,
                "target_gateway_id": kind_id(b["kind"], b["target"]) if b["kind"] == "gateway" else None,
                "connect_to_end": b["kind"] == "end",
                "end_event_name": b["target"] if b["kind"] == "end" else None,
                "end_task_id": None,
            })

        conv = resolved["gateway_convergence"].get(gw_id)
        gateways.append({
            "gateway_pk_id": gw_pk, "gateway_type": resolved["gateway_type_of"][gw_id],
            "name": resolved["shapes"][gw_id]["name"] or f"Gateway {gw_pk}",
            "after_task_id": kind_id(*pred) if pred and pred[0] == "task" else None,
            "after_gateway_id": kind_id(*pred) if pred and pred[0] == "gateway" else None,
            "converge_at_task_id": kind_id(*conv) if conv and conv[0] == "task" else None,
            "converge_gateway_name": "",
            "converge_to_end": bool(conv and conv[0] == "end"),
            "converge_at_gateway_id": kind_id(*conv) if conv and conv[0] == "gateway" else None,
            "branches": branch_records,
        })

    bpmn_xml = _build_bpmn_xml(resolved, row["name"] or process_code, process_code)
    total_time = sum(pt["task"]["expected_process_time"] for pt in process_tasks)

    return {
        "process_id": process_id, "company_id": 900_000 + process_id,
        "created_at": row["datetime"], "updated_at": row["datetime"],
        "capacity_requirement_minutes": total_time, "parent_process_id": None, "parent_task_id": None,
        "process_code": process_code, "process_name": row["name"] or process_code,
        "process_overview": row["description"] or "<p>No description provided in source data.</p>",
        "process_category_id": 1, "process_status_id": 1, "process_version": 0,
        "bpmn_xml": bpmn_xml, "created_by": None, "updated_by": None, "PROCESS_STATUS": "CREATED",
        "bpmn_xml_updated_at": row["datetime"],
        "company": {"company_id": 900_000 + process_id, "companyCode": f"SYN-{process_id}",
                    "name": "Synthetic Org", "created_by": None, "org_type_id": 1},
        "process": None,
        "creator": {"user_id": None, "name": "Synthetic Pipeline"},
        "processCategory": {"id": 1, "description": "Auto-assigned category for synthetic dataset",
                            "name": "Uncategorized"},
        "gateways": gateways, "process_task": process_tasks,
        "_source": {"revision_id": row["revision_id"], "model_id": row["model_id"],
                    "organization_id": row["organization_id"]},
    }


# 4. Validation

REQUIRED_TOP_LEVEL = ["process_id", "process_code", "process_name", "bpmn_xml", "gateways", "process_task"]


def validate_record(record: dict, min_tasks: int = 2) -> list:
    problems = []
    for key in REQUIRED_TOP_LEVEL:
        if key not in record or record[key] in (None, ""):
            problems.append(f"missing/empty field: {key}")

    if not record.get("process_task"):
        problems.append("process_task list is empty")
    else:
        if len(record["process_task"]) < min_tasks:
            problems.append(f"only {len(record['process_task'])} task(s), below min_tasks={min_tasks}")
        for pt in record["process_task"]:
            task = pt.get("task", {})
            if task.get("expected_process_time", 0) <= 0:
                problems.append(f"task {task.get('task_code')} has non-positive process time")
            has_next = any([task.get("_next_task_id") is not None,
                             task.get("_next_gateway_id") is not None,
                             task.get("_connects_to_end")])
            if not has_next:
                problems.append(f"task {task.get('task_code')} has no recorded successor")

    for gw in record.get("gateways", []):
        probs = [b["probability"] for b in gw.get("branches", [])]
        if probs and abs(sum(probs) - 1.0) > 0.05:
            problems.append(f"gateway {gw.get('name')} branch probabilities sum to {sum(probs):.2f}")

    try:
        ET.fromstring(record["bpmn_xml"])
    except (ET.ParseError, KeyError) as exc:
        problems.append(f"invalid bpmn_xml: {exc}")

    return problems


# 5. Graph model for process-measure computation

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
        self.tasks: dict = {}
        self.gateways: dict = {}
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

    def gateway_after_task(self, task_id: int):
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

    def enumerate_structured_paths(self) -> list:
        raw_paths: list = []
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
                gw_ref = ("gateway", gw.gateway_pk_id)
                if stop_ref is not None and gw_ref == stop_ref:
                    return segments
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


DEPARTMENT_OF = {
    "Process Owner": "Management", "Department Manager": "Management",
    "Analyst": "Operations", "Specialist": "Operations",
    "Coordinator": "Coordination", "Supervisor": "Coordination",
    "Clerk": "Administration",
}


def _task_job_names(task):
    return [(jt.get("job") or {}).get("name", "") for jt in task.job_tasks]


def _departments(task):
    return {DEPARTMENT_OF.get(n, "Other") for n in _task_job_names(task)}


def _task_cost_rate(task) -> float:
    return sum(
        (jt.get("job") or {}).get("hourlyRate", 0) * (jt.get("time_allocation_percentage", 0) / 100.0)
        for jt in task.job_tasks
    )


def compute_measures(record: dict) -> dict:
    gb = GraphBuilder(record)
    pe = PathEnumerator(gb)
    tasks = [t for t in gb.tasks.values() if not t.is_subprocess_slot]
    n = len(tasks) or 1
    gateways = list(gb.gateways.values())

    parallel_ids = set()
    for gw in gateways:
        if gw.gateway_type != "PARALLEL":
            continue
        conv = pe._convergence_ref(gw)
        for b in gw.branches:
            target = pe._resolve_target(b.target_task_id, b.target_gateway_id, b.connect_to_end, b.end_event_name)
            segs = pe._expand(target, [], 1.0, [], 0, stop_ref=conv) or []
            parallel_ids.update(s["task_id"] for s in segs if s["type"] == "task")

    all_depts = set()
    for t in tasks:
        all_depts |= _departments(t)
    dept_share_count = sum(1 for t in tasks if len(_departments(t)) >= 2)

    all_roles = set()
    for t in tasks:
        all_roles |= set(_task_job_names(t))

    handoffs, pairs = 0, 0
    for t in tasks:
        if t.next_task_id is not None and t.next_task_id in gb.tasks:
            pairs += 1
            nxt = gb.tasks[t.next_task_id]
            if _task_job_names(t) and _task_job_names(nxt) and not (set(_task_job_names(t)) & set(_task_job_names(nxt))):
                handoffs += 1

    knock_out_branches = sum(
        1 for gw in gateways if gw.gateway_type in ("EXCLUSIVE", "EVENT_BASED")
        for b in gw.branches if b.connect_to_end
    )

    levels = [
        (jt.get("job") or {}).get("job_level_id", 1)
        for t in tasks for jt in t.job_tasks
    ]

    comm_tasks = [t for t in tasks if t.activity_type == "communication"]

    rates = [_task_cost_rate(t) for t in tasks if t.job_tasks]
    mean_rate = statistics.mean(rates) if rates else 0.0
    cost_outlier_ratio = (max(rates) / mean_rate) if mean_rate > 0 else 0.0

    durations = [t.proc_time for t in tasks]
    mean_dur = statistics.mean(durations) if durations else 0.0
    duration_outlier_ratio = (max(durations) / mean_dur) if mean_dur > 0 else 0.0

    gateway_after_ids = {gw.after_task_id for gw in gateways if gw.after_task_id is not None}
    resequencing_available = 0.0
    for t in tasks:
        if t.task_id in gateway_after_ids or t.next_task_id is None:
            continue
        nxt = gb.tasks.get(t.next_task_id)
        if nxt is None or nxt.is_subprocess_slot:
            continue
        if t.activity_type == "check" and nxt.activity_type == "check":
            r1, r2 = _task_cost_rate(t), _task_cost_rate(nxt)
            if r2 > 0 and r1 > r2 * 1.2:
                resequencing_available = 1.0
                break

    return {
        "parallelism": len(parallel_ids) / n,
        "level_of_control": sum(1 for t in tasks if t.activity_type == "check") / n,
        "level_of_authorization": sum(1 for t in tasks if t.activity_type == "authorize") / n,
        "batch": sum(1 for t in tasks if t.is_batch) / n,
        "periodic": sum(1 for t in tasks if t.is_periodic) / n,
        "process_contacts": len(comm_tasks) / n,
        "department_involvement": len(all_depts) / n,
        "department_share": dept_share_count / n,
        "role_usage": len(all_roles) / 7,
        "user_involvement": sum(len(t.job_tasks) for t in tasks) / n,
        "process_hand_offs": (handoffs / pairs) if pairs else 0.0,
        "knock_outs": knock_out_branches / n,
        "managerial_layers": ((max(levels) - min(levels) + 1) / 6) if levels else 0.0,
        "it_automation": sum(1 for t in tasks if is_automated(t.name, t.activity_type)) / n,
        "it_comm": 1.0 if not comm_tasks else sum(1 for t in comm_tasks if is_automated(t.name, t.activity_type)) / len(comm_tasks),
        "process_versions": 1,
        "process_size": n,
        "cost_outlier_ratio": cost_outlier_ratio,
        "duration_outlier_ratio": duration_outlier_ratio,
        "resequencing_available": resequencing_available,
    }


# 6. Redesign heuristics (10 total; see chat for Netjes/R&M sourcing notes)

MIN_TASKS_REDESIGN = 2


def _tasks_by_order(record):
    return sorted(record["process_task"], key=lambda pt: pt["order"])


def _job_names(pt):
    return [(jt.get("job") or {}).get("name", "") for jt in pt["task"].get("jobTasks", [])]


def _find_task(record, task_id):
    for pt in record["process_task"]:
        if pt["task_id"] == task_id:
            return pt
    return None


def _relink_predecessors(record, removed_task_id, new_next_task_id, new_next_gateway_id, new_connects_to_end):
    for pt in record["process_task"]:
        t = pt["task"]
        if t.get("_next_task_id") == removed_task_id:
            t["_next_task_id"] = new_next_task_id
            t["_next_gateway_id"] = new_next_gateway_id
            t["_connects_to_end"] = new_connects_to_end


def qualify_parallelism(m):
    return m["parallelism"] < 0.1


def apply_parallelism(record):
    ordered = _tasks_by_order(record)
    gateway_after_ids = {gw["after_task_id"] for gw in record["gateways"] if gw.get("after_task_id") is not None}
    for pt in ordered:
        t = pt["task"]
        nxt_id = t.get("_next_task_id")
        if nxt_id is None or pt["task_id"] in gateway_after_ids:
            continue
        nxt_pt = _find_task(record, nxt_id)
        if nxt_pt is None:
            continue
        if set(_job_names(pt)) & set(_job_names(nxt_pt)):
            continue

        trial = copy.deepcopy(record)
        trial_pt = _find_task(trial, pt["task_id"])
        trial_nxt_pt = _find_task(trial, nxt_id)
        trial_t = trial_pt["task"]

        after_pt_next = trial_nxt_pt["task"].get("_next_task_id")
        after_pt_next_gw = trial_nxt_pt["task"].get("_next_gateway_id")
        after_pt_connects_end = trial_nxt_pt["task"].get("_connects_to_end", False)

        existing_ids = [gw["gateway_pk_id"] for gw in trial["gateways"]]
        new_gw_id = (max(existing_ids) + 1) if existing_ids else 1000

        trial_t["_next_task_id"] = None
        trial_t["_next_gateway_id"] = new_gw_id
        trial_t["_connects_to_end"] = False

        new_gateway = {
            "gateway_pk_id": new_gw_id, "gateway_type": "PARALLEL",
            "name": f"Parallel split after {trial_t['task_name']}",
            "after_task_id": trial_pt["task_id"], "after_gateway_id": None,
            "converge_at_task_id": after_pt_next, "converge_gateway_name": "",
            "converge_to_end": after_pt_connects_end,
            "converge_at_gateway_id": after_pt_next_gw,
            "branches": [
                {"id": 1, "gateway_pk_id": new_gw_id, "is_default": True, "condition": "branch_1",
                 "probability": 1.0, "target_task_id": trial_nxt_pt["task_id"], "target_gateway_id": None,
                 "connect_to_end": False, "end_event_name": None, "end_task_id": None},
            ],
        }
        trial["gateways"].append(new_gateway)
        trial_nxt_pt["task"]["_next_task_id"] = after_pt_next
        trial_nxt_pt["task"]["_next_gateway_id"] = after_pt_next_gw
        trial_nxt_pt["task"]["_connects_to_end"] = after_pt_connects_end

        try:
            gb = GraphBuilder(trial)
            paths = PathEnumerator(gb).enumerate_structured_paths()
            if not paths:
                continue
        except Exception:
            continue

        reason = (f"Tasks '{pt['task']['task_name']}' and '{nxt_pt['task']['task_name']}' use different "
                  f"roles with no dependency; placed in parallel to reduce cycle time.")
        return trial, [{"task_id": pt["task_id"]}, {"task_id": nxt_pt["task_id"]}], reason

    return record, [], "No safe independent adjacent task pair found despite qualifying measure."


def qualify_elimination(m):
    return m["level_of_control"] > 0.2


def apply_elimination(record):
    if len(record["process_task"]) <= MIN_TASKS_REDESIGN:
        return record, [], "Process too small to safely eliminate a task without dropping below the minimum."
    candidates = [pt for pt in record["process_task"]
                  if pt["task"].get("_activity_type") == "check" or pt.get("value_classification") == "NVA"]
    if not candidates:
        return record, [], "No control/check or NVA tasks found despite qualifying measure."
    target = candidates[0]
    tid = target["task_id"]
    t = target["task"]
    next_task_id = t.get("_next_task_id")
    next_gateway_id = t.get("_next_gateway_id")
    connects_to_end = t.get("_connects_to_end", False)
    _relink_predecessors(record, tid, next_task_id, next_gateway_id, connects_to_end)
    record["process_task"] = [pt for pt in record["process_task"] if pt["task_id"] != tid]
    reason = f"Task '{t['task_name']}' is a control/check task adding no direct customer value; removed per task elimination heuristic."
    return record, [{"task_id": tid, "task_name": t["task_name"]}], reason


def qualify_automation(m):
    return m["it_automation"] < 0.5 or (m["it_comm"] < 0.5 and m["level_of_control"] > 0.2)


def apply_automation(record):
    for pt in record["process_task"]:
        t = pt["task"]
        activity_type = t.get("_activity_type")
        if activity_type in ("communication", "check") and not is_automated(t["task_name"], activity_type):
            t["expected_process_time"] = max(1, round(t["expected_process_time"] * 0.4))
            for jt in t.get("jobTasks", []):
                jt["time_allocation_percentage"] = round(jt.get("time_allocation_percentage", 0) * 0.4, 2)
            reason = f"Task '{t['task_name']}' is a manual {activity_type} task; automated to reduce processing time and labor cost."
            return record, [{"task_id": pt["task_id"], "task_name": t["task_name"]}], reason
    return record, [], "No automatable task found despite qualifying measure."


def qualify_composition(m):
    return m["parallelism"] < 0.25 and m["process_hand_offs"] < 0.5 and m["process_versions"] < 2


def apply_composition(record):
    if len(record["process_task"]) <= MIN_TASKS_REDESIGN:
        return record, [], "Process too small to safely compose two tasks without dropping below the minimum."
    ordered = _tasks_by_order(record)
    gateway_after_ids = {gw["after_task_id"] for gw in record["gateways"] if gw.get("after_task_id") is not None}
    for pt in ordered:
        t = pt["task"]
        nxt_id = t.get("_next_task_id")
        if nxt_id is None or pt["task_id"] in gateway_after_ids:
            continue
        nxt_pt = _find_task(record, nxt_id)
        if nxt_pt is None:
            continue
        if set(_job_names(pt)) & set(_job_names(nxt_pt)) and _job_names(pt):
            nxt_t = nxt_pt["task"]
            t["expected_process_time"] += nxt_t["expected_process_time"]
            t["expected_rework_time"] += nxt_t["expected_rework_time"]
            t["task_name"] = f"{t['task_name']} + {nxt_t['task_name']}"
            t["_next_task_id"] = nxt_t.get("_next_task_id")
            t["_next_gateway_id"] = nxt_t.get("_next_gateway_id")
            t["_connects_to_end"] = nxt_t.get("_connects_to_end", False)
            _relink_predecessors(record, nxt_id, pt["task_id"], None, False)
            for gw in record["gateways"]:
                if gw.get("after_task_id") == nxt_id:
                    gw["after_task_id"] = pt["task_id"]
            record["process_task"] = [p for p in record["process_task"] if p["task_id"] != nxt_id]
            reason = f"Adjacent tasks '{t['task_name']}' share the same role with low hand-off risk; composed into one task."
            return record, [{"task_id": pt["task_id"], "task_name": t["task_name"]}], reason
    return record, [], "No adjacent same-role task pair found despite qualifying measure."


def qualify_case_based(m):
    return m["batch"] > 0 or m["periodic"] > 0


def apply_case_based(record):
    for pt in record["process_task"]:
        t = pt["task"]
        if t.get("_is_batch") or t.get("_is_periodic"):
            t["_is_batch"] = False
            t["_is_periodic"] = False
            t["frequency_period"] = "DAY"
            old_wait = t.get("expected_waiting_time") or 0
            t["expected_waiting_time"] = round(old_wait * 0.3) if old_wait else 0
            reason = f"Task '{t['task_name']}' was batch/periodically processed; converted to case-based handling to cut waiting time."
            return record, [{"task_id": pt["task_id"], "task_name": t["task_name"]}], reason
    return record, [], "No batch/periodic task found despite qualifying measure."


def qualify_numerical_involvement(m):
    return m["department_involvement"] > 0.25 or m["user_involvement"] > 1 or m["role_usage"] < 0.5


def apply_numerical_involvement(record):
    for pt in record["process_task"]:
        job_tasks = pt["task"].get("jobTasks", [])
        if len(job_tasks) > 1:
            primary = max(job_tasks, key=lambda jt: jt.get("time_allocation_percentage", 0))
            pt["task"]["jobTasks"] = [primary]
            reason = f"Task '{pt['task']['task_name']}' had multiple role assignments; consolidated to a single owning role."
            return record, [{"task_id": pt["task_id"], "task_name": pt["task"]["task_name"]}], reason
    return record, [], "No task with multiple role assignments found despite qualifying measure."


def qualify_knockout(m):
    return m["knock_outs"] > 0


def apply_knockout(record):
    for gw in record["gateways"]:
        if gw["gateway_type"] not in ("EXCLUSIVE", "EVENT_BASED"):
            continue
        if not any(b.get("connect_to_end") for b in gw["branches"]):
            continue
        original_order = [b["condition"] for b in gw["branches"]]
        gw["branches"].sort(key=lambda b: -b["probability"])
        if [b["condition"] for b in gw["branches"]] == original_order:
            continue
        reason = f"Gateway '{gw['name']}' branches reordered by descending termination probability to minimize average effort."
        return record, [{"gateway_pk_id": gw["gateway_pk_id"], "gateway_name": gw["name"]}], reason
    return record, [], "No reorderable knock-out gateway found despite qualifying measure."


def qualify_resequencing(m):
    return m["resequencing_available"] > 0


def apply_resequencing(record):
    ordered = _tasks_by_order(record)
    gateway_after_ids = {gw["after_task_id"] for gw in record["gateways"] if gw.get("after_task_id") is not None}
    for pt in ordered:
        t = pt["task"]
        if pt["task_id"] in gateway_after_ids or t.get("_activity_type") != "check":
            continue
        nxt_id = t.get("_next_task_id")
        if nxt_id is None:
            continue
        nxt_pt = _find_task(record, nxt_id)
        if nxt_pt is None or nxt_pt["task"].get("_activity_type") != "check":
            continue

        def rate(p):
            return sum((jt.get("job") or {}).get("hourlyRate", 0) * (jt.get("time_allocation_percentage", 0) / 100.0)
                       for jt in p["task"].get("jobTasks", []))

        r1, r2 = rate(pt), rate(nxt_pt)
        if r2 <= 0 or r1 <= r2 * 1.2:
            continue

        after_next = nxt_pt["task"].get("_next_task_id")
        after_next_gw = nxt_pt["task"].get("_next_gateway_id")
        after_next_end = nxt_pt["task"].get("_connects_to_end", False)

        _relink_predecessors(record, pt["task_id"], nxt_pt["task_id"], None, False)
        nxt_pt["task"]["_next_task_id"] = pt["task_id"]
        nxt_pt["task"]["_next_gateway_id"] = None
        nxt_pt["task"]["_connects_to_end"] = False
        t["_next_task_id"] = after_next
        t["_next_gateway_id"] = after_next_gw
        t["_connects_to_end"] = after_next_end

        pt["order"], nxt_pt["order"] = nxt_pt["order"], pt["order"]
        for gw in record["gateways"]:
            if gw.get("after_task_id") == pt["task_id"]:
                gw["after_task_id"] = nxt_pt["task_id"]
            elif gw.get("after_task_id") == nxt_pt["task_id"]:
                gw["after_task_id"] = pt["task_id"]

        reason = (f"Check '{t['task_name']}' costs more per case than the following check "
                  f"'{nxt_pt['task']['task_name']}'; reordered so the cheaper check runs first.")
        return record, [{"task_id": pt["task_id"], "task_name": t["task_name"]},
                         {"task_id": nxt_pt["task_id"], "task_name": nxt_pt["task"]["task_name"]}], reason
    return record, [], "No costlier-check-before-cheaper-check pair found despite qualifying measure."


def qualify_trusted_party(m):
    return m["cost_outlier_ratio"] >= 1.5


def apply_trusted_party(record):
    best_pt, best_rate = None, 0.0
    for pt in record["process_task"]:
        rate = sum((jt.get("job") or {}).get("hourlyRate", 0) * (jt.get("time_allocation_percentage", 0) / 100.0)
                   for jt in pt["task"].get("jobTasks", []))
        if rate > best_rate:
            best_pt, best_rate = pt, rate
    if best_pt is None or best_rate <= 0:
        return record, [], "No costed task found despite qualifying measure."

    t = best_pt["task"]
    external_job = {
        "job_id": 9001, "jobCode": "SYN-J-EXTERNAL", "job_level_id": 3,
        "hourlyRate": round(best_rate * 0.6), "maxHoursPerDay": 8,
        "description": "Trusted external party", "name": "Trusted Party",
        "capacity_buffer": "10", "days_per_week": "5", "hours_per_day": "8", "currencyType": "USD",
    }
    t["jobTasks"] = [{"job_id": 9001, "task_id": t["task_id"], "role": "R",
                       "time_allocation_percentage": 100.0, "job": external_job}]
    reason = (f"Task '{t['task_name']}' had an unusually high cost-per-hour "
              f"({best_rate:.1f} vs process average); outsourced to a trusted external party.")
    return record, [{"task_id": best_pt["task_id"], "task_name": t["task_name"]}], reason


def qualify_extra_resources(m):
    return m["duration_outlier_ratio"] >= 1.5


def apply_extra_resources(record):
    best_pt, best_time = None, 0
    for pt in record["process_task"]:
        pt_time = pt["task"].get("expected_process_time", 0)
        if pt_time > best_time:
            best_pt, best_time = pt, pt_time
    if best_pt is None or best_time <= 0:
        return record, [], "No timed task found despite qualifying measure."

    t = best_pt["task"]
    old_time = t["expected_process_time"]
    t["expected_process_time"] = max(1, round(old_time * 0.6))
    if t.get("jobTasks"):
        extra = copy.deepcopy(t["jobTasks"][0])
        extra["job_id"] = 9002
        extra["job"]["job_id"] = 9002
        extra["job"]["jobCode"] = "SYN-J-EXTRA"
        t["jobTasks"].append(extra)
    reason = (f"Task '{t['task_name']}' took noticeably longer than the process average "
              f"({old_time} min); added capacity to cut its duration.")
    return record, [{"task_id": best_pt["task_id"], "task_name": t["task_name"]}], reason


HEURISTICS = [
    (1, "parallelism", qualify_parallelism, apply_parallelism),
    (2, "task_elimination", qualify_elimination, apply_elimination),
    (3, "task_automation", qualify_automation, apply_automation),
    (4, "task_composition", qualify_composition, apply_composition),
    (5, "case_based_work", qualify_case_based, apply_case_based),
    (6, "numerical_involvement", qualify_numerical_involvement, apply_numerical_involvement),
    (7, "knock_out", qualify_knockout, apply_knockout),
    (8, "resequencing", qualify_resequencing, apply_resequencing),
    (9, "trusted_party", qualify_trusted_party, apply_trusted_party),
    (10, "extra_resources", qualify_extra_resources, apply_extra_resources),
]

APPLICATION_ORDER = ["task_elimination", "task_composition", "resequencing", "knock_out", "parallelism",
                     "case_based_work", "numerical_involvement", "task_automation",
                     "trusted_party", "extra_resources"]


def redesign_process(as_is_record: dict) -> dict:
    working = copy.deepcopy(as_is_record)
    trace = []
    by_name = {name: (hid, qualify, apply_fn) for hid, name, qualify, apply_fn in HEURISTICS}

    for name in APPLICATION_ORDER:
        hid, qualify, apply_fn = by_name[name]
        measures = compute_measures(working)
        if qualify(measures):
            working, targets, reason = apply_fn(working)
            applied = bool(targets)
            trace.append({
                "heuristicId": hid, "heuristicName": name, "isApplied": applied,
                "taskApplied": targets, "reasonApplied": reason if applied else None,
            })
        else:
            trace.append({
                "heuristicId": hid, "heuristicName": name, "isApplied": False,
                "taskApplied": [], "reasonApplied": None,
            })

    return {"as-is": as_is_record, "to-be": working, "redesignTrace": trace}


# 7. Top-level worker wrappers (must stay top-level so ProcessPoolExecutor
#    can pickle them on Windows)

def worker_convert_and_validate(args):
    raw_row, process_id, min_tasks = args
    try:
        record = convert_to_schema(raw_row, process_id=process_id)
    except Exception as exc:
        return {"ok": False, "stage": "conversion", "model_id": raw_row.get("model_id"), "error": str(exc)}
    problems = validate_record(record, min_tasks=min_tasks)
    if problems:
        return {"ok": False, "stage": "validation", "model_id": raw_row.get("model_id"), "error": "; ".join(problems)}
    return {"ok": True, "record": record}


def worker_redesign(record):
    try:
        combined = redesign_process(record)
        problems = validate_record(combined["to-be"])
        if problems:
            return {"ok": False, "process_code": record.get("process_code"), "error": f"to-be failed validation: {problems}"}
        return {"ok": True, "combined": combined}
    except Exception as exc:
        return {"ok": False, "process_code": record.get("process_code"), "error": str(exc)}
