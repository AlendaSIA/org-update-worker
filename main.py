import os
import base64
import json
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# ----------------------------
# ENV
# ----------------------------
PAYTRAQ_BASE_URL = os.getenv("PAYTRAQ_BASE_URL", "https://go.paytraq.com").rstrip("/")
PAYTRAQ_API_KEY = os.getenv("PAYTRAQ_API_KEY")
PAYTRAQ_API_TOKEN = os.getenv("PAYTRAQ_API_TOKEN")
PIPEDRIVE_API_TOKEN = os.getenv("PIPEDRIVE_API_TOKEN")  # vēl nelietojam šajā versijā

app = FastAPI()


@app.get("/health")
async def health():
    return {
        "ok": True,
        "env": {
            "PAYTRAQ_BASE_URL": PAYTRAQ_BASE_URL,
            "PAYTRAQ_API_KEY_set": bool(PAYTRAQ_API_KEY),
            "PAYTRAQ_API_TOKEN_set": bool(PAYTRAQ_API_TOKEN),
            "PIPEDRIVE_API_TOKEN_set": bool(PIPEDRIVE_API_TOKEN),
        },
    }


# ----------------------------
# UTILS
# ----------------------------
def _require_env():
    missing = [k for k, v in {
        "PAYTRAQ_API_KEY": PAYTRAQ_API_KEY,
        "PAYTRAQ_API_TOKEN": PAYTRAQ_API_TOKEN,
    }.items() if not v]
    if missing:
        raise RuntimeError("Missing env vars: " + ", ".join(missing))


def _safe_b64decode(s: str) -> bytes:
    s = (s or "").strip()
    pad = (-len(s)) % 4
    if pad:
        s = s + ("=" * pad)
    return base64.b64decode(s)


