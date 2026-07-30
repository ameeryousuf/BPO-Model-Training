"""Converts a raw Signavio BPMN diagram into a 1972.json-schema process record."""
import random
import xml.etree.ElementTree as ET

from connectivity_resolver import resolve_connectivity, build_ordered_task_list, ConnectivityResolutionError
from task_attributes import derive

DEFAULT_JOB_TITLES = ("Process Owner", "Department Manager", "Analyst", "Coordinator",
                      "Specialist", "Clerk", "Supervisor")


def _synth_job(rng: random.Random, job_id: int, cfg) -> dict:
    title = rng.choice(cfg.default_job_titles)
    return {
        "job_id": job_id, "jobCode": f"SYN-J-{job_id}", "job_level_id": rng.randint(1, 6),
        "hourlyRate": rng.randint(*cfg.hourly_rate_range), "maxHoursPerDay": 8,
        "description": f"Synthetic role: {title}", "name": title,
        "capacity_buffer": str(rng.choice([5, 10, 15, 20])), "days_per_week": "5",
        "hours_per_day": "8", "currencyType": cfg.default_currency,
    }


def _build_bpmn_xml(resolved: dict, process_name: str, process_code: str) -> str:
    ET.register_namespace("bpmn", "http://www.omg.org/spec/BPMN/20100524/MODEL")
    ns = "http://www.omg.org/spec/BPMN/20100524/MODEL"
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
    return ET.tostring(definitions, encoding="utf-8", xml_declaration=True).decode("utf-8")


def convert_to_schema(row: dict, process_id: int, cfg):
    resolved = resolve_connectivity(row["model"])
    ordered_ids = build_ordered_task_list(resolved)

    task_id_of = {rid: i + 1 for i, rid in enumerate(ordered_ids)}
    gateway_pk_of = {rid: 1000 + i for i, rid in enumerate(sorted(resolved["split_gateways"]))}

    def kind_id(kind, ident):
        return task_id_of[ident] if kind == "task" else gateway_pk_of[ident] if kind == "gateway" else None

    process_code = f"SYN-P-{process_id}"
    rng = random.Random(cfg.synth_seed ^ process_id)

    process_tasks = []
    job_id = 1
    for order, rid in enumerate(ordered_ids, start=1):
        shape = resolved["shapes"][rid]
        task_id = task_id_of[rid]
        next_ref = resolved["task_next"][rid]
        attrs = derive(shape["name"])

        proc_time = rng.randint(*cfg.process_time_range_min)
        rework_frac = round(rng.uniform(*cfg.rework_time_fraction_range), 2)
        n_jobs = rng.choice([1, 1, 1, 2])
        job_tasks = []
        for _ in range(n_jobs):
            job = _synth_job(rng, job_id, cfg)
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
    for gw_id in sorted(resolved["split_gateways"]):
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
        "process_category_id": cfg.default_process_category_id, "process_status_id": 1, "process_version": 0,
        "bpmn_xml": bpmn_xml, "created_by": None, "updated_by": None, "PROCESS_STATUS": "CREATED",
        "bpmn_xml_updated_at": row["datetime"],
        "company": {
            "company_id": 900_000 + process_id, "companyCode": f"SYN-{process_id}",
            "name": cfg.default_org_name, "created_by": None, "org_type_id": 1,
        },
        "process": None,
        "creator": {"user_id": None, "name": "Synthetic Pipeline"},
        "processCategory": {
            "id": cfg.default_process_category_id, "description": "Auto-assigned category for synthetic dataset",
            "name": cfg.default_process_category_name,
        },
        "gateways": gateways, "process_task": process_tasks,
        "_source": {
            "revision_id": row["revision_id"], "model_id": row["model_id"],
            "organization_id": row["organization_id"],
        },
    }
