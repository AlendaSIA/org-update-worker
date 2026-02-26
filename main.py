import os
import base64
import json
from datetime import date, timedelta
from xml.etree import ElementTree as ET

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

PAYTRAQ_BASE_URL = os.getenv("PAYTRAQ_BASE_URL", "https://go.paytraq.com")
PAYTRAQ_API_KEY = os.getenv("PAYTRAQ_API_KEY")
PAYTRAQ_API_TOKEN = os.getenv("PAYTRAQ_API_TOKEN")

# (Ir pieslēgts kā secret, bet pagaidām šajā solī to nelietojam)
PIPEDRIVE_API_TOKEN = os.getenv("PIPEDRIVE_API_TOKEN")

app = FastAPI()


@app.get("/health")
async def health():
    # nerādām vērtības, tikai set=True/False
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


def fetch_client_id_from_sale(document_id: str) -> str | None:
    """
    Ja payloadā nav client_id, tad ņemam to no PayTraq sale/{document_id}.
    XML piem.: <Client><ClientID>864973</ClientID>...
    """
    _require_env()

    url = f"{PAYTRAQ_BASE_URL}/api/sale/{document_id}"
    params = {
        "APIKey": PAYTRAQ_API_KEY,
        "APIToken": PAYTRAQ_API_TOKEN,
    }

    r = requests.get(url, params=params, timeout=30)
    ct = (r.headers.get("content-type") or "").lower()
    print(f"PAYTRAQ /api/sale/{document_id} -> status={r.status_code} content-type={ct} len={len(r.text)}")

    if r.status_code != 200:
        print("PAYTRAQ sale body(first500):", r.text[:500])
        r.raise_for_status()

    # PayTraq sale response is XML
    root = ET.fromstring(r.text)

    # Mēģinām tiešo ceļu
    node = root.find(".//Client/ClientID")
    if node is not None and node.text and node.text.strip():
        cid = node.text.strip()
        print(f"PAYTRAQ sale -> found ClientID via .//Client/ClientID = {cid}")
        return cid

    # Fallback: jebkurš ClientID tag
    for el in root.iter():
        tag = (str(el.tag) or "").lower()
        if tag.endswith("clientid") and el.text and el.text.strip():
            cid = el.text.strip()
            print(f"PAYTRAQ sale -> found ClientID via iter(*ClientID) = {cid}")
            return cid

    # Ja nav atrasts — izdrukājam sākumu, lai redzam struktūru
    print("PAYTRAQ sale -> ClientID NOT found. XML head(first1200):")
    print(r.text[:1200])
    return None


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
        print(f"PAYTRAQ /api/sales -> status={r.status_code} content-type={ct} len={len(r.text)} page={page}")

        if r.status_code != 200:
            print("PAYTRAQ body(first300):", r.text[:300])
            r.raise_for_status()

        root = ET.fromstring(r.text)
        items = list(root) if root is not None else []
        if not items:
            break

        out.extend(items)

        # vienkāršs stop nosacījums
        if len(items) < 100:
            break

        page += 1

    print(f"PAYTRAQ /api/sales -> total items collected: {len(out)}")
    return out


def compute_total_365d(sales_items) -> float:
    total = 0.0
    for it in sales_items:
        try:
            node = it.find(".//Total") or it.find(".//SaleTotal") or it.find(".//total")
            if node is None or not node.text:
                continue
            total += float(node.text.strip().replace(",", "."))
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

    # ✅ JA client_id nav payloadā, paņemam no sale/{document_id}
    if not client_id and document_id:
        try:
            client_id = fetch_client_id_from_sale(str(document_id))
        except Exception as e:
            print("PAYTRAQ sale fetch error:", str(e))
            return JSONResponse(
                {"ok": False, "error": "paytraq_sale_failed", "detail": str(e), "document_id": document_id},
                status_code=500,
            )

    if not client_id:
        return {"ok": True, "skipped": "no_client_id", "payload": payload, "document_id": document_id}

    # Tagad jau varam rēķināt 365d
    try:
        sales = list_sales_client_365d(str(client_id))
        total_365d = compute_total_365d(sales)
        print(f"ORG-UPDATE total_365d for client_id={client_id}: {total_365d}")
    except Exception as e:
        print("PAYTRAQ error:", str(e))
        return JSONResponse({"ok": False, "error": "paytraq_failed", "detail": str(e)}, status_code=500)

    # Šajā solī vēl neupdeitojam Pipedrive (lai izolēti notestētu PayTraq plūsmu)
    return {
        "ok": True,
        "client_id": str(client_id),
        "document_id": document_id,
        "deal_id": deal_id,
        "total_365d": total_365d,
        "sales_count": len(sales),
    }