def decode_event_to_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pieņem:
    1) Pub/Sub push: {"message":{"data":"base64(json)"}, ...}
    2) Tiešu JSON payload testam: {"deal_id":..,"document_id":..}
    """
    if not isinstance(body, dict):
        return {}

    # direct test JSON
    if any(k in body for k in ("document_id", "deal_id", "client_id", "org_id", "date_from", "date_till", "dry_run")):
        return body

    # pubsub push
    msg = (body.get("message") or {}) if isinstance(body.get("message"), dict) else {}
    data_b64 = msg.get("data")
    if not data_b64:
        return {}

    raw = _safe_b64decode(str(data_b64)).decode("utf-8", errors="replace")
    return json.loads(raw)


def _parse_float(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    try:
        return float(text.strip().replace(",", "."))
    except Exception:
        return None


# ----------------------------
# PAYTRAQ
# ----------------------------
def fetch_client_id_from_sale(document_id: str) -> Optional[str]:
    _require_env()
    url = f"{PAYTRAQ_BASE_URL}/api/sale/{document_id}"
    params = {"APIKey": PAYTRAQ_API_KEY, "APIToken": PAYTRAQ_API_TOKEN}

    r = requests.get(url, params=params, timeout=30)
    ct = (r.headers.get("content-type") or "").lower()
    print(f"PAYTRAQ /api/sale/{document_id} -> status={r.status_code} ct={ct} len={len(r.text)}")

    if r.status_code != 200:
        print("PAYTRAQ sale body(first500):", r.text[:500])
        r.raise_for_status()

    root = ET.fromstring(r.text)

    node = root.find(".//Client/ClientID")
    if node is not None and node.text and node.text.strip():
        return node.text.strip()

    # fallback
    for el in root.iter():
        tag = (str(el.tag) or "").lower()
        if tag.endswith("clientid") and el.text and el.text.strip():
            return el.text.strip()

    print("PAYTRAQ sale -> ClientID NOT found. XML head(first1200):")
    print(r.text[:1200])
    return None


def list_sales_client_range(client_id: str, d_from: date, d_to: date) -> List[ET.Element]:
    """
    SVARĪGI: paging sākas ar page=0
    """
    _require_env()

    url = f"{PAYTRAQ_BASE_URL}/api/sales"
    page = 0
    out: List[ET.Element] = []

    print(f"PAYTRAQ /api/sales RANGE client_id={client_id} date_from={d_from.isoformat()} date_till={d_to.isoformat()}")

    while True:
        params = {
            "APIKey": PAYTRAQ_API_KEY,
            "APIToken": PAYTRAQ_API_TOKEN,
            "ClientID": client_id,
            "date_from": d_from.isoformat(),
            "date_till": d_to.isoformat(),
            "page": page,
        }

        r = requests.get(url, params=params, timeout=30)
        ct = (r.headers.get("content-type") or "").lower()
        print(f"PAYTRAQ /api/sales -> status={r.status_code} ct={ct} len={len(r.text)} page={page}")

        if r.status_code != 200:
            print("PAYTRAQ sales body(first500):", r.text[:500])
            r.raise_for_status()

        root = ET.fromstring(r.text)
        sales = root.findall(".//Sale")
        print(f"PAYTRAQ /api/sales -> found Sale nodes: {len(sales)} on page={page}")

        if not sales:
            break

        out.extend(sales)

        if page >= 200:
            print("WARN: paging safety stop at page=200")
            break
        page += 1

    print(f"PAYTRAQ /api/sales -> total Sale nodes collected: {len(out)}")
    return out


def compute_total_12m(sales_items: List[ET.Element], only_ale: bool = True) -> float:
    total = 0.0
    for sale in sales_items:
        try:
            if only_ale:
                ref = (sale.findtext(".//Header/Document/DocumentRef") or sale.findtext(".//DocumentRef") or "").strip()
                if ref and not ref.startswith("ALE"):
                    continue

            candidates = [
                sale.findtext(".//Header/Total"),
                sale.findtext(".//Header/Document/Total"),
                sale.findtext(".//Total"),
            ]
            val = None
            for c in candidates:
                val = _parse_float(c)
                if val is not None:
                    break
            if val is None:
                continue

            total += val
        except Exception:
            pass
    return round(total, 2)


def extract_refs(sales_items: List[ET.Element], limit: int = 20, only_ale: bool = True) -> List[str]:
    out: List[str] = []
    for sale in sales_items:
        doc = sale.find(".//Header/Document") or sale.find(".//Document")
        if doc is None:
            continue

        d = (doc.findtext("DocumentDate") or "").strip()
        ref = (doc.findtext("DocumentRef") or "").strip()

        if only_ale and ref and not ref.startswith("ALE"):
            continue

        if d or ref:
            out.append(f"{d} | {ref}")
        if len(out) >= limit:
            break
    return out


# ----------------------------
# STEP RUNNER
# ----------------------------
def _run_step(ctx: Dict[str, Any], name: str, fn):
    t0 = time.time()
    try:
        fn(ctx)
        ms = int((time.time() - t0) * 1000)
        ctx["_trace"].append({"step": name, "ok": True, "ms": ms})
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        ctx["_trace"].append({"step": name, "ok": False, "ms": ms, "error": str(e)})
        raise


# ----------------------------
# STEPS (tagad vienā failā; vēlāk var 1:1 iznest atsevišķos failos)
# ----------------------------
def step_01_parse_event(ctx: Dict[str, Any]) -> None:
    payload = decode_event_to_payload(ctx["body"])
    ctx["payload"] = payload

    if not payload:
        ctx["skipped"] = "no_data"
        return

    # ids
    ctx["deal_id"] = payload.get("deal_id", 0)
    ctx["org_id"] = payload.get("org_id", 0)
    ctx["document_id"] = payload.get("document_id", 0)
    ctx["client_id"] = payload.get("client_id")  # var būt None
    ctx["dry_run"] = bool(payload.get("dry_run", False))

    # dates
    d_to = date.today()
    d_from = d_to - timedelta(days=365)
    if payload.get("date_from") and payload.get("date_till"):
        try:
            y, m, d = [int(x) for x in str(payload["date_from"]).split("-")]
            d_from = date(y, m, d)
            y, m, d = [int(x) for x in str(payload["date_till"]).split("-")]
            d_to = date(y, m, d)
        except Exception:
            print("WARN: bad date_from/date_till; using default 365d")

    ctx["date_from"] = d_from
    ctx["date_till"] = d_to

    print("ORG-UPDATE payload:", payload)
    print(f"ORG-UPDATE ids: deal_id={ctx['deal_id']} document_id={ctx['document_id']} client_id={ctx['client_id']} org_id={ctx['org_id']}")


def step_02_fetch_client_id(ctx: Dict[str, Any]) -> None:
    if ctx.get("skipped"):
        return

    # Ja client_id jau atnāca no worker/pubsub -> neko nedaram
    if ctx.get("client_id"):
        return

    doc_id = ctx.get("document_id")
    if not doc_id:
        ctx["skipped"] = "no_document_id"
        return

    ctx["client_id"] = fetch_client_id_from_sale(str(doc_id))
    if not ctx.get("client_id"):
        ctx["skipped"] = "no_client_id"


def step_03_compute_12m(ctx: Dict[str, Any]) -> None:
    if ctx.get("skipped"):
        return

    sales = list_sales_client_range(str(ctx["client_id"]), ctx["date_from"], ctx["date_till"])
    ctx["sales_count"] = len(sales)
    ctx["total_sum"] = compute_total_12m(sales, only_ale=True)
    ctx["sample_refs"] = extract_refs(sales, limit=20, only_ale=True)

    print(f"ORG-UPDATE computed: client_id={ctx['client_id']} total_sum={ctx['total_sum']} sales_count={ctx['sales_count']}")


def run_steps(ctx: Dict[str, Any]) -> None:
    # Te ir “step modelis”. Pievienojot nākamo soli, pieliec vēl vienu _run_step rindu.
    _run_step(ctx, "01_parse_event", step_01_parse_event)
    _run_step(ctx, "02_fetch_client_id", step_02_fetch_client_id)
    _run_step(ctx, "03_compute_12m", step_03_compute_12m)


@app.post("/")
async def handle_pubsub(request: Request):
    body = await request.json()
    ctx: Dict[str, Any] = {"body": body, "_trace": []}

    try:
        run_steps(ctx)
    except Exception as e:
        return JSONResponse(
            {
                "ok": False,
                "error": "step_failed",
                "detail": str(e),
                "_trace": ctx["_trace"],
                "payload": ctx.get("payload"),
            },
            status_code=500,
        )

    if ctx.get("skipped"):
        return {
            "ok": True,
            "skipped": ctx["skipped"],
            "_trace": ctx["_trace"],
            "payload": ctx.get("payload"),
            "document_id": ctx.get("document_id"),
            "deal_id": ctx.get("deal_id"),
            "org_id": ctx.get("org_id"),
        }

    return {
        "ok": True,
        "_trace": ctx["_trace"],
        "client_id": str(ctx["client_id"]),
        "document_id": ctx.get("document_id"),
        "deal_id": ctx.get("deal_id"),
        "org_id": ctx.get("org_id"),
        "date_from": ctx["date_from"].isoformat(),
        "date_till": ctx["date_till"].isoformat(),
        "sales_count": ctx.get("sales_count"),
        "total_sum": ctx.get("total_sum"),
        "sample_refs": ctx.get("sample_refs"),
    }
