import os, base64, json, time, importlib
from datetime import date, timedelta
from typing import Any, Dict, List
from xml.etree import ElementTree as ET

import requests

PAYTRAQ_BASE_URL = os.getenv("PAYTRAQ_BASE_URL", "https://go.paytraq.com").rstrip("/")
PAYTRAQ_API_KEY = os.getenv("PAYTRAQ_API_KEY")
PAYTRAQ_API_TOKEN = os.getenv("PAYTRAQ_API_TOKEN")
PIPEDRIVE_API_TOKEN = os.getenv("PIPEDRIVE_API_TOKEN")


def health():
    # Health MUST NOT crash. Never return secrets, only booleans.
    return {
        "ok": True,
        "env": {
            "PAYTRAQ_BASE_URL": PAYTRAQ_BASE_URL,
            "PAYTRAQ_API_KEY_set": bool(PAYTRAQ_API_KEY),
            "PAYTRAQ_API_TOKEN_set": bool(PAYTRAQ_API_TOKEN),
            "PIPEDRIVE_API_TOKEN_set": bool(PIPEDRIVE_API_TOKEN),
        },
    }


def _require_env():
    missing = []
    if not PAYTRAQ_API_KEY:
        missing.append("PAYTRAQ_API_KEY")
    if not PAYTRAQ_API_TOKEN:
        missing.append("PAYTRAQ_API_TOKEN")
    if not PIPEDRIVE_API_TOKEN:
        missing.append("PIPEDRIVE_API_TOKEN")
    if missing:
        raise RuntimeError("Missing env vars: " + ", ".join(missing))


def _safe_b64decode(s: str) -> bytes:
    s = (s or "").strip()
    pad = (-len(s)) % 4
    if pad:
        s += "=" * pad
    return base64.b64decode(s)


def decode_event_to_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    # allow direct payload testing
    if any(k in body for k in ("document_id", "deal_id", "client_id", "org_id", "date_from", "date_till", "dry_run")):
        return body

    msg = body.get("message") or {}
    data_b64 = (msg.get("data") if isinstance(msg, dict) else None)
    if not data_b64:
        return {}
    raw = _safe_b64decode(str(data_b64)).decode("utf-8", errors="replace")
    return json.loads(raw)


def fetch_client_id_from_sale(document_id: str) -> str:
    _require_env()
    r = requests.get(
        f"{PAYTRAQ_BASE_URL}/api/sale/{document_id}",
        params={"APIKey": PAYTRAQ_API_KEY, "APIToken": PAYTRAQ_API_TOKEN},
        timeout=30,
    )
    if r.status_code != 200:
        r.raise_for_status()
    root = ET.fromstring(r.text)
    node = root.find(".//Client/ClientID")
    if node is not None and node.text and node.text.strip():
        return node.text.strip()
    raise RuntimeError("ClientID not found in sale XML")


def list_sales_client_range(client_id: str, d_from: date, d_to: date):
    _require_env()
    out = []
    page = 0
    while True:
        r = requests.get(
            f"{PAYTRAQ_BASE_URL}/api/sales",
            params={
                "APIKey": PAYTRAQ_API_KEY,
                "APIToken": PAYTRAQ_API_TOKEN,
                "ClientID": client_id,
                "date_from": d_from.isoformat(),
                "date_till": d_to.isoformat(),
                "page": page,
            },
            timeout=30,
        )
        if r.status_code != 200:
            r.raise_for_status()
        root = ET.fromstring(r.text)
        sales = root.findall(".//Sale")
        if not sales:
            break
        out.extend(sales)
        page += 1
        if page > 200:
            break
    return out


def _parse_float(t):
    if not t:
        return None
    try:
        return float(str(t).strip().replace(",", "."))
    except Exception:
        return None


def compute_total_12m(sales_items):
    total = 0.0
    for sale in sales_items:
        ref = (sale.findtext(".//Header/Document/DocumentRef") or sale.findtext(".//DocumentRef") or "").strip()
        if ref and not ref.startswith("ALE"):
            continue
        for c in (sale.findtext(".//Header/Total"), sale.findtext(".//Total")):
            v = _parse_float(c)
            if v is not None:
                total += v
                break
    return round(total, 2)


def extract_refs(sales_items, limit=20):
    out = []
    for sale in sales_items:
        doc = sale.find(".//Header/Document") or sale.find(".//Document")
        if doc is None:
            continue
        d = (doc.findtext("DocumentDate") or "").strip()
        ref = (doc.findtext("DocumentRef") or "").strip()
        if ref and not ref.startswith("ALE"):
            continue
        out.append(f"{d} | {ref}")
        if len(out) >= limit:
            break
    return out


def _run_step(ctx, name, fn):
    t0 = time.time()
    try:
        fn(ctx)
        ctx["_trace"].append({"step": name, "ok": True, "ms": int((time.time() - t0) * 1000)})
    except Exception as e:
        ctx["_trace"].append({"step": name, "ok": False, "ms": int((time.time() - t0) * 1000), "error": str(e)})
        raise


def _discover_steps() -> List[str]:
    return [
        "steps.step_01_parse_event",
        "steps.step_02_fetch_client_id",
        "steps.step_03_compute_12m",
        "steps.step_04_build_update",
        "steps.step_05_update_org",
    ]


def run(body: Dict[str, Any]):
    ctx = {
        "body": body,
        "_trace": [],
        "log": print,

        "decode_event_to_payload": decode_event_to_payload,
        "fetch_client_id_from_sale": fetch_client_id_from_sale,
        "list_sales_client_range": list_sales_client_range,
        "compute_total_12m": compute_total_12m,
        "extract_refs": extract_refs,
        "today": date.today,
        "timedelta": timedelta,

        # aliases (compat)
        "decode_pubsub": decode_event_to_payload,
        "paytraq_fetch_client_id": fetch_client_id_from_sale,
        "paytraq_list_sales": list_sales_client_range,
        "compute_total": compute_total_12m,
    }

    for modname in _discover_steps():
        mod = importlib.import_module(modname)
        _run_step(ctx, modname.split(".")[-1], mod.run)

    if ctx.get("skipped"):
        return {"ok": True, "skipped": ctx["skipped"], "_trace": ctx["_trace"], "payload": ctx.get("payload")}

    # ✅ FIX: include computed metrics in response (safe; does not write to Pipedrive)
    computed = ctx.get("computed") or {
        "orders_count_12m": ctx.get("orders_count_12m"),
        "last_order_date": ctx.get("last_order_date"),
        "avg_days_between_last_orders": ctx.get("avg_days_between_last_orders"),
    }

    return {
        "ok": True,
        "_trace": ctx["_trace"],
        "payload": ctx.get("payload"),
        "document_id": ctx.get("document_id"),
        "deal_id": ctx.get("deal_id"),
        "org_id": ctx.get("org_id"),
        "client_id": str(ctx.get("client_id")),
        "date_from": ctx["date_from"].isoformat(),
        "date_till": ctx["date_till"].isoformat(),
        "sales_count": ctx.get("sales_count"),
        "total_sum": ctx.get("total_sum"),
        "sample_refs": ctx.get("sample_refs"),
        "update": ctx.get("update"),
        "computed": computed,  # <— NEW in response
        "step_05": ctx.get("step_05"),
        "dry_run": bool(ctx.get("dry_run")),
    }
