from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, date
from xml.etree import ElementTree as ET
import os
import requests
import re

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


def _extract_doc_ref(sale_el: ET.Element) -> str:
    return (
        (sale_el.findtext(".//Header/Document/DocumentRef") or "")
        or (sale_el.findtext(".//Document/DocumentRef") or "")
        or (sale_el.findtext(".//DocumentRef") or "")
    ).strip()


def _is_ale_sale(sale_el: ET.Element) -> bool:
    # Biznesa noteikums: reālie pārdošanas dokumenti ir tie, kuru DocumentRef sākas ar "ALE"
    ref = _extract_doc_ref(sale_el)
    return ref.upper().startswith("ALE")


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


def _extract_include_tax(root: ET.Element) -> bool:
    v = (
        root.findtext(".//Header/IncludeTax")
        or root.findtext(".//IncludeTax")
        or ""
    ).strip().lower()
    return v in ("true", "1", "yes")


def _vat_rate_from_text(s: str) -> Optional[float]:
    if not s:
        return None
    m = re.search(r"(\d{1,2})(?:[.,](\d{1,2}))?\s*%", s)
    if not m:
        return None
    whole = m.group(1)
    frac = m.group(2)
    try:
        if frac:
            pct = float(f"{whole}.{frac}")
        else:
            pct = float(whole)
        return pct / 100.0
    except Exception:
        return None


def _extract_line_vat_rate(line_node: ET.Element, full_root: ET.Element) -> Optional[float]:
    # 1) mēģinam no līnijas TaxKeyName, piem. "PVN 21%"
    for xp in ("TaxKey/TaxKeyName", "TaxKeyName", "VAT", "Vat", "Tax"):
        txt = (line_node.findtext(xp) or "").strip()
        r = _vat_rate_from_text(txt)
        if r is not None:
            return r

    # 2) fallback no dokumenta Taxes/Tax/TaxKeyName
    for tax in full_root.findall(".//Taxes/Tax"):
        txt = (tax.findtext(".//TaxKey/TaxKeyName") or tax.findtext("TaxKeyName") or "").strip()
        r = _vat_rate_from_text(txt)
        if r is not None:
            return r

    return None


def _line_amount_gross(line_node: ET.Element) -> Optional[float]:
    # prefer totals if present (šobrīd bieži ir gross, ja IncludeTax=true)
    for nm in ("Total", "LineTotal", "Sum", "TotalWithTax", "Amount", "RowTotal"):
        v = _parse_float(line_node.findtext(nm))
        if v is not None:
            return v
    # fallback qty*price
    qty = _parse_float(line_node.findtext("Qty") or line_node.findtext("Quantity"))
    price = _parse_float(line_node.findtext("Price") or line_node.findtext("UnitPrice"))
    if qty is not None and price is not None:
        return float(qty) * float(price)
    return None


def _line_amount_net(line_node: ET.Element, full_root: ET.Element) -> Optional[float]:
    gross = _line_amount_gross(line_node)
    if gross is None:
        return None

    include_tax = _extract_include_tax(full_root)
    if not include_tax:
        # ja PayTraq saka, ka nodoklis nav iekļauts cenā, tad pieņemam, ka šī summa jau ir NET
        return float(gross)

    vat = _extract_line_vat_rate(line_node, full_root)
    if vat is None:
        # nevaram droši izrēķināt net -> atgriežam gross, lai neizdomātu
        return float(gross)

    return float(gross) / (1.0 + float(vat))


def _line_item_id(node: ET.Element) -> Optional[str]:
    # tolerant paths (lokālajā skriptā bija Item/ItemID)
    for nm in ("Item/ItemID", "ItemID", "ProductID", "Product/ID", "Item/ID"):
        t = (node.findtext(nm) or "").strip()
        if t:
            return t
    return None


def _sale_net_total(full_root: ET.Element) -> Optional[float]:
    # Prefer Totals/NetAmount (tas ir tieši NET bez PVN)
    for xp in (".//Totals/NetAmount", ".//NetAmount"):
        v = _parse_float(full_root.findtext(xp))
        if v is not None:
            return float(v)
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

    sales_all = ctx["paytraq_list_sales"](str(ctx["client_id"]), ctx["date_from"], ctx["date_till"])

    # ✅ FILTRS: tikai ALE dokumenti tiek ņemti 12m un PG aprēķinos
    sales = [s for s in sales_all if _is_ale_sale(s)]

    ctx["sales_count"] = len(sales)
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

    # ---- PG all groups (12m) + TOTAL NET (12m) ----
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    product_group_cache: Dict[str, Optional[Tuple[str, str]]] = {}

    total_net_sum = 0.0

    for sale_el in sales:
        doc_id = _extract_sale_id(sale_el)
        if not doc_id:
            continue

        sale_date = _extract_sale_date(sale_el)

        try:
            full = _fetch_sale_xml(str(doc_id))
        except Exception:
            continue

        # ✅ TOTAL NET: summējam NetAmount no pilnā XML
        net_total = _sale_net_total(full)
        if net_total is not None:
            total_net_sum += float(net_total)

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

            # ✅ PG NET: rindu summas bez PVN
            amt = _line_amount_net(ln, full)
            if amt is None:
                continue

            key = (ginfo[0], ginfo[1])
            if key not in groups:
                groups[key] = {"sum": 0.0, "last_date": None}

            groups[key]["sum"] += float(amt)
            if sale_date and (groups[key]["last_date"] is None or sale_date > groups[key]["last_date"]):
                groups[key]["last_date"] = sale_date

    # ✅ total_sum tagad ir NET (bez PVN)
    ctx["total_sum"] = round(float(total_net_sum), 2)

    pg_out: Dict[str, Dict[str, Any]] = {}
    for (_gid, gname), info in groups.items():
        pg_out[gname] = {
            "sum": round(float(info["sum"]), 2),
            "date": (info["last_date"].isoformat() if info["last_date"] else None),
        }

    ctx["pg"] = pg_out

    ctx["step_03"] = {
        "groups_count": len(pg_out),
        "products_lookups": len(product_group_cache),
        "sales_count": ctx["sales_count"],
        # debug: cik listā bija vispār un cik palika pēc ALE filtra
        "sales_count_all": len(sales_all),
        "sales_count_ale": len(sales),
    }

    ctx["computed"] = {
        "sales_count": ctx["sales_count"],
        "total_sum": ctx["total_sum"],
        "orders_count_12m": ctx["orders_count_12m"],
        "last_order_date": ctx["last_order_date"],
        "avg_days_between_last_orders": ctx["avg_days_between_last_orders"],
        "pg": ctx["pg"],
    }
