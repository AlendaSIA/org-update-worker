import os
import requests
from typing import Any, Dict

def run(ctx: Dict[str, Any]) -> None:
    if ctx.get("skipped"):
        return

    if ctx.get("dry_run"):
        ctx["step_05"] = {"ok": True, "skipped": "dry_run_true"}
        return

    org_id = ctx.get("org_id")
    if not org_id:
        ctx["skipped"] = "no_org_id"
        return

    update = ctx.get("update") or {}
    if not update:
        ctx["step_05"] = {"ok": True, "skipped": "no_update"}
        return

    token = os.getenv("PIPEDRIVE_API_TOKEN")
    if not token:
        raise RuntimeError("Missing env PIPEDRIVE_API_TOKEN (should come from Secret Manager)")

    url = f"https://api.pipedrive.com/v1/organizations/{org_id}?api_token={token}"
    r = requests.put(url, json=update, timeout=30)

    ctx["step_05"] = {
        "ok": (r.status_code == 200),
        "status_code": r.status_code,
        "response_preview": r.text[:300],
    }

    if r.status_code != 200:
        raise RuntimeError(f"Pipedrive update failed {r.status_code}: {r.text[:300]}")
