import base64
import json
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

@app.get("/health")
async def health():
    return {"ok": True}

def _decode_pubsub_push(body: dict) -> dict:
    msg = (body or {}).get("message") or {}
    data_b64 = msg.get("data")
    if not data_b64:
        return {}
    raw = base64.b64decode(data_b64).decode("utf-8", errors="replace")
    return json.loads(raw)

@app.post("/")
async def handle_pubsub(request: Request):
    body = await request.json()
    try:
        payload = _decode_pubsub_push(body)
    except Exception as e:
        print("ORG-UPDATE decode error:", str(e))
        print("RAW body:", body)
        return JSONResponse({"ok": False, "error": "decode_failed"}, status_code=400)

    deal_id = payload.get("deal_id")
    document_id = payload.get("document_id")
    client_id = payload.get("client_id")

    print("ORG-UPDATE payload:", payload)
    print(f"ORG-UPDATE ids: deal_id={deal_id} document_id={document_id} client_id={client_id}")

    return {"ok": True, "payload": payload}
