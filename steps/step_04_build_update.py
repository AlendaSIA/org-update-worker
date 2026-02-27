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

    # Pipedrive update (pagaidām tikai esošais 12m sum lauks)
    ctx["update"] = {
        PIPEDRIVE_ORG_FIELD_12M_SUM: total_sum
    }

    # Debug/trace output: computed metrics (NERAKSTA pipedrive, tikai lai redzi dry_run atbildē)
    ctx["computed"] = {
        "sales_count": ctx.get("sales_count"),
        "total_sum": ctx.get("total_sum"),
        "orders_count_12m": ctx.get("orders_count_12m"),
        "last_order_date": ctx.get("last_order_date"),
        "avg_days_between_last_orders": ctx.get("avg_days_between_last_orders"),
    }
