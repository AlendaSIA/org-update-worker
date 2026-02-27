from typing import Any, Dict, List, Optional, Tuple
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


def _fetch_product_group(item_id: str) -> Optional[Tuple[str, str]]:
    """Returns (group_id, group_name)"""
    if not PAYTRAQ_API_KEY or not PAYTRAQ_API_TOKEN:
        raise RuntimeError("Missing PAYTRAQ_API_KEY / PAYTRAQ_API_TOKEN env")

    r = requests.get(
        f"{PAYTRAQ_BASE_URL}/api/product/{item_id}",
        params={"APIKey": PAYTRAQ_API_KEY, "APIToken": PAYTRAQ_API_TOKEN},
        timeout=30,
    )
    if r.status_code != 200:
        return None

    root = ET.fromstring(r.text)
    g = root.find("Group")
    if g is None:
        return None

    gid = (g.findtext("GroupID") or "").strip()
    gname = (g.findtext("GroupName") or "").strip()
    if not gid or not gname:
        return None

    return gid, gname


def _iter_line_like_nodes(root: ET.Element):
    for xp in (".//LineItem", ".//Lines/Line", ".//Line", ".//SaleLine", ".//Items/Item", ".//Rows/Row"):
        nodes = root.findall(xp)
        if nodes:
            for n in nodes:
                yield n


def _line_amount(node: ET.Element) -> Optional[float]:
    # prefer totals if present
    for nm in ("Total", "LineTotal", "Sum", "TotalWithTax", "Amount", "RowTotal"):
        v = _parse_float(node.findtext(nm))
        if v is not None:
            return v
    # fallback qty*price
    qty = _parse_float(node.findtext("Qty") or node.findtext("Quantity"))
    price = _parse_float(node.findtext("Price") or node.findtext("UnitPrice"))
    if qty is not None and price is not None:
        return float(qty) * float(price)
    return None


def _line_item_id(node: ET.Element) -> Optional[str]:
    # tolerant paths (lokālajā skriptā bija Item/ItemID)
    for nm in ("Item/ItemID", "ItemID", "ProductID", "Product/ID", "Item/ID"):
        t = (node.findtext(nm) or "").strip()
        if t:
            return t
    return None


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

    return {
        "orders_count_12m": len(dates),
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

    # ---- PG all groups (12m) ----
    # group_key: (group_id, group_name) -> {sum, last_date}
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    product_group_cache: Dict[str, Optional[Tuple[str, str]]] = {}

    for sale_el in sales:
        doc_id = _extract_sale_id(sale_el)
        if not doc_id:
            continue

        sale_date = _extract_sale_date(sale_el)

        try:
            full = _fetch_sale_xml(str(doc_id))
        except Exception:
            continue

        for ln in _iter_line_like_nodes(full):
            item_id = _line_item_id(ln)
            if not item_id:
                continue

            if item_id not in product_group_cache:
                try:
                    product_group_cache[item_id] = _fetch_product_group(item_id)
                except Exception:
                    product_group_cache[item_id] = None

            ginfo = product_group_cache.get(item_id)
            if not ginfo:
                continue

            amt = _line_amount(ln)
            if amt is None:
                continue

            key = (ginfo[0], ginfo[1])
            if key not in groups:
                groups[key] = {"sum": 0.0, "last_date": None}

            groups[key]["sum"] += float(amt)
            if sale_date and (groups[key]["last_date"] is None or sale_date > groups[key]["last_date"]):
                groups[key]["last_date"] = sale_date

    # ctx["pg"] by GroupName (jo Step_04 taisa laukus pēc nosaukuma)
    pg_out: Dict[str, Dict[str, Any]] = {}
    for (_gid, gname), info in groups.items():
        pg_out[gname] = {
            "sum": round(float(info["sum"]), 2),
            "date": (info["last_date"].isoformat() if info["last_date"] else None),
        }

    ctx["pg"] = pg_out

    # step_03 signature + quick debug counts
    ctx["step_03"] = {
        "groups_count": len(pg_out),
        "products_lookups": len(product_group_cache),
        "sales_count": ctx["sales_count"],
    }

    ctx["computed"] = {
        "sales_count": ctx["sales_count"],
        "total_sum": ctx["total_sum"],
        "orders_count_12m": ctx["orders_count_12m"],
        "last_order_date": ctx["last_order_date"],
        "avg_days_between_last_orders": ctx["avg_days_between_last_orders"],
        "pg": ctx["pg"],
    }
