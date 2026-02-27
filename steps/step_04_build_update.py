from typing import Any, Dict
from field_registry import get_or_create_org_field_key

PIPEDRIVE_ORG_FIELD_12M_SUM = "0b79b8878b6eebe6ab289a60a34cd7340b28899b"

FIELD_AVG_DAYS = "Avg days (last 4)"
FIELD_ORDERS_12M = "Orders count (12m)"
FIELD_LAST_ORDER_DATE = "Last order date"


def run(ctx: Dict[str, Any]) -> None:
    if ctx.get("skipped"):
        return

    if not ctx.get("org_id"):
        ctx["skipped"] = "no_org_id"
        return

    total_sum = float(ctx.get("total_sum") or 0.0)

    update = {
        PIPEDRIVE_ORG_FIELD_12M_SUM: total_sum
    }

    # Resolve/create keys
    key_avg_days = get_or_create_org_field_key(FIELD_AVG_DAYS, "double")
    key_orders_12m = get_or_create_org_field_key(FIELD_ORDERS_12M, "double")
    key_last_order_date = get_or_create_org_field_key(FIELD_LAST_ORDER_DATE, "date")

    if ctx.get("avg_days_between_last_orders") is not None:
        update[key_avg_days] = float(ctx["avg_days_between_last_orders"])

    if ctx.get("orders_count_12m") is not None:
        update[key_orders_12m] = float(ctx["orders_count_12m"])

    if ctx.get("last_order_date"):
        update[key_last_order_date] = str(ctx["last_order_date"])

    ctx["update"] = update

    # 🔥 LABOJUMS: nepārrakstām computed, bet papildinām
    existing = ctx.get("computed", {})

    existing.update({
        "sales_count": ctx.get("sales_count"),
        "total_sum": ctx.get("total_sum"),
        "orders_count_12m": ctx.get("orders_count_12m"),
        "last_order_date": ctx.get("last_order_date"),
        "avg_days_between_last_orders": ctx.get("avg_days_between_last_orders"),
    })

    ctx["computed"] = existing
