from typing import Any, Dict
from datetime import date, timedelta

def run(ctx: Dict[str, Any]) -> None:
    body = ctx["body"]
    payload = ctx["decode_pubsub"](body)
    ctx["payload"] = payload

    if not payload:
        ctx["skipped"] = "no_data"
        return

    ctx["deal_id"] = payload.get("deal_id", 0)
    ctx["org_id"] = payload.get("org_id", 0)
    ctx["document_id"] = payload.get("document_id", 0)
    ctx["client_id"] = payload.get("client_id")
    ctx["dry_run"] = bool(payload.get("dry_run", False))

    d_to = date.today()
    d_from = d_to - timedelta(days=365)

    if payload.get("date_from") and payload.get("date_till"):
        try:
            y, m, d = [int(x) for x in str(payload["date_from"]).split("-")]
            d_from = date(y, m, d)
            y, m, d = [int(x) for x in str(payload["date_till"]).split("-")]
            d_to = date(y, m, d)
        except Exception:
            ctx["log"]("WARN bad date_from/date_till; using default 365d")

    ctx["date_from"] = d_from
    ctx["date_till"] = d_to
    ctx["log"](f"payload ids deal_id={ctx['deal_id']} doc_id={ctx['document_id']} client_id={ctx['client_id']} org_id={ctx['org_id']}")
