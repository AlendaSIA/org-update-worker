from typing import Any, Dict

def run(ctx: Dict[str, Any]) -> None:
    if ctx.get("skipped"):
        return

    # ja client_id jau atnāca no worker/pubsub → neko nedaram
    if ctx.get("client_id"):
        returnfrom typing import Any, Dict
import os
import requests

PIPEDRIVE_API_TOKEN = os.getenv("PIPEDRIVE_API_TOKEN")
PIPEDRIVE_BASE_URL = os.getenv("PIPEDRIVE_BASE_URL", "https://api.pipedrive.com").rstrip("/")


def _parse_int(v):
    try:
        if v is None:
            return None
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return int(v)
        s = str(v).strip()
        if s == "":
            return None
        return int(float(s))
    except Exception:
        return None


def _resolve_org_id_from_deal(deal_id: int) -> int | None:
    # token comes only from env (Cloud Run secrets). Never print it.
    if not PIPEDRIVE_API_TOKEN:
        return None

    r = requests.get(
        f"{PIPEDRIVE_BASE_URL}/v1/deals/{deal_id}",
        params={"api_token": PIPEDRIVE_API_TOKEN},
        timeout=30,
    )
    if r.status_code != 200:
        # don't crash whole pipeline for org_id resolve
        return None

    data = (r.json() or {}).get("data") or {}
    org = data.get("org_id")

    # Pipedrive may return org_id as {"value": 123, ...} or as int-ish
    if isinstance(org, dict):
        return _parse_int(org.get("value"))
    return _parse_int(org)


def run(ctx: Dict[str, Any]) -> None:
    if ctx.get("skipped"):
        return

    payload = ctx.get("payload") or {}

    # 1) always hydrate ids from payload if present
    doc_id = payload.get("document_id", ctx.get("document_id"))
    deal_id = payload.get("deal_id", ctx.get("deal_id"))
    client_id = payload.get("client_id", ctx.get("client_id"))
    org_id = payload.get("org_id", ctx.get("org_id"))

    if doc_id is not None:
        ctx["document_id"] = _parse_int(doc_id) or doc_id
    if deal_id is not None:
        ctx["deal_id"] = _parse_int(deal_id) or deal_id
    if client_id is not None:
        ctx["client_id"] = _parse_int(client_id) or client_id
    if org_id is not None:
        ctx["org_id"] = _parse_int(org_id) or org_id

    # 2) ensure document_id exists
    doc_id_final = ctx.get("document_id")
    if not doc_id_final:
        ctx["skipped"] = "no_document_id"
        return

    # 3) fetch client_id from PayTraq only if missing
    if not ctx.get("client_id"):
        ctx["client_id"] = ctx["paytraq_fetch_client_id"](str(doc_id_final))
        if not ctx.get("client_id"):
            ctx["skipped"] = "no_client_id"
            return

    # 4) resolve org_id from deal if missing/zero
    org_id_final = _parse_int(ctx.get("org_id"))
    deal_id_final = _parse_int(ctx.get("deal_id"))

    if (not org_id_final or org_id_final == 0) and deal_id_final:
        resolved = _resolve_org_id_from_deal(deal_id_final)
        if resolved and resolved != 0:
            ctx["org_id"] = resolved

    doc_id = ctx.get("document_id")
    if not doc_id:
        ctx["skipped"] = "no_document_id"
        return

    ctx["client_id"] = ctx["paytraq_fetch_client_id"](str(doc_id))
    if not ctx.get("client_id"):
        ctx["skipped"] = "no_client_id"
