from typing import Any, Dict

# 12 mēn summa (currency custom field key) – tev tas ir šis
PIPEDRIVE_ORG_FIELD_12M_SUM = "0b79b8878b6eebe6ab289a60a34cd7340b28899b"

def run(ctx: Dict[str, Any]) -> None:
    if ctx.get("skipped"):
        return

    if not ctx.get("org_id"):
        ctx["skipped"] = "no_org_id"
        return

    total_sum = float(ctx.get("total_sum") or 0.0)

    ctx["update"] = {
        PIPEDRIVE_ORG_FIELD_12M_SUM: total_sum
    }
