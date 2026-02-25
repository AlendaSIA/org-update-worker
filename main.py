import base64
import json
import os
import socket
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request

app = FastAPI()

PAYTRAQ_BASE_URL = os.getenv("PAYTRAQ_BASE_URL", "https://go.paytraq.com").rstrip("/")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/debug/config")
def debug_config():
    # Nerādam vērtības, tikai vai ir pieejami
    return {
        "ok": True,
        "has_PAYTRAQ_API_KEY": bool(os.getenv("PAYTRAQ_API_KEY")),
        "has_PAYTRAQ_API_TOKEN": bool(os.getenv("PAYTRAQ_API_TOKEN")),
        "PAYTRAQ_BASE_URL": PAYTRAQ_BASE_URL,
    }


def _paytraq_auth() -> Dict[str, str]:
    key = os.getenv("PAYTRAQ_API_KEY")
    token = os.getenv("PAYTRAQ_API_TOKEN")
    if not key or not token:
        raise RuntimeError("Missing env PAYTRAQ_API_KEY or PAYTRAQ_API_TOKEN")
    return {"APIKey": key, "APIToken": token}


def _http_get(url: str, params: Dict[str, str], timeout: int = 40) -> str:
    qs = urllib.parse.urlencode(params)
    full_url = f"{url}?{qs}" if qs else url

    req = urllib.request.Request(
        full_url,
        method="GET",
        headers={"User-Agent": "org-update-worker/1.0"},
    )

    # drošības pēc: lai timeout tiešām strādā DNS + connect + read
    socket.setdefaulttimeout(timeout)

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        body = resp.read().decode("utf-8", errors="replace")
        print(f"PAYTRAQ GET {url} -> {status} (len={len(body)})")
        if status >= 300:
            raise RuntimeError(f"PayTraq HTTP {status}: {body[:300]}")
        return body


def _fetch_sale_xml(document_id: int) -> str:
    url = f"{PAYTRAQ_BASE_URL}/api/sale/{document_id}"
    return _http_get(url, _paytraq_auth(), timeout=40) or ""


def _b64_payload(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    msg = body.get("message") or {}
    data_b64 = msg.get("data")
    if not data_b64:
        return None
    raw = base64.b64decode(data_b64).decode("utf-8")
    return json.loads(raw)


def _xt(root: ET.Element, path: str) -> Optional[str]:
    el = root.find(path)
    if el is None or el.text is None:
        return None
    s = el.text.strip()
    return s or None


def _extract_from_sale_xml(xml: str) -> Dict[str, Optional[str]]:
    if not xml.strip().startswith("<"):
        return {"parse_error": "not_xml"}

    root = ET.fromstring(xml)

    doc_id = _xt(root, ".//Header/Document/DocumentID")
    doc_ref = _xt(root, ".//Header/Document/DocumentRef")
    client_id = _xt(root, ".//Header/Document/Client/ClientID")
    client_name = _xt(root, ".//Header/Document/Client/ClientName")

    # Source ref (OrderReference -> DocumentRef)
    source_doc_ref = _xt(root, ".//OrderReference/DocumentLink/DocumentRef")

    return {
        "doc_id": doc_id,
        "doc_ref": doc_ref,
        "client_id": client_id,
        "client_name": client_name,
        "source_doc_ref": source_doc_ref,
        "parse_error": None,
    }


@app.post("/")
async def pubsub_handler(request: Request):
    body = await request.json()

    payload = _b64_payload(body)
    if not payload:
        print("ORG-UPDATE: no message.data")
        return {"ok": True, "skipped": "no_message_data"}

    deal_id = payload.get("deal_id")
    document_id = payload.get("document_id")

    print("ORG-UPDATE payload:", payload)

    if not document_id:
        return {"ok": False, "error": "no_document_id", "payload": payload}

    try:
        xml = _fetch_sale_xml(int(document_id))
        extracted = _extract_from_sale_xml(xml)
    except Exception as e:
        print("ORG-UPDATE error:", type(e).__name__, str(e)[:300])
        return {"ok": False, "error": f"{type(e).__name__}", "detail": str(e)[:300], "payload": payload}

    return {
        "ok": True,
        "payload": {"deal_id": deal_id, "document_id": document_id},
        "paytraq": extracted,
    }
