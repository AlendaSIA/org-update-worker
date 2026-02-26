import os
import json
import base64
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, List, Tuple

import requests
import xml.etree.ElementTree as ET
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# =============================================================================
# FASTAPI APP
# =============================================================================
app = FastAPI()

# =============================================================================
# LOG / UTIL
# =============================================================================
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def log(title: str, obj: Any = None) -> None:
    print("\n" + "=" * 100)
    print(title)
    if obj is not None:
        try:
            if isinstance(obj, (dict, list)):
                print(json.dumps(obj, ensure_ascii=False, indent=2))
            else:
                print(str(obj))
        except Exception:
            print(repr(obj))
    print("=" * 100 + "\n")

def preview_text(s: str, limit: int = 1800) -> str:
    s = (s or "").strip()
    return s if len(s) <= limit else (s[:limit] + "\n...[TRUNCATED]...\n")

def safe_float(x: Any) -> float:
    try:
        if isinstance(x, str):
            x = x.replace(",", ".").strip()
        return float(x)
    except Exception:
        return 0.0

def require_env(name: str) -> str:
    v = os.getenv(name)
    print(f"[ENV] {name} set={bool(v)}")
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v

# =============================================================================
# SECRETS ENV (ONLY)
# =============================================================================
PAYTRAQ_API_KEY = require_env("PAYTRAQ_API_KEY")
PAYTRAQ_API_TOKEN = require_env("PAYTRAQ_API_TOKEN")
PIPEDRIVE_API_TOKEN = require_env("PIPEDRIVE_API_TOKEN")

# =============================================================================
# CONFIG (non-secret)
# =============================================================================
PAYTRAQ_BASE_URL = os.getenv("PAYTRAQ_BASE_URL", "https://go.paytraq.com")
PIPEDRIVE_BASE_URL = os.getenv("PIPEDRIVE_BASE_URL", "https://api.pipedrive.com/v1")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))

# PayTraq URLs — tāpat kā tavā lokālajā kodā (format string ar APIKey/APIToken query)
SALE_URL = f"{PAYTRAQ_BASE_URL}/api/sale/{{document_id}}?APIKey={{api_key}}&APIToken={{api_token}}"
CLIENT_URL = f"{PAYTRAQ_BASE_URL}/api/client/{{client_id}}?APIKey={{api_key}}&APIToken={{api_token}}"
SALES_LIST_URL = f"{PAYTRAQ_BASE_URL}/api/sales?APIKey={{api_key}}&APIToken={{api_token}}&ClientID={{client_id}}&date_from={{date_from}}&date_till={{date_till}}"
PRODUCT_DETAILS_URL = f"{PAYTRAQ_BASE_URL}/api/product/{{item_id}}?APIKey={{api_key}}&APIToken={{api_token}}"

# =============================================================================
# PIPEDRIVE FIELD KEYS (tavs mapping)
# =============================================================================
PD_ORG_EMAIL_KEY = "faac2c792221bf18216ef17eae1941feef9a17cc"
PD_ORG_COUNTRY_KEY = "0905a8eedcb78f85063132a4aa37b76c0fc7da1d"
PD_ORG_REGNO_KEY = "259f90917e748590024b17a61fa5014a685fc3e6"
PD_ORG_VAT_KEY = "abf60c765911b83a0e0243483c2bf3ee680f6b0c"
PD_ORG_SHIPADDR_KEY = "dae3df0edeadce95ba223719cc18141795387de8"
PD_ORG_PHONE_KEY = "4b4db855bb2ac128d585e2d84c554eb099e588f7"
PD_ORG_API_UPDATED_KEY = "aefa60a6cdc10e98eb5235f9f2d5a7bf421c1cdb"
PD_ORG_12M_SUM_KEY = "0b79b8878b6eebe6ab289a60a34cd7340b28899b"

# Nitrile PG piemērs (atstājam, bet nekas nelūzīs, ja nav match)
PD_PG_NITRILE_SUM_KEY = "4abca39441adff414bbc87e0853ef15c42784c14"
PD_PG_NITRILE_DATE_KEY = "5160be434e5c47525f5ffba46a2e0eef63de6c59"
NITRILE_SKUS = [s.strip() for s in (os.getenv("NITRILE_SKUS", "")).split(",") if s.strip()]
NITRILE_KEYWORDS = [s.strip().lower() for s in (os.getenv("NITRILE_KEYWORDS", "nitril,nitrile").split(",")) if s.strip()]

