from typing import Any, Dict, List, Optional
from datetime import datetime, date

def _to_date(v: Any) -> Optional[date]:
    """
    Convert various date representations to datetime.date.
    Accepts:
      - "YYYY-MM-DD"
      - ISO strings "YYYY-MM-DDTHH:MM:SS" (optionally with trailing Z)
      - datetime/date objects
    """
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        # normalize Z
        s = s.replace("Z", "")
        # try isoformat first
        try:
            return datetime.fromisoformat(s).date()
        except Exception:
            pass
        # try YYYY-MM-DD fallback
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except Exception:
            return None
    return None


def _extract_sale_date(sale: Any) -> Optional[date]:
    """
    Best-effort extraction of document date from a PayTraq sale item.
    Since exact schema isn't provided, we try common keys.
    """
    if isinstance(sale, dict):
        # Common candidates (add/adjust once we see actual payload)
        for k in (
            "DocumentDate",
            "document_date",
            "date",
            "Date",
            "SaleDate",
            "sale_date",
        ):
            if k in sale:
                d = _to_date(sale.get(k))
                if d:
                    return d

        # Sometimes nested structures exist
        # e.g. {"Header":{"Document":{"DocumentDate":"..."}}}
        header = sale.get("Header") or sale.get("header")
        if isinstance(header, dict):
            doc = header.get("Document") or header.get("document")
            if isinstance(doc, dict):
                d = _to_date(doc.get("DocumentDate") or doc.get("document_date") or doc.get("date"))
                if d:
                    return d

    # If sale isn't dict or no date found
    return None


def _compute_order_metrics_from_sales(sales: List[Any]) -> Dict[str, Any]:
    dates: List[date] = []
    for s in sales:
        d = _extract_sale_date(s)
        if d:
            dates.append(d)

    if not dates:
        return {
            "orders_count_12m": 0,
            "last_order_date": None,
            "avg_days_between_last_orders": None,
        }

    dates.sort()  # oldest -> newest
    orders_count = len(dates)
    last_order_date = dates[-1].isoformat()

    last4 = dates[-4:]
    if len(last4) <= 1:
        avg_days = 1
    else:
        diffs = [(last4[i] - last4[i - 1]).days for i in range(1, len(last4))]
        # defensive: avoid division by zero though it shouldn't happen
        avg_days = round(sum(diffs) / max(len(diffs), 1), 2)

    return {
        "orders_count_12m": orders_count,
        "last_order_date": last_order_date,
        "avg_days_between_last_orders": avg_days,
    }


def run(ctx: Dict[str, Any]) -> None:
    if ctx.get("skipped"):
        return

    sales = ctx["paytraq_list_sales"](str(ctx["client_id"]), ctx["date_from"], ctx["date_till"])

    ctx["sales_count"] = len(sales)
    ctx["total_sum"] = ctx["compute_total"](sales)
    ctx["sample_refs"] = ctx["extract_refs"](sales, limit=20)

    metrics = _compute_order_metrics_from_sales(sales)
    ctx["orders_count_12m"] = metrics["orders_count_12m"]
    ctx["last_order_date"] = metrics["last_order_date"]
    ctx["avg_days_between_last_orders"] = metrics["avg_days_between_last_orders"]

    ctx["log"](
        "12m "
        f"total_sum={ctx['total_sum']} "
        f"sales_count={ctx['sales_count']} "
        f"orders_count_12m={ctx['orders_count_12m']} "
        f"last_order_date={ctx['last_order_date']} "
        f"avg_days_between_last_orders={ctx['avg_days_between_last_orders']}"
    )
