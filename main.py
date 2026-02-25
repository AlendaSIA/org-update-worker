import base64
import json
import os
import requests
import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional, Union

from fastapi import FastAPI, Request

app = FastAPI()

PAYTRAQ_BASE_URL = os.getenv("PAYTRAQ_BASE_URL", "https://go.paytraq.com").rstrip("/")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/debug/env")
def debug_env():
    """
    Safe env presence check (NEVER returns secret values).
    Useful to confirm Cloud Run has Secret Manager -> env wired correctly.
    """
    def has(name: str) -> bool:
        v = os.getenv(name)
        return bool(v and str(v).strip())

    return {
        "ok": True,
        "env": {
            "PAYTRAQ_BASE_URL": PAYTRAQ_BASE_URL,
            "has_PAYTRAQ_API_KEY": has("PAYTRAQ_API_KEY"),
            "has_PAYTRAQ_API_TOKEN": has("PAYTRAQ_API_TOKEN"),
            # optional, for later steps
            "has_PIPEDRIVE_API_TOKEN": has("PIPEDRIVE_API_TOKEN"),
            "has_PIPEDRIVE_BASE_URL": has("PIPEDRIVE_BASE_URL"),
        },
    }


def _paytraq_auth() -> Dict[str, str]:
    key = os.getenv("PAYTRAQ_API_KEY")
    token = os.getenv("PAYTRAQ_API_TOKEN")
    if not key or not token:
        raise RuntimeError("Missing env PAYTRAQ_API_KEY or PAYTRAQ_API_TOKEN")
    return {"APIKey": key, "APIToken": token}


def _fetch_sale_xml(document_id: int) -> str:
    url = f"{PAYTRAQ_BASE_URL}/api/sale/{document_id}"
    r = requests.get(url, params=_paytraq_auth(), timeout=40)
    print(f"PAYTRAQ GET {url} -> {r.status_code} (len={len(r.text or '')})")
    r.raise_for_status()
    return r.text or ""


def _xt(root: ET.Element, path: str) -> Optional[str]:
    el = root.find(path)
    if el is None or el.text is None:
        return None
    s = el.text.strip()
    return s or None


def _extract_from_sale_xml(xml: str) -> Dict[str, Optional[str]]:
    if not isinstance(xml, str) or not xml.strip().startswith("<"):
        return {"parse_error": "not_xml"}

    try:
        root = ET.fromstring(xml)
    except Exception as e:
        return {"parse_error": f"xml_parse_error:{type(e).__name__}"}

    # PayTraq <Sale><Header><Document>...
    doc_id = _xt(root, ".//Header/Document/DocumentID")
    doc_ref = _xt(root, ".//Header/Document/DocumentRef")
    doc_status = _xt(root, ".//Header/Document/DocumentStatus")

    client_id = _xt(root, ".//Header/Document/Client/ClientID")
    client_name = _xt(root, ".//Header/Document/Client/ClientName")

    # Source ref: OrderReference/DocumentLink/DocumentRef
    source_doc_ref = _xt(root, ".//OrderReference/DocumentLink/DocumentRef")

    return {
        "doc_id": doc_id,
        "doc_ref": doc_ref,
        "doc_status": doc_status,
        "client_id": client_id,
        "client_name": client_name,
        "source_doc_ref": source_doc_ref,
        "parse_error": None,
    }


def _decode_pubsub_message(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Accepts Pub/Sub push body:
      {"message": {"data": "<base64>"}}
    Returns decoded JSON dict, or None if not present/invalid.
    """
    msg = body.get("message") if isinstance(body, dict) else None
    if not isinstance(msg, dict):
        return None

    data_b64 = msg.get("data")
    if not data_b64 or not isinstance(data_b64, str):
        return None

    try:
        raw = base64.b64decode(data_b64).decode("utf-8")
        decoded = json.loads(raw)
        return decoded if isinstance(decoded, dict) else None
    except Exception:
        return None


def _normalize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    deal_id = payload.get("deal_id")
    document_id = payload.get("document_id")
    try:
        document_id_int = int(document_id) if document_id is not None else None
    except Exception:
        document_id_int = None

    return {
        "deal_id": deal_id,
        "document_id": document_id_int,
        "raw": payload,
    }


@app.post("/debug")
async def debug_direct(request: Request):
    """
    Direct test endpoint (no Pub/Sub wrapper):
      POST /debug  {"deal_id":66889,"document_id":15692758}
    """
    body = await request.json()
    if not isinstance(body, dict):
        return {"ok": False, "error": "body_not_object"}

    norm = _normalize_payload(body)
    if not norm["document_id"]:
        return {"ok": False, "error": "no_document_id", "payload": norm}

    try:
        xml = _fetch_sale_xml(int(norm["document_id"]))
        extracted = _extract_from_sale_xml(xml)
    except Exception as e:
        print("ORG-UPDATE error:", type(e).__name__, str(e)[:300])
        return {"ok": False, "error": f"{type(e).__name__}", "detail": str(e)[:300], "payload": norm}

    return {"ok": True, "payload": {"deal_id": norm["deal_id"], "document_id": norm["document_id"]}, "paytraq": extracted}


@app.post("/")
async def pubsub_handler(request: Request):
    """
    Pub/Sub push handler (CloudEvent). Expects body.message.data base64(JSON).
    """
    body = await request.json()
    payload = _decode_pubsub_message(body)

    if not payload:
        print("ORG-UPDATE: no/invalid message.data")
        return {"ok": True, "skipped": "no_or_invalid_message_data"}

    norm = _normalize_payload(payload)
    print("ORG-UPDATE payload:", norm)

    if not norm["document_id"]:
        return {"ok": False, "error": "no_document_id", "payload": norm}

    try:
        xml = _fetch_sale_xml(int(norm["document_id"]))
        extracted = _extract_from_sale_xml(xml)
    except Exception as e:
        print("ORG-UPDATE error:", type(e).__name__, str(e)[:300])
        return {"ok": False, "error": f"{type(e).__name__}", "detail": str(e)[:300], "payload": norm}

    return {
        "ok": True,
        "payload": {"deal_id": norm["deal_id"], "document_id": norm["document_id"]},
        "paytraq": extracted,
    }
