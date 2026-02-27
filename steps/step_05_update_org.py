import os
import requests
from typing import Any, Dict

def _same_value(a, b) -> bool:
    if a is None and b is None:
        return True
    # dates / strings
    if isinstance(a, str) or isinstance(b, str):
        return str(a) == str(b)
    # numbers (Pipedrive var dot int, mēs dodam float)
    try:
        return float(a) == float(b)
    except Exception:
        return a == b

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

    # 1) GET current org
    g = requests.get(url, timeout=30)
    if g.status_code != 200:
        raise RuntimeError(f"Pipedrive org GET failed {g.status_code}: {g.text[:300]}")
    org = (g.json().get("data") or {})

    # 2) diff only changed keys
    changed: Dict[str, Any] = {}
    for k, v in update.items():
        if not _same_value(org.get(k), v):
            changed[k] = v

    if not changed:
        ctx["step_05"] = {"ok": True, "skipped": "no_change", "would_update_keys": list(update.keys())}
        return

    # 3) PUT only changed
    r = requests.put(url, json=changed, timeout=30)

    ctx["step_05"] = {
        "ok": (r.status_code == 200),
        "status_code": r.status_code,
        "updated_keys": list(changed.keys()),
        "response_preview": r.text[:300],
    }

    if r.status_code != 200:
        raise RuntimeError(f"Pipedrive update failed {r.status_code}: {r.text[:300]}")
