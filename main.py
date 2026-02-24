import base64, json
from fastapi import FastAPI, Request

app = FastAPI()

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/")
async def pubsub_handler(request: Request):
    body = await request.json()
    msg = body.get("message") or {}
    data_b64 = msg.get("data")
    if not data_b64:
        print("WARN: no message.data; body=", body)
        return {"ok": True, "skipped": "no_data"}
    payload = json.loads(base64.b64decode(data_b64).decode("utf-8"))
    print("ORG-UPDATE payload:", payload)
    return {"ok": True, "payload": payload}
