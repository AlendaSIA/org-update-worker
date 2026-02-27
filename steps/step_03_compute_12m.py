from typing import Any, Dict, List, Optional
from datetime import datetime, date
from xml.etree import ElementTree as ET
import os
import requests

PAYTRAQ_BASE_URL = os.getenv("PAYTRAQ_BASE_URL", "https://go.paytraq.com").rstrip("/")
PAYTRAQ_API_KEY = os.getenv("PAYTRAQ_API_KEY")
PAYTRAQ_API_TOKEN = os.getenv("PAYTRAQ_API_TOKEN")


def _to_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        s = v.strip().replace("Z", "")
        if not s:
            return None
        try:
            return datetime.fromisoformat(s).date()
        except Exception:
            pass
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except Exception:
            return None
    return None


def _extract_sale_date(sale_el: ET.Element) -> Optional[date]:
    txt = (
        sale_el.findtext(".//Header/Document/DocumentDate")
        or sale_el.findtext(".//Document/DocumentDate")
        or sale_el.findtext(".//DocumentDate")
        or ""
    ).strip()
    return _to_date(txt)


def _extract_sale_ref(sale_el: ET.Element) -> str:
    return (
        sale_el.findtext(".//Header/Document/DocumentRef")
        or sale_el.findtext(".//Document/DocumentRef")
        or sale_el.findtext(".//DocumentRef")
        or ""
    ).strip()


def _extract_sale_id(sale_el: ET.Element) -> Optional[str]:
    for path in (
        ".//Header/Document/DocumentID",
        ".//Document/DocumentID",
        ".//DocumentID",
        ".//SaleID",
        ".//ID",
    ):
        t = (sale_el.findtext(path) or "").strip()
        if t:
            return t
    return None


def _parse_float(t: Any) -> Optional[float]:
    if t is None:
        return None
    s = str(t).strip()
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except Exception:
        return None


def _fetch_sale_xml(document_id: str) -> ET.Element:
    if not PAYTRAQ_API_KEY or not PAYTRAQ_API_TOKEN:
        raise RuntimeError("Missing PAYTRAQ_API_KEY / PAYTRAQ_API_TOKEN env")
    r = requests.get(
        f"{PAYTRAQ_BASE_URL}/api/sale/{document_id}",
        params={"APIKey": PAYTRAQ_API_KEY, "APIToken": PAYTRAQ_API_TOKEN},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"PayTraq /api/sale/{document_id} failed {r.status_code}: {r.text[:200]}")
    return ET.fromstring(r.text)


def _iter_line_like_nodes(root: ET.Element):
    for xp in (
        ".//Lines/Line",
        ".//Line",
        ".//SaleLine",
        ".//LineItem",
        ".//Items/Item",
        ".//Rows/Row",
    ):
        nodes = root.findall(xp)
        if nodes:
            for n in nodes:
                yield n


def _line_text(node: ET.Element, *names: str) -> str:
    for nm in names:
        t = node.findtext(nm)
        if t and t.strip():
            return t.strip()
    return ""


def _line_amount(node: ET.Element) -> Optional[float]:
    for nm in (
        "Total",
        "LineTotal",
        "Sum",
        "TotalWithTax",
        "Amount",
        "RowTotal",
    ):
        v = _parse_float(node.findtext(nm))
        if v is not None:
            return v
    return None


def _is_nitrile(node: ET.Element) -> bool:
    hay = " ".join(
        [
            _line_text(node, "ProductName", "Name", "ItemName"),
            _line_text(node, "ProductGroup", "Group", "GroupName", "ProductGroupName"),
            _line_text(node, "ProductCode", "Code", "ItemCode"),
        ]
    ).lower()
    return "nitril" in hay


def _compute_order_metrics_from_dates(dates: List[date]) -> Dict[str, Any]:
    if not dates:
        return {"orders_count_12m": 0, "last_order_date": None, "avg_days_between_last_orders": None}

    dates.sort()
    last_order_date = dates[-1].isoformat()

    last4 = dates[-4:]
    if len(last4) <= 1:
        avg_days = 1
    else:
        diffs = [(last4[i] - last4[i - 1]).days for i in range(1, len(last4))]
        avg_days = round(sum(diffs) / max(len(diffs), 1), 2)

    return {"orders_count_12m": len(dates), "last_order_date": last_order_date, "avg_days_between_last_orders": avg_days}


def run(ctx: Dict[str, Any]) -> None:
    if ctx.get("skipped"):
        return

    sales = ctx["paytraq_list_sales"](str(ctx["client_id"]), ctx["date_from"], ctx["date_till"])

    ctx["sales_count"] = len(sales)
    ctx["total_sum"] = ctx["compute_total"](sales)
    ctx["sample_refs"] = ctx["extract_refs"](sales, limit=20)

    # ---- orders metrics ----
    dates: List[date] = []
    for s in sales:
        d = _extract_sale_date(s)
        if d:
            dates.append(d)

    metrics = _compute_order_metrics_from_dates(dates)
    ctx["orders_count_12m"] = metrics["orders_count_12m"]
    ctx["last_order_date"] = metrics["last_order_date"]
    ctx["avg_days_between_last_orders"] = metrics["avg_days_between_last_orders"]

    # ---- PG 12m (start: tikai “Cimdi nitrila”) ----
    pg_sum_nitrile = 0.0
    pg_last_date: Optional[date] = None

    for sale_el in sales:
        ref = _extract_sale_ref(sale_el)
        if ref and not ref.startswith("ALE"):
            continue

        doc_id = _extract_sale_id(sale_el)
        if not doc_id:
            continue

        sale_date = _extract_sale_date(sale_el)
        try:
            full = _fetch_sale_xml(str(doc_id))
        except Exception:
            continue

        sale_has_nitrile = False
        sale_nitrile_sum = 0.0

        for ln in _iter_line_like_nodes(full):
            if not _is_nitrile(ln):
                continue
            amt = _line_amount(ln)
            if amt is None:
                continue
            sale_has_nitrile = True
            sale_nitrile_sum += float(amt)

        if sale_has_nitrile and sale_nitrile_sum > 0:
            pg_sum_nitrile += sale_nitrile_sum
            if sale_date and (pg_last_date is None or sale_date > pg_last_date):
                pg_last_date = sale_date

    ctx["pg"] = {
        "Cimdi nitrila": {
            "sum": round(pg_sum_nitrile, 2),
            "date": (pg_last_date.isoformat() if pg_last_date else None),
        }
    }

    # neatkarīgs step_03 “paraksts” (lai Step_04 nepazūd)
    ctx["step_03"] = {
        "pg_sum_nitrile": ctx["pg"]["Cimdi nitrila"]["sum"],
        "pg_date_nitrile": ctx["pg"]["Cimdi nitrila"]["date"],
        "sales_count": ctx["sales_count"],
    }

    # computed (var tikt pārrakstīts vēlāk, bet step_03 paliks)
    ctx["computed"] = {
        "sales_count": ctx["sales_count"],
        "total_sum": ctx["total_sum"],
        "orders_count_12m": ctx["orders_count_12m"],
        "last_order_date": ctx["last_order_date"],
        "avg_days_between_last_orders": ctx["avg_days_between_last_orders"],
        "pg": ctx["pg"],
    }
