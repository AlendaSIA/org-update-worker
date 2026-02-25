from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional, Any, Dict
import os

from runner import Runner
from steps import step_01_deal_upsert
from steps import step_02_parse_line_items
from steps import step_03_products_sync

# NEW stub steps
from steps import step_04_find_update_organization
from steps import step_05_find_update_person
from steps import step_99_update_products

app = FastAPI()


class ClientIn(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


class DealIn(BaseModel):
    title: str
    pipeline_id: Optional[int] = None
    stage_id: Optional[int] = None
    value: Optional[float] = None
    currency: Optional[str] = "EUR"


class DocumentIn(BaseModel):
    id: int
    client: Optional[ClientIn] = None
    deal: DealIn
    meta: Optional[Dict[str, Any]] = None
    paytraq_full_xml: Optional[str] = None


class ProcessIn(BaseModel):
    document: DocumentIn


class DebugIn(BaseModel):
    document: DocumentIn
    mode: str  # all | step | until
    step: Optional[str] = None


def _env_bool(name: str, default: str = "true") -> bool:
    v = os.getenv(name, default).strip().lower()
    return v in ("1", "true", "yes", "y", "on")


# Build steps with feature flags (safe)
steps_list = [
    ("01_deal_upsert", step_01_deal_upsert.run),
    ("02_parse_line_items", step_02_parse_line_items.run),
    ("03_products_sync", step_03_products_sync.run),
]

if _env_bool("ENABLE_STEP_04", "true"):
    steps_list.append(("04_find_update_organization", step_04_find_update_organization.run))

if _env_bool("ENABLE_STEP_05", "true"):
    steps_list.append(("05_find_update_person", step_05_find_update_person.run))

if _env_bool("ENABLE_STEP_99", "true"):
    steps_list.append(("99_update_products", step_99_update_products.run))


runner = Runner(steps=steps_list)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/steps")
def list_steps():
    return {"steps": runner.list_steps()}


@app.post("/process")
def process(payload: ProcessIn):
    ctx = {"document": payload.document.model_dump()}
    ctx = runner.run_all(ctx)

    # Saglabā veco trigger contract: deal_id/status/created_title utt. paliek top-level
    resp = dict(ctx.get("result") or {})

    # Pievienojam diagnostiku
    resp["_trace"] = ctx.get("_trace", [])
    resp["document_id"] = (ctx.get("document") or {}).get("id")

    # Existing debug outputs
    resp["step_02_fetch"] = ctx.get("step_02_fetch")
    resp["step_02"] = ctx.get("step_02")
    resp["line_items_count"] = ctx.get("line_items_count")
    resp["line_items_preview"] = ctx.get("line_items_preview")

    resp["step_03"] = ctx.get("step_03")
    resp["products_attached"] = ctx.get("products_attached")
    resp["products_created"] = ctx.get("products_created")
    resp["activities_created"] = ctx.get("activities_created")

    # NEW stub step outputs (will exist if enabled)
    resp["step_04"] = ctx.get("step_04")
    resp["step_05"] = ctx.get("step_05")
    resp["step_99"] = ctx.get("step_99")

    return resp


@app.post("/debug")
def debug(payload: DebugIn):
    ctx = {"document": payload.document.model_dump()}

    if payload.mode == "all":
        return runner.run_all(ctx)

    if payload.mode == "step":
        if not payload.step:
            return {"error": "Missing step"}
        return runner.run_step(ctx, payload.step)

    if payload.mode == "until":
        if not payload.step:
            return {"error": "Missing step"}
        return runner.run_until(ctx, payload.step)

    return {"error": "Unknown mode"}
