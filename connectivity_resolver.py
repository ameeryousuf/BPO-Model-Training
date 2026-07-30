"""Resolves real task/gateway connectivity from a raw Signavio BPMN diagram."""
from __future__ import annotations


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

    split_gateways, join_only = set(), set()
    for shape_id, shape in shapes.items():
        if shape["type"] != "Gateway":
            continue
        (split_gateways if len(outgoing_edges(shape_id)) > 1 else join_only).add(shape_id)

    def skip_pass_through(shape_id, max_hops=30):
        current, hops = shape_id, 0
        while current in join_only and hops < max_hops:
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
        if shape["type"] == "Gateway" and real_id in join_only:
            return ("end", "End")
        raise ConnectivityResolutionError(f"Shape {real_id} ({shape['type']}) is not a valid flow destination.")

    task_next = {}
    for shape_id, shape in shapes.items():
        if shape["type"] != "Task":
            continue
        edges = outgoing_edges(shape_id)
        if not edges:
            raise ConnectivityResolutionError(f"Task '{shape.get('name') or shape_id}' has no outgoing flow.")
        task_next[shape_id] = classify(edges[0][0])

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

    start_events = [sid for sid, s in shapes.items() if s["type"] == "StartEvent"]
    if not start_events:
        raise ConnectivityResolutionError("No StartEvent found in diagram.")
    start_edges = outgoing_edges(start_events[0])
    if not start_edges:
        raise ConnectivityResolutionError("StartEvent has no outgoing flow.")
    start_ref = classify(start_edges[0][0])
    if start_ref[0] == "end":
        raise ConnectivityResolutionError("Process has no tasks between start and end.")

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
        "shapes": shapes,
        "split_gateways": split_gateways,
        "gateway_type_of": {gw_id: _classify_gateway_type(shapes[gw_id]) for gw_id in split_gateways},
        "start_ref": start_ref,
        "task_next": task_next,
        "gateway_predecessor": gateway_predecessor,
        "gateway_branches": gateway_branches,
        "gateway_convergence": gateway_convergence,
    }


def build_ordered_task_list(resolved: dict) -> list[str]:
    order, seen = [], set()

    def visit(ref):
        kind, ident = ref
        if kind == "end" or (kind, ident) in seen:
            return
        seen.add((kind, ident))
        if kind == "task":
            order.append(ident)
            visit(resolved["task_next"][ident])
        elif kind == "gateway":
            for b in resolved["gateway_branches"][ident]:
                if b["kind"] in ("task", "gateway"):
                    visit((b["kind"], b["target"]))

    visit(resolved["start_ref"])

    all_tasks = {sid for sid, s in resolved["shapes"].items() if s["type"] == "Task"}
    orphans = all_tasks - set(order)
    if orphans:
        raise ConnectivityResolutionError(f"{len(orphans)} task(s) unreachable from process start: {sorted(orphans)[:5]}")
    return order
