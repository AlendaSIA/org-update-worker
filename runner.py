from typing import Dict, Any
import traceback

from steps import (
    step_01_parse_event,
    step_02_fetch_client_id,
    step_03_compute_12m,
    step_04_build_update,
    step_05_update_org,
)


def run(payload: Dict[str, Any]) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {}
    trace = []

    try:
        ctx["payload"] = payload

        for step in [
            step_01_parse_event,
            step_02_fetch_client_id,
            step_03_compute_12m,
            step_04_build_update,
            step_05_update_org,
        ]:
            step_name = step.__name__
            try:
                step.run(ctx)
                trace.append({"step": step_name, "ok": True})
            except Exception as e:
                trace.append(
                    {
                        "step": step_name,
                        "ok": False,
                        "error": str(e),
                        "traceback": traceback.format_exc(),
                    }
                )
                return {
                    "ok": False,
                    "_trace": trace,
                    "error": str(e),
                }

        return {
            "ok": True,
            "_trace": trace,
            "payload": payload,
            "document_id": ctx.get("document_id"),
            "deal_id": ctx.get("deal_id"),
            "org_id": ctx.get("org_id"),
            "client_id": ctx.get("client_id"),
            "date_from": ctx.get("date_from"),
            "date_till": ctx.get("date_till"),
            "sales_count": ctx.get("sales_count"),
            "total_sum": ctx.get("total_sum"),
            "sample_refs": ctx.get("sample_refs"),
            "update": ctx.get("update"),
            "computed": ctx.get("computed"),
            "step_03": ctx.get("step_03"),  # 👈 tagad redzēsim Step_03 rezultātu
            "step_05": ctx.get("step_05"),
            "dry_run": bool(ctx.get("dry_run")),
        }

    except Exception as e:
        return {
            "ok": False,
            "_trace": trace,
            "error": str(e),
            "traceback": traceback.format_exc(),
        }
