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

    # ===== 12M FIELDS =====
    key_avg_days = get_or_create_org_field_key(FIELD_AVG_DAYS, "double")
    key_orders_12m = get_or_create_org_field_key(FIELD_ORDERS_12M, "double")
    key_last_order_date = get_or_create_org_field_key(FIELD_LAST_ORDER_DATE, "date")

    if ctx.get("avg_days_between_last_orders") is not None:
        update[key_avg_days] = float(ctx["avg_days_between_last_orders"])

    if ctx.get("orders_count_12m") is not None:
        update[key_orders_12m] = float(ctx["orders_count_12m"])

    if ctx.get("last_order_date"):
        update[key_last_order_date] = str(ctx["last_order_date"])


    # ===== PG DYNAMIC FIELDS =====
    pg = ctx.get("pg") or {}

    for pg_name, pg_data in pg.items():

        sum_value = pg_data.get("sum")
        date_value = pg_data.get("date")

        field_sum_name = f"PG Sum {pg_name}"
        field_date_name = f"PG Date {pg_name}"

        # ✅ IZMAIŅA: lai PG Sum būtu € lauks (monetary), nevis double
        key_pg_sum = get_or_create_org_field_key(field_sum_name, "monetary")
        key_pg_date = get_or_create_org_field_key(field_date_name, "date")

        # ✅ LABOJUMS — tikai ja > 0
        if sum_value is not None and float(sum_value) > 0:
            update[key_pg_sum] = float(sum_value)

        if date_value:
            update[key_pg_date] = str(date_value)


    ctx["update"] = update

    # ===== Nepārrakstām computed =====
    existing = ctx.get("computed", {})

    existing.update({
        "sales_count": ctx.get("sales_count"),
        "total_sum": ctx.get("total_sum"),
        "orders_count_12m": ctx.get("orders_count_12m"),
        "last_order_date": ctx.get("last_order_date"),
        "avg_days_between_last_orders": ctx.get("avg_days_between_last_orders"),
        "pg": pg
    })

    ctx["computed"] = existing
