import os
import base64
import json
from datetime import date, timedelta

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

PAYTRAQ_BASE_URL = os.getenv("PAYTRAQ_BASE_URL", "https://go.paytraq.com")
PAYTRAQ_API_KEY = os.getenv("PAYTRAQ_API_KEY")
PAYTRAQ_API_TOKEN = os.getenv("PAYTRAQ_API_TOKEN")

app = FastAPI()

@app.get("/health")
async def health():
    return {"ok": True}

def _require_env():
    missing = [k for k, v in {
        "PAYTRAQ_API_KEY": PAYTRAQ_API_KEY,
        "PAYTRAQ_API_TOKEN": PAYTRAQ_API_TOKEN,
    }.items() if not v]
    if missing:
        raise RuntimeError("Missing env vars: " + ", ".join(missing))

def _decode_pubsub_push(body: dict) -> dict:
    msg = (body or {}).get("message") or {}
    data_b64 = msg.get("data")
    if not data_b64:
        return {}
    raw = base64.b64decode(data_b64).decode("utf-8", errors="replace")
    return json.loads(raw)

def list_sales_client_365d(client_id: str):
    _require_env()
    d_to = date.today()
    d_from = d_to - timedelta(days=365)

    url = f"{PAYTRAQ_BASE_URL}/api/sales"
    page = 1
    out = []

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
        print(f"PAYTRAQ /api/sales -> status={r.status_code} content-type={ct} len={len(r.text)}")
        if r.status_code != 200:
            print("PAYTRAQ body(first300):", r.text[:300])
            r.raise_for_status()

        # PayTraq reizēm atdod ne-JSON (piem., HTML). Tad ielogojam preview.
        try:
            data = r.json()
        except Exception:
            print("PAYTRAQ non-json body(first300):", r.text[:300])
            raise

        items = None
        if isinstance(data, dict):
            items = data.get("data") or data.get("Data") or data.get("sales") or data.get("Sales")
        elif isinstance(data, list):
            items = data

        if not items:
            break

        out.extend(items)

        # vienkāršs stop nosacījums
        if len(items) < 100:
            break

        page += 1

    return out

def compute_total_365d(sales_items) -> float:
    total = 0.0
    for it in sales_items:
        if not isinstance(it, dict):
            continue
        v = it.get("Total") or it.get("total") or it.get("SaleTotal") or it.get("sale_total")
        if v is None:
            continue
        try:
            total += float(str(v).replace(",", "."))
        except Exception:
            pass
    return round(total, 2)

@app.post("/")
async def handle_pubsub(request: Request):
    body = await request.json()

    try:
        payload = _decode_pubsub_push(body)
    except Exception as e:
        print("ORG-UPDATE decode error:", str(e))
        print("RAW body:", body)
        return JSONResponse({"ok": False, "error": "decode_failed"}, status_code=400)

    if not payload:
        print("WARN: no message.data; body=", body)
        return {"ok": True, "skipped": "no_data"}

    deal_id = payload.get("deal_id")
    document_id = payload.get("document_id")
    client_id = payload.get("client_id")

    print("ORG-UPDATE payload:", payload)
    print(f"ORG-UPDATE ids: deal_id={deal_id} document_id={document_id} client_id={client_id}")

    if not client_id:
        return {"ok": True, "skipped": "no_client_id", "payload": payload}

    try:
        sales = list_sales_client_365d(str(client_id))
        total_365d = compute_total_365d(sales)
    except Exception as e:
        print("PAYTRAQ error:", str(e))
        return JSONResponse({"ok": False, "error": "paytraq_failed", "detail": str(e)}, status_code=500)

    print(f"BUY_TOTAL_365D client_id={client_id} total_365d={total_365d} sales_count={len(sales)}")

    return {
        "ok": True,
        "deal_id": deal_id,
        "document_id": document_id,
        "client_id": client_id,
        "buy_total_365d": total_365d,
        "sales_count_365d": len(sales),
    }
