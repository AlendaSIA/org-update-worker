import os
import base64
import json
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
PIPEDRIVE_API_TOKEN = os.getenv("PIPEDRIVE_API_TOKEN")  # šajā solī vēl nelietojam

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


def _require_env():
    missing = [k for k, v in {
        "PAYTRAQ_API_KEY": PAYTRAQ_API_KEY,
        "PAYTRAQ_API_TOKEN": PAYTRAQ_API_TOKEN,
    }.items() if not v]
    if missing:
        raise RuntimeError("Missing env vars: " + ", ".join(missing))


def _safe_b64decode(s: str) -> bytes:
    # Pub/Sub dažreiz atnāk bez padding (=)
    s = (s or "").strip()
    pad = (-len(s)) % 4
    if pad:
        s = s + ("=" * pad)
    return base64.b64decode(s)


def _decode_pubsub_push(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pieņem:
    1) Pub/Sub push formātu: {"message":{"data":"base64(json)"}, ...}
    2) Tiešu JSON payload testam: {"deal_id":..,"document_id":..}
    """
    if not isinstance(body, dict):
        return {}

    # Ja jau ir payload lauki — pieņemam kā tiešo payload
    if any(k in body for k in ("document_id", "deal_id", "client_id", "date_from", "date_till")):
        return body

    msg = (body.get("message") or {}) if isinstance(body.get("message"), dict) else {}
    data_b64 = msg.get("data")
    if not data_b64:
        return {}

    raw = _safe_b64decode(str(data_b64)).decode("utf-8", errors="replace")
    return json.loads(raw)


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
        cid = node.text.strip()
        print(f"PAYTRAQ sale -> ClientID via .//Client/ClientID = {cid}")
        return cid

    for el in root.iter():
        tag = (str(el.tag) or "").lower()
        if tag.endswith("clientid") and el.text and el.text.strip():
            cid = el.text.strip()
            print(f"PAYTRAQ sale -> ClientID via iter(*ClientID) = {cid}")
            return cid

    print("PAYTRAQ sale -> ClientID NOT found. XML head(first1200):")
    print(r.text[:1200])
    return None


def list_sales_client_range(client_id: str, d_from: date, d_to: date) -> List[ET.Element]:
    """
    SVARĪGI:
    - PayTraq paging tavos strādājošajos skriptos sākas ar page=0, nevis 1.
    - Tāpēc šeit page=0 (tas bija galvenais bug).
    """
    _require_env()

    url = f"{PAYTRAQ_BASE_URL}/api/sales"
    page = 0  # <<< FIX: bija 1
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
        print("PAYTRAQ sales xml head(first200):", r.text[:200])

        if r.status_code != 200:
            print("PAYTRAQ sales body(first500):", r.text[:500])
            r.raise_for_status()

        root = ET.fromstring(r.text)
        sales = root.findall(".//Sale")
        print(f"PAYTRAQ /api/sales -> found Sale nodes: {len(sales)} on page={page}")

        if not sales:
            break

        out.extend(sales)

        # PayTraq lapas izmērs nav dokumentēts šeit; tavos skriptos pietika ar "kamēr vēl ir Sale".
        # Drošības bremze: ja kādreiz iestrēgst, pārtraucam pēc 200 lapām.
        if page >= 200:
            print("WARN: paging safety stop at page=200")
            break

        page += 1

    print(f"PAYTRAQ /api/sales -> total Sale nodes collected: {len(out)}")
    return out


def _parse_float(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    try:
        return float(text.strip().replace(",", "."))
    except Exception:
        return None


def compute_total_365d(sales_items: List[ET.Element], only_ale: bool = True) -> float:
    total = 0.0
    for sale in sales_items:
        try:
            # filtrs: tikai ALE, ja vajag
            if only_ale:
                ref = (sale.findtext(".//Header/Document/DocumentRef") or sale.findtext(".//DocumentRef") or "").strip()
                if ref and not ref.startswith("ALE"):
                    continue

            # Total var būt dažādos ceļos atkarībā no PayTraq XML
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


def extract_refs(sales_items: List[ET.Element], limit: int = 50, only_ale: bool = True) -> List[str]:
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
        print("WARN: no payload; body=", body)
        return {"ok": True, "skipped": "no_data"}

    deal_id = payload.get("deal_id")
    document_id = payload.get("document_id")
    client_id = payload.get("client_id")

    # optional override for testing:
    date_from_s = payload.get("date_from")  # "YYYY-MM-DD"
    date_till_s = payload.get("date_till")  # "YYYY-MM-DD"

    print("ORG-UPDATE payload:", payload)
    print(f"ORG-UPDATE ids: deal_id={deal_id} document_id={document_id} client_id={client_id}")

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

    # default: 450 days (buffer)
    d_to = date.today()
    d_from = d_to - timedelta(days=450)

    if date_from_s and date_till_s:
        try:
            y, m, d = [int(x) for x in str(date_from_s).split("-")]
            d_from = date(y, m, d)
            y, m, d = [int(x) for x in str(date_till_s).split("-")]
            d_to = date(y, m, d)
        except Exception:
            print("WARN: bad date_from/date_till in payload; using default 450d")

    try:
        sales = list_sales_client_range(str(client_id), d_from, d_to)
        total_sum = compute_total_365d(sales, only_ale=True)
        refs = extract_refs(sales, limit=20, only_ale=True)
        print(f"ORG-UPDATE total_sum client_id={client_id}: {total_sum}")
    except Exception as e:
        print("PAYTRAQ error:", str(e))
        return JSONResponse({"ok": False, "error": "paytraq_failed", "detail": str(e)}, status_code=500)

    return {
        "ok": True,
        "client_id": str(client_id),
        "document_id": document_id,
        "deal_id": deal_id,
        "date_from": d_from.isoformat(),
        "date_till": d_to.isoformat(),
        "sales_count": len(sales),
        "total_sum": total_sum,
        "sample_refs": refs,
    }
