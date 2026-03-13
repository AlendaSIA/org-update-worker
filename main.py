import traceback

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import runner

app = FastAPI()


@app.get("/health")
async def health():
    return runner.health()


@app.post("/")
async def handle(request: Request):
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"invalid_json: {e}"},
            status_code=400,
        )

    try:
        result = runner.run(body)
        return JSONResponse(result, status_code=200)
    except Exception as e:
        print("ORG-UPDATE ERROR:", str(e))
        print(traceback.format_exc())
        return JSONResponse(
            {"ok": False, "error": str(e)},
            status_code=500,
        )
