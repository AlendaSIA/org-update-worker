from typing import Any, Dict

def run(ctx: Dict[str, Any]) -> None:
    if ctx.get("skipped"):
        return

    # ja client_id jau atnāca no worker/pubsub → neko nedaram
    if ctx.get("client_id"):
        return

    doc_id = ctx.get("document_id")
    if not doc_id:
        ctx["skipped"] = "no_document_id"
        return

    ctx["client_id"] = ctx["paytraq_fetch_client_id"](str(doc_id))
    if not ctx.get("client_id"):
        ctx["skipped"] = "no_client_id"
