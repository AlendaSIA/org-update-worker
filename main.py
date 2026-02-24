import base64
import json
import os
import requests
from fastapi import FastAPI, Request

app = FastAPI()

PAYTRAQ_BASE = "https://go.paytraq.com"


@app.get("/health")
def health():
    return {"ok": True}


def get_paytraq_auth():
    return {
        "APIKey": os.environ.get("PAYTRAQ_API_KEY"),
        "APIToken": os.environ.get("PAYTRAQ_API_TOKEN"),
    }


def fetch_sale(document_id: int):
    auth = get_paytraq_auth()
    url = f"{PAYTRAQ_BASE}/api/sale/{document_id}"
    r = requests.get(url, params=auth, timeout=30)
    print("PAYTRAQ fetch status:", r.status_code)
    return r.text


@app.post("/")
async def pubsub_handler(request: Request):
    body = await request.json()
    msg = body.get("message") or {}
    data_b64 = msg.get("data")

    if not data_b64:
        print("No message data")
        return {"ok": True}

    payload = json.loads(base64.b64decode(data_b64).decode("utf-8"))
    print("ORG-UPDATE payload:", payload)

    document_id = payload.get("document_id")
    if not document_id:
        return {"ok": False, "error": "no_document_id"}

    xml = fetch_sale(document_id)

    if "<ClientID>" in xml:
        start = xml.find("<ClientID>") + len("<ClientID>")
        end = xml.find("</ClientID>")
        client_id = xml[start:end]
    else:
        client_id = None

    print("Extracted ClientID:", client_id)

    return {
        "ok": True,
        "document_id": document_id,
        "client_id": client_id,
    }
