from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import runner

app = FastAPI()

@app.get("/health")
async def health():
    return runner.health()

@app.post("/")
async def handle(request: Request):
    body = await request.json()
    try:
        return runner.run(body)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