# =============================================================================
# INPUT (Pub/Sub push)
# =============================================================================
def decode_pubsub_message(body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    dbg: Dict[str, Any] = {"body_keys": list(body.keys()) if isinstance(body, dict) else None}

    if not isinstance(body, dict):
        dbg["reason"] = "body_not_dict"
        return None, dbg

    msg = body.get("message")
    if not isinstance(msg, dict):
        dbg["reason"] = "no_message"
        return None, dbg

    data = msg.get("data")
    if not isinstance(data, str) or not data.strip():
        dbg["reason"] = "no_message_data"
        return None, dbg

    try:
        raw = base64.b64decode(data).decode("utf-8", errors="ignore")
        dbg["decoded_preview"] = raw[:1000]
        payload = json.loads(raw)
        dbg["payload_keys"] = list(payload.keys()) if isinstance(payload, dict) else None
        return payload, dbg
    except Exception as e:
        dbg["reason"] = "decode_failed"
        dbg["error"] = str(e)
        dbg["trace"] = traceback.format_exc()
        return None, dbg

def extract_doc_and_deal(payload: Dict[str, Any]) -> Tuple[Optional[int], Optional[int], Dict[str, Any]]:
    dbg = {"payload": payload}
    doc_id = None
    deal_id = None

    for k in ["document_id", "DocumentID", "doc_id", "id", "documentId"]:
        if k in payload:
            try:
                doc_id = int(payload[k])
                dbg["doc_id_from"] = k
                break
            except Exception:
                dbg["doc_id_bad"] = {k: payload.get(k)}

    for k in ["deal_id", "DealID", "dealId"]:
        if k in payload:
            try:
                deal_id = int(payload[k])
                dbg["deal_id_from"] = k
                break
            except Exception:
                dbg["deal_id_bad"] = {k: payload.get(k)}

    return doc_id, deal_id, dbg

# =============================================================================
# PAYTRAQ (tavs stils: pilns URL ar query)
# =============================================================================
def paytraq_get_url(url: str) -> str:
    log("PAYTRAQ GET request", {"url": url})
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    log("PAYTRAQ GET response", {"status": r.status_code, "body_preview": preview_text(r.text)})
    r.raise_for_status()
    return r.text

def parse_xml(xml_text: str) -> ET.Element:
    return ET.fromstring(xml_text.encode("utf-8", errors="ignore"))

def find_text(root: ET.Element, xpath: str) -> Optional[str]:
    el = root.find(xpath)
    if el is None or el.text is None:
        return None
    t = el.text.strip()
    return t if t else None

def get_sale_root_and_meta(document_id: int) -> Tuple[ET.Element, Dict[str, Any]]:
    url = SALE_URL.format(document_id=document_id, api_key=PAYTRAQ_API_KEY, api_token=PAYTRAQ_API_TOKEN)
    xml_text = paytraq_get_url(url)
    root = parse_xml(xml_text)

    # tieši kā tavā lokālajā: DocumentDate no .//DocumentDate
    meta = {
        "DocumentID": str(document_id),
        "DocumentDate": find_text(root, ".//DocumentDate"),
        "DocumentRef": find_text(root, ".//DocumentRef"),
        "Total": find_text(root, ".//Total"),
        "IncludeTax": find_text(root, ".//IncludeTax"),
    }
    log("SALE meta (best-effort, .//)", meta)
    return root, meta

def extract_client_id_from_sale(sale_root: ET.Element) -> Tuple[Optional[int], Dict[str, Any]]:
    # tavā stilā: ejam ar .// lai nav atkarīgs no struktūras
    attempted: List[str] = []
    paths = [
        ".//ClientID",
        ".//Client/ClientID",
        ".//Document/ClientID",
        ".//Header//ClientID",
    ]
    for p in paths:
        attempted.append(p)
        v = find_text(sale_root, p)
        if v:
            try:
                return int(v), {"found": True, "path": p, "value": v, "attempted": attempted}
            except Exception:
                return None, {"found": False, "path": p, "bad_value": v, "attempted": attempted}

    # fallback scan (namespace gadījumiem)
    for el in sale_root.iter():
        tag = (el.tag or "").lower()
        if tag.endswith("clientid") and el.text and el.text.strip():
            t = el.text.strip()
            try:
                return int(t), {"found": True, "path": "iter(*ClientID)", "value": t, "attempted": attempted}
            except Exception:
                return None, {"found": False, "path": "iter(*ClientID)", "bad_value": t, "attempted": attempted}

    return None, {"found": False, "attempted": attempted}

def get_client(client_id: int) -> Dict[str, Any]:
    url = CLIENT_URL.format(client_id=client_id, api_key=PAYTRAQ_API_KEY, api_token=PAYTRAQ_API_TOKEN)
    xml_text = paytraq_get_url(url)
    root = parse_xml(xml_text)

    client = {
        "ClientID": find_text(root, ".//ClientID"),
        "Name": find_text(root, ".//Name"),
        "Email": find_text(root, ".//Email"),
        "RegNumber": find_text(root, ".//RegNumber"),
        "VatNumber": find_text(root, ".//VatNumber"),
        "Phone": find_text(root, ".//Phone"),
        "LegalAddress_Address": find_text(root, ".//LegalAddress/Address"),
        "LegalAddress_Zip": find_text(root, ".//LegalAddress/Zip"),
        "LegalAddress_Country": find_text(root, ".//LegalAddress/Country"),
    }
    log("CLIENT parsed (.//)", client)
    return client

def list_sales_365d(client_id: int) -> List[Dict[str, Any]]:
    till = datetime.now(timezone.utc).date()
    frm = (till - timedelta(days=365))
    url = SALES_LIST_URL.format(
        api_key=PAYTRAQ_API_KEY,
        api_token=PAYTRAQ_API_TOKEN,
        client_id=client_id,
        date_from=frm.isoformat(),
        date_till=till.isoformat(),
    )
    xml_text = paytraq_get_url(url)
    root = parse_xml(xml_text)

    rows: List[Dict[str, Any]] = []
    for sale in root.findall(".//Sale"):
        rows.append({
            "DocumentID": find_text(sale, ".//DocumentID"),
            "DocumentDate": find_text(sale, ".//DocumentDate"),
            "DocumentStatus": find_text(sale, ".//DocumentStatus"),
            "Total": find_text(sale, ".//Total"),
            "IncludeTax": find_text(sale, ".//IncludeTax"),
        })

    log("SALES 365d parsed", {"count": len(rows), "sample_first_5": rows[:5]})
    return rows

def compute_12m_total(rows: List[Dict[str, Any]]) -> float:
    bad = {"voided", "reversed"}
    total = 0.0
    for r in rows:
        st = (r.get("DocumentStatus") or "").strip().lower()
        if st in bad:
            continue
        total += safe_float(r.get("Total"))
    return round(total, 2)

# --- no tava lokālā koda: group by product item_id ---
_product_group_cache: Dict[str, Optional[Dict[str, str]]] = {}

def get_group_by_item_id(item_id: str) -> Optional[Dict[str, str]]:
    """
    1:1 princips kā lokāli:
    - GET /api/product/{item_id}
    - product_xml.find("Group") -> GroupID/GroupName
    Ar cache, lai vienā dokumentā nešauj 100x pēc viena un tā paša item_id.
    """
    if not item_id:
        return None

    if item_id in _product_group_cache:
        return _product_group_cache[item_id]

    try:
        url = PRODUCT_DETAILS_URL.format(item_id=item_id, api_key=PAYTRAQ_API_KEY, api_token=PAYTRAQ_API_TOKEN)
        xml_text = paytraq_get_url(url)
        product_xml = parse_xml(xml_text)

        group_el = product_xml.find("Group")
        if group_el is not None:
            group_id = group_el.findtext("GroupID")
            group_name = group_el.findtext("GroupName")
            if group_id and group_name:
                _product_group_cache[item_id] = {"id": group_id, "name": group_name}
                return _product_group_cache[item_id]
    except Exception as e:
        print(f"[ERROR] Nevarēja iegūt grupu produktam ID {item_id}: {e}")

    _product_group_cache[item_id] = None
    return None

def extract_group_data(sale_root: ET.Element) -> Dict[str, Any]:
    """
    1:1 ar tavu step_6:
    - line_items = .//LineItem
    - document_date = .//DocumentDate
    - item_id = Item/ItemID
    - item_code = Item/ItemCode
    - qty = Qty
    - price = Price
    - total = qty*price
    """
    line_items = sale_root.findall(".//LineItem")
    document_date_el = sale_root.find(".//DocumentDate")
    document_date = document_date_el.text.strip() if (document_date_el is not None and document_date_el.text) else "Unknown"

    group_data: Dict[str, Any] = {}

    for item in line_items:
        item_id = item.findtext("Item/ItemID")
        item_code = item.findtext("Item/ItemCode")
        qty_text = item.findtext("Qty")
        price_text = item.findtext("Price")

        if not item_id or not qty_text or not price_text:
            continue

        group_info = get_group_by_item_id(item_id)
        if not group_info:
            print(f"[WARNING] Produkts '{item_code}' (ID: {item_id}) nav atrasts vai nav grupas.")
            continue

        quantity = safe_float(qty_text)
        price = safe_float(price_text)
        total = round(quantity * price, 2)

        gid = group_info["id"]
        gname = group_info["name"]

        if gid not in group_data:
            group_data[gid] = {"group_name": gname, "total_amount": 0.0, "date": document_date}

        group_data[gid]["total_amount"] += total

    log("GROUP DATA extracted", {"groups_count": len(group_data), "sample": list(group_data.items())[:5]})
    return group_data

def detect_nitrile(sale_root: ET.Element) -> Tuple[bool, float, Dict[str, Any]]:
    # Best-effort: ja vajag, vēlāk pieslīpēsim pēc reālajiem line itemiem
    matches = []
    sum_total = 0.0

    for li in sale_root.findall(".//LineItem"):
        code = li.findtext("Item/ItemCode") or li.findtext("ItemCode")
        desc = (li.findtext("Description") or "") + " " + (li.findtext("ItemDescription") or "")
        desc_l = desc.lower()
        line_total = li.findtext("LineTotal") or "0"

        hit = False
        if code and code in NITRILE_SKUS:
            hit = True
        if not hit and any(k in desc_l for k in NITRILE_KEYWORDS):
            hit = True

        if hit:
            lt = safe_float(line_total)
            sum_total += lt
            matches.append({"ItemCode": code, "Description": desc.strip(), "LineTotal": line_total})

    dbg = {"matches": matches, "sum_total": round(sum_total, 2)}
    log("NITRILE detect debug", dbg)
    return (len(matches) > 0), round(sum_total, 2), dbg

# =============================================================================
# PIPEDRIVE API
# =============================================================================
def pd_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{PIPEDRIVE_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    qp = {"api_token": PIPEDRIVE_API_TOKEN}
    if params:
        qp.update(params)

    log("PIPEDRIVE GET request", {"url": url, "params": qp})
    r = requests.get(url, params=qp, timeout=HTTP_TIMEOUT)
    log("PIPEDRIVE GET response", {"status": r.status_code, "body_preview": preview_text(r.text, 2200)})
    r.raise_for_status()
    return r.json()

def pd_put(path: str, data: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{PIPEDRIVE_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    qp = {"api_token": PIPEDRIVE_API_TOKEN}

    log("PIPEDRIVE PUT request", {"url": url, "params": qp, "data": data})
    r = requests.put(url, params=qp, json=data, timeout=HTTP_TIMEOUT)
    log("PIPEDRIVE PUT response", {"status": r.status_code, "body_preview": preview_text(r.text, 2600)})
    r.raise_for_status()
    return r.json()

def pd_find_org_by_regno(regno: str) -> Optional[int]:
    term = (regno or "").strip()
    if not term:
        return None

    try:
        js = pd_get("organizations/search", params={"term": term, "exact_match": 1})
        items = (((js or {}).get("data") or {}).get("items") or [])
        if items:
            org_id = items[0].get("item", {}).get("id")
            log("ORG found via organizations/search (regno)", {"term": term, "org_id": org_id})
            return int(org_id) if org_id else None
    except Exception as e:
        log("organizations/search failed (regno) -> fallback", {"error": str(e)})

    try:
        js = pd_get("organizations/find", params={"term": term, "start": 0, "limit": 10})
        data = (js or {}).get("data") or []
        if data:
            org_id = data[0].get("id")
            log("ORG found via organizations/find (regno)", {"term": term, "org_id": org_id})
            return int(org_id) if org_id else None
    except Exception as e:
        log("organizations/find failed (regno)", {"error": str(e)})

    log("ORG not found by regno", {"regno": term})
    return None

def pd_find_org_by_email(email: str) -> Optional[int]:
    term = (email or "").strip()
    if not term:
        return None

    try:
        js = pd_get("organizations/search", params={"term": term, "exact_match": 1})
        items = (((js or {}).get("data") or {}).get("items") or [])
        if items:
            org_id = items[0].get("item", {}).get("id")
            log("ORG found via organizations/search (email)", {"term": term, "org_id": org_id})
            return int(org_id) if org_id else None
    except Exception as e:
        log("organizations/search failed (email) -> fallback", {"error": str(e)})

    try:
        js = pd_get("organizations/find", params={"term": term, "search_by_email": 1, "start": 0, "limit": 10})
        data = (js or {}).get("data") or []
        if data:
            org_id = data[0].get("id")
            log("ORG found via organizations/find (email)", {"term": term, "org_id": org_id})
            return int(org_id) if org_id else None
    except Exception as e:
        log("organizations/find failed (email)", {"error": str(e)})

    log("ORG not found by email", {"email": term})
    return None

def pd_get_org(org_id: int) -> Dict[str, Any]:
    js = pd_get(f"organizations/{org_id}")
    return (js or {}).get("data") or {}

def build_org_update_payload(
    client: Dict[str, Any],
    total_12m: float,
    sale_meta: Dict[str, Any],
    existing_org: Dict[str, Any],
    nitrile_present: bool,
    nitrile_sum: float,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}

    def set_if(val: Any, key: str, label: str) -> None:
        v = val.strip() if isinstance(val, str) else val
        if v is None or v == "":
            print(f"[SKIP] {label}: empty")
            return
        payload[key] = v
        print(f"[SET] {label} -> {key} = {v}")

    set_if(client.get("RegNumber"), PD_ORG_REGNO_KEY, "RegNumber")
    set_if(client.get("Email"), PD_ORG_EMAIL_KEY, "Email")
    set_if(client.get("VatNumber"), PD_ORG_VAT_KEY, "VatNumber")
    set_if(client.get("Phone"), PD_ORG_PHONE_KEY, "Phone")
    set_if(client.get("LegalAddress_Country"), PD_ORG_COUNTRY_KEY, "Country")

    addr = client.get("LegalAddress_Address")
    zipc = client.get("LegalAddress_Zip")
    ctry = client.get("LegalAddress_Country")
    ship_full = " ".join([x for x in [addr, zipc, ctry] if x and str(x).strip()])
    set_if(ship_full, PD_ORG_SHIPADDR_KEY, "Shipping address (from LegalAddress)")

    payload[PD_ORG_12M_SUM_KEY] = total_12m
    print(f"[SET] 12m sum -> {PD_ORG_12M_SUM_KEY} = {total_12m}")

    payload[PD_ORG_API_UPDATED_KEY] = now_iso()
    print(f"[SET] API updated -> {PD_ORG_API_UPDATED_KEY} = {payload[PD_ORG_API_UPDATED_KEY]}")

    # PG nitrile rule (optional)
    if nitrile_present:
        payload[PD_PG_NITRILE_SUM_KEY] = nitrile_sum
        print(f"[SET] PG Sum nitrile -> {PD_PG_NITRILE_SUM_KEY} = {nitrile_sum}")

        doc_date = sale_meta.get("DocumentDate")
        if doc_date:
            current = existing_org.get(PD_PG_NITRILE_DATE_KEY)
            should_set = False
            if not current:
                print("[PG DATE] current empty -> will set")
                should_set = True
            else:
                try:
                    cur_d = datetime.fromisoformat(str(current)).date()
                    new_d = datetime.fromisoformat(str(doc_date)).date()
                    if cur_d < new_d:
                        print(f"[PG DATE] current {cur_d} < new {new_d} -> will set")
                        should_set = True
                    else:
                        print(f"[PG DATE] current {cur_d} >= new {new_d} -> keep")
                except Exception:
                    print("[PG DATE] parse failed -> will set to be safe")
                    should_set = True

            if should_set:
                payload[PD_PG_NITRILE_DATE_KEY] = doc_date
                print(f"[SET] PG Date nitrile -> {PD_PG_NITRILE_DATE_KEY} = {doc_date}")
        else:
            print("[PG DATE] DocumentDate missing -> skip date update")
    else:
        print("[PG] nitrile not present -> skip PG fields")

    log("FINAL org update payload", payload)
    return payload

# =============================================================================
# ROUTES
# =============================================================================
@app.get("/health")
async def health():
    return {
        "ok": True,
        "env": {
            "PAYTRAQ_BASE_URL": PAYTRAQ_BASE_URL,
            "PIPEDRIVE_BASE_URL": PIPEDRIVE_BASE_URL,
            "PAYTRAQ_API_KEY_set": True,
            "PAYTRAQ_API_TOKEN_set": True,
            "PIPEDRIVE_API_TOKEN_set": True,
            "NITRILE_SKUS": NITRILE_SKUS,
            "NITRILE_KEYWORDS": NITRILE_KEYWORDS,
            "ts": now_iso(),
        },
    }

@app.post("/")
async def pubsub_push(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    log("INCOMING POST / body", body)

    payload, dbg_decode = decode_pubsub_message(body)
    log("DECODE debug", dbg_decode)

    if not payload:
        return JSONResponse({"ok": True, "skipped": "no_data", "decode": dbg_decode}, status_code=200)

    doc_id, deal_id, dbg_extract = extract_doc_and_deal(payload)
    log("EXTRACT debug", dbg_extract)

    if not doc_id:
        return JSONResponse({"ok": True, "skipped": "no_document_id", "payload": payload}, status_code=200)

    try:
        result = process_document(doc_id, deal_id, payload)
        return JSONResponse({"ok": True, "result": result}, status_code=200)
    except Exception as e:
        log("PROCESS FAILED", {"error": str(e), "trace": traceback.format_exc()})
        return JSONResponse({"ok": False, "error": str(e), "trace": traceback.format_exc()}, status_code=500)

# =============================================================================
# CORE
# =============================================================================
def process_document(document_id: int, deal_id: Optional[int], raw_payload: Dict[str, Any]) -> Dict[str, Any]:
    log("PROCESS START", {"document_id": document_id, "deal_id": deal_id, "ts": now_iso(), "raw_payload": raw_payload})

    sale_root, sale_meta = get_sale_root_and_meta(document_id)

    client_id, dbg_cid = extract_client_id_from_sale(sale_root)
    log("CLIENT_ID extract debug", dbg_cid)

    if not client_id:
        return {
            "document_id": document_id,
            "deal_id": deal_id,
            "skipped": "no_client_id",
            "sale_meta": sale_meta,
            "client_id_debug": dbg_cid,
        }

    client = get_client(client_id)

    sales_rows = list_sales_365d(client_id)
    total_12m = compute_12m_total(sales_rows)
    log("12M computed", {"client_id": client_id, "total_12m": total_12m})

    # Group extraction (tavs lokālais princips) — pagaidām tikai logam, lai redzam ka viss strādā
    group_data = extract_group_data(sale_root)

    nitrile_present, nitrile_sum, nitrile_dbg = detect_nitrile(sale_root)

    regno = (client.get("RegNumber") or "").strip()
    email = (client.get("Email") or "").strip()

    org_id = None
    if regno:
        org_id = pd_find_org_by_regno(regno)
    if not org_id and email:
        org_id = pd_find_org_by_email(email)

    if not org_id:
        return {
            "document_id": document_id,
            "deal_id": deal_id,
            "skipped": "org_not_found",
            "client_id": client_id,
            "regno": regno,
            "email": email,
            "sale_meta": sale_meta,
            "groups_count": len(group_data),
        }

    existing_org = pd_get_org(org_id)
    log("EXISTING ORG snapshot", {
        "org_id": org_id,
        "name": existing_org.get("name"),
        "regno_field": existing_org.get(PD_ORG_REGNO_KEY),
        "email_field": existing_org.get(PD_ORG_EMAIL_KEY),
    })

    update_payload = build_org_update_payload(
        client=client,
        total_12m=total_12m,
        sale_meta=sale_meta,
        existing_org=existing_org,
        nitrile_present=nitrile_present,
        nitrile_sum=nitrile_sum,
    )

    if not update_payload:
        return {"document_id": document_id, "deal_id": deal_id, "org_id": org_id, "updated": False, "reason": "payload_empty"}

    pd_resp = pd_put(f"organizations/{org_id}", update_payload)

    result = {
        "document_id": document_id,
        "deal_id": deal_id,
        "org_id": org_id,
        "updated": True,
        "payload_keys": list(update_payload.keys()),
        "paytraq": {
            "sale_meta": sale_meta,
            "client_id": client_id,
            "client_name": client.get("Name"),
        },
        "computed": {
            "total_12m": total_12m,
            "groups_count": len(group_data),
            "nitrile_present": nitrile_present,
            "nitrile_sum": nitrile_sum,
        },
        "pipedrive_response_success": bool((pd_resp or {}).get("success", True)),
    }

    log("PROCESS DONE", result)
    return result
