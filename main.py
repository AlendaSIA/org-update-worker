from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from threading import Thread
import traceback
import runner

app = FastAPI()


@app.get("/health")
async def health():
    return runner.health()


def _run_in_background(body: dict):
    try:
        result = runner.run(body)
        print("org-update-worker DONE:", result)
    except Exception as e:
        print("org-update-worker ERROR:", str(e))
        print(traceback.format_exc())


@app.post("/")
async def handle(request: Request):
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"invalid_json: {e}"}, status_code=400)

    try:
        Thread(target=_run_in_background, args=(body,), daemon=True).start()
        return JSONResponse({"ok": True, "accepted": True}, status_code=200)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
