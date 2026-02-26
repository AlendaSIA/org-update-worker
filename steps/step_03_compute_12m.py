from typing import Any, Dict

def run(ctx: Dict[str, Any]) -> None:
    if ctx.get("skipped"):
        return

    sales = ctx["paytraq_list_sales"](str(ctx["client_id"]), ctx["date_from"], ctx["date_till"])
    ctx["sales_count"] = len(sales)
    ctx["total_sum"] = ctx["compute_total"](sales)
    ctx["sample_refs"] = ctx["extract_refs"](sales, limit=20)
    ctx["log"](f"12m total_sum={ctx['total_sum']} sales_count={ctx['sales_count']}")
