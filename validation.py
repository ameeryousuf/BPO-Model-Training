"""Structural and content validation for generated process records."""
import xml.etree.ElementTree as ET

from flow_analysis_engine import GraphBuilder, PathEnumerator

REQUIRED_TOP_LEVEL = ["process_id", "process_code", "process_name", "bpmn_xml", "gateways", "process_task"]


def validate_record(record: dict) -> list[str]:
    problems = []

    for key in REQUIRED_TOP_LEVEL:
        if key not in record or record[key] in (None, ""):
            problems.append(f"missing/empty field: {key}")

    if not record.get("process_task"):
        problems.append("process_task list is empty")
    else:
        for pt in record["process_task"]:
            task = pt.get("task", {})
            if task.get("expected_process_time", 0) <= 0:
                problems.append(f"task {task.get('task_code')} has non-positive process time")
            if not task.get("jobTasks"):
                problems.append(f"task {task.get('task_code')} has no job assignments")
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

    try:
        gb = GraphBuilder(record)
        paths = PathEnumerator(gb).enumerate_structured_paths()
        if not paths:
            problems.append("no paths could be enumerated from this process graph")
    except Exception as exc:
        problems.append(f"graph is not traversable: {exc}")

    return problems
