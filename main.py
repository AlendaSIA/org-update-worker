import os
import json
import base64
import traceback
from datetime import datetime, timedelta, timezone, date
from typing import Any, Dict, Optional, List, Tuple

import requests
import xml.etree.ElementTree as ET
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# -----------------------------------------------------------------------------
# APP
# -----------------------------------------------------------------------------
app = FastAPI()

# -----------------------------------------------------------------------------
# CONFIG (env)
# -----------------------------------------------------------------------------
PAYTRAQ_BASE_URL = os.getenv("PAYTRAQ_BASE_URL", "https://go.paytraq.com")
PAYTRAQ_API_KEY = os.getenv("PAYTRAQ_API_KEY") or os.getenv("API_KEY")
PAYTRAQ_API_TOKEN = os.getenv("PAYTRAQ_API_TOKEN") or os.getenv("API_TOKEN")

PIPEDRIVE_BASE_URL = os.getenv("PIPEDRIVE_BASE_URL", "https://api.pipedrive.com/v1")
PIPEDRIVE_API_TOKEN = os.getenv("PIPEDRIVE_API_TOKEN")

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))

# Pipedrive custom field keys (from your saved mapping)
PD_ORG_EMAIL_KEY = "faac2c792221bf18216ef17eae1941feef9a17cc"
PD_ORG_COUNTRY_KEY = "0905a8eedcb78f85063132a4aa37b76c0fc7da1d"
PD_ORG_REGNO_KEY = "259f90917e748590024b17a61fa5014a685fc3e6"
PD_ORG_VAT_KEY = "abf60c765911b83a0e0243483c2bf3ee680f6b0c"
PD_ORG_SHIPADDR_KEY = "dae3df0edeadce95ba223719cc18141795387de8"
PD_ORG_PHONE_KEY = "4b4db855bb2ac128d585e2d84c554eb099e588f7"
PD_ORG_API_UPDATED_KEY = "aefa60a6cdc10e98eb5235f9f2d5a7bf421c1cdb"
PD_ORG_12M_SUM_KEY = "0b79b8878b6eebe6ab289a60a34cd7340b28899b"

# Example PG fields (you can expand later)
PD_PG_NITRILE_SUM_KEY = "4abca39441adff414bbc87e0853ef15c42784c14"
PD_PG_NITRILE_DATE_KEY = "5160be434e5c47525f5ffba46a2e0eef63de6c59"

# Optional: define nitrile detection by SKU/keywords (tweak anytime via env)
NITRILE_SKUS = [s.strip() for s in (os.getenv("NITRILE_SKUS", "")).split(",") if s.strip()]
NITRILE_KEYWORDS = [s.strip().lower() for s in (os.getenv("NITRILE_KEYWORDS", "nitril,nitrile").split(",")) if s.strip()]

# -----------------------------------------------------------------------------
# LOG HELPERS
# -----------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def log(title: str, obj: Any = None) -> None:
    print("\n" + "=" * 90)
    print(title)
    if obj is not None:
        try:
            if isinstance(obj, (dict, list)):
                print(json.dumps(obj, ensure_ascii=False, indent=2))
            else:
                print(str(obj))
        except Exception:
            print(repr(obj))
    print("=" * 90 + "\n")

def xml_preview(s: str, limit: int = 1500) -> str:
    s = (s or "").strip()
    return s if len(s) <= limit else (s[:limit] + "\n...[TRUNCATED]...\n")

def safe_float(x: Any) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0

def env_status() -> Dict[str, Any]:
    return {
        "PAYTRAQ_BASE_URL": PAYTRAQ_BASE_URL,
        "PAYTRAQ_API_KEY_set": bool(PAYTRAQ_API_KEY),
        "PAYTRAQ_API_TOKEN_set": bool(PAYTRAQ_API_TOKEN),
        "PIPEDRIVE_BASE_URL": PIPEDRIVE_BASE_URL,
        "PIPEDRIVE_API_TOKEN_set": bool(PIPEDRIVE_API_TOKEN),
        "NITRILE_SKUS": NITRILE_SKUS,
        "NITRILE_KEYWORDS": NITRILE_KEYWORDS,
        "ts": now_iso(),
    }

# -----------------------------------------------------------------------------
# INPUT PARSING (Pub/Sub push)
# -----------------------------------------------------------------------------
def decode_pubsub_payload(body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    Expects: {"message":{"data":"base64(json)"}}.
    Returns: (payload_dict_or_None, debug)
    """
    debug: Dict[str, Any] = {"has_message": False, "has_data": False}
    if not isinstance(body, dict):
        debug["reason"] = "body_not_dict"
        return None, debug

    msg = body.get("message")
    if not isinstance(msg, dict):
        debug["reason"] = "no_message_dict"
        return None, debug
    debug["has_message"] = True

    b64 = msg.get("data")
    if not isinstance(b64, str) or not b64.strip():
        debug["reason"] = "no_message_data"
        return None, debug
    debug["has_data"] = True

    try:
        raw = base64.b64decode(b64).decode("utf-8", errors="ignore")
        debug["decoded_raw_preview"] = raw[:1000]
        payload = json.loads(raw)
        return payload, debug
    except Exception as e:
        debug["reason"] = "decode_failed"
        debug["error"] = str(e)
        debug["trace"] = traceback.format_exc()
        return None, debug

def extract_doc_and_deal(payload: Dict[str, Any]) -> Tuple[Optional[int], Optional[int], Dict[str, Any]]:
    debug = {"payload_keys": list(payload.keys()) if isinstance(payload, dict) else None}
    if not isinstance(payload, dict):
        return None, None, debug

    doc_id = None
    deal_id = None

    for k in ["document_id", "DocumentID", "doc_id", "id", "documentId"]:
        if k in payload:
            try:
                doc_id = int(payload[k])
                debug["doc_id_from"] = k
                break
            except Exception:
                debug["doc_id_bad_value"] = payload.get(k)

    for k in ["deal_id", "DealID", "dealId"]:
        if k in payload:
            try:
                deal_id = int(payload[k])
                debug["deal_id_from"] = k
                break
            except Exception:
                debug["deal_id_bad_value"] = payload.get(k)

    return doc_id, deal_id, debug

# -----------------------------------------------------------------------------
# PAYTRAQ
# -----------------------------------------------------------------------------
def paytraq_get(path: str, params: Optional[Dict[str, Any]] = None) -> str:
    if not PAYTRAQ_API_KEY or not PAYTRAQ_API_TOKEN:
        raise RuntimeError("PAYTRAQ_API_KEY/PAYTRAQ_API_TOKEN nav uzlikti (Cloud Run secrets/env)!")

    url = f"{PAYTRAQ_BASE_URL.rstrip('/')}/api/{path.lstrip('/')}"
    qp = {"APIKey": PAYTRAQ_API_KEY, "APIToken": PAYTRAQ_API_TOKEN}
    if params:
        qp.update(params)

    log("PAYTRAQ GET request", {"url": url, "params": qp})
    r = requests.get(url, params=qp, timeout=HTTP_TIMEOUT)
    log("PAYTRAQ GET response", {"status": r.status_code, "body_preview": xml_preview(r.text)})
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

def get_sale_xml(document_id: int) -> Tuple[ET.Element, Dict[str, Any]]:
    xml_text = paytraq_get(f"sale/{document_id}")
    root = parse_xml(xml_text)

    # Try extract meta (best effort)
    meta = {
        "DocumentID": str(document_id),
        "DocumentDate": find_text(root, "./Header/Document/DocumentDate"),
        "DocumentRef": find_text(root, "./Header/Document/DocumentRef"),
        "Total": find_text(root, "./Header/Total"),
        "IncludeTax": find_text(root, "./Header/IncludeTax"),
    }
    log("SALE meta parsed (best effort)", meta)
    return root, meta

def extract_client_id_from_sale(root: ET.Element) -> Tuple[Optional[int], Dict[str, Any]]:
    """
    PayTraq XML sometimes differs. We try multiple paths + deep scan fallback.
    """
    attempted = []
    paths = [
        "./Header/Document/Client/ClientID",
        "./Header/Document/ClientID",
        "./Header/Client/ClientID",
        "./Client/ClientID",
    ]
    for p in paths:
        attempted.append(p)
        v = find_text(root, p)
        if v:
            try:
                cid = int(v)
                return cid, {"found": True, "path": p, "value": v, "attempted": attempted}
            except Exception:
                return None, {"found": False, "path": p, "bad_value": v, "attempted": attempted}

    # fallback: search any element named ClientID anywhere
    for el in root.iter():
        if (el.tag or "").lower().endswith("clientid") and el.text and el.text.strip():
            t = el.text.strip()
            try:
                cid = int(t)
                return cid, {"found": True, "path": "iter(ClientID)", "value": t, "attempted": attempted}
            except Exception:
                return None, {"found": False, "path": "iter(ClientID)", "bad_value": t, "attempted": attempted}

    return None, {"found": False, "attempted": attempted}

def get_client(client_id: int) -> Dict[str, Any]:
    xml_text = paytraq_get(f"client/{client_id}")
    root = parse_xml(xml_text)
    client = {
        "ClientID": find_text(root, "./ClientID"),
        "Name": find_text(root, "./Name"),
        "Email": find_text(root, "./Email"),
        "RegNumber": find_text(root, "./RegNumber"),
        "VatNumber": find_text(root, "./VatNumber"),
        "Phone": find_text(root, "./Phone"),
        "LegalAddress_Address": find_text(root, "./LegalAddress/Address"),
        "LegalAddress_Zip": find_text(root, "./LegalAddress/Zip"),
        "LegalAddress_Country": find_text(root, "./LegalAddress/Country"),
    }
    log("CLIENT parsed", client)
    return client

def list_sales_365d(client_id: int) -> List[Dict[str, Any]]:
    till = datetime.now(timezone.utc).date()
    frm = (till - timedelta(days=365))
    params = {"ClientID": str(client_id), "date_from": frm.isoformat(), "date_till": till.isoformat()}
    xml_text = paytraq_get("sales", params=params)
    root = parse_xml(xml_text)

    rows: List[Dict[str, Any]] = []
    for sale in root.findall("./Sale"):
        rows.append({
            "DocumentID": find_text(sale, "./Header/Document/DocumentID"),
            "DocumentDate": find_text(sale, "./Header/Document/DocumentDate"),
            "DocumentStatus": find_text(sale, "./Header/Document/DocumentStatus"),
            "Total": find_text(sale, "./Header/Total"),
            "IncludeTax": find_text(sale, "./Header/IncludeTax"),
        })

    log("SALES(365d) parsed", {"count": len(rows), "sample_first_5": rows[:5]})
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

def detect_nitrile(sale_root: ET.Element) -> Tuple[bool, float, Dict[str, Any]]:
    """
    SUPER best-effort. We will refine after we see real line item structure in logs.
    """
    matches = []
    sum_total = 0.0

    for li in sale_root.findall("./LineItems/LineItem"):
        code = find_text(li, "./Item/ItemCode") or find_text(li, "./ItemCode")
        desc = (find_text(li, "./Description") or "") + " " + (find_text(li, "./ItemDescription") or "")
        desc_l = desc.lower()
        line_total = find_text(li, "./LineTotal") or "0"

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

# -----------------------------------------------------------------------------
# PIPEDRIVE
# -----------------------------------------------------------------------------
def pd_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not PIPEDRIVE_API_TOKEN:
        raise RuntimeError("PIPEDRIVE_API_TOKEN nav uzlikts (Cloud Run secrets/env)!")

    url = f"{PIPEDRIVE_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    qp = {"api_token": PIPEDRIVE_API_TOKEN}
    if params:
        qp.update(params)

    log("PIPEDRIVE GET request", {"url": url, "params": qp})
    r = requests.get(url, params=qp, timeout=HTTP_TIMEOUT)
    log("PIPEDRIVE GET response", {"status": r.status_code, "body_preview": r.text[:2000]})
    r.raise_for_status()
    return r.json()

def pd_put(path: str, data: Dict[str, Any]) -> Dict[str, Any]:
    if not PIPEDRIVE_API_TOKEN:
        raise RuntimeError("PIPEDRIVE_API_TOKEN nav uzlikts (Cloud Run secrets/env)!")

    url = f"{PIPEDRIVE_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    qp = {"api_token": PIPEDRIVE_API_TOKEN}

    log("PIPEDRIVE PUT request", {"url": url, "params": qp, "data": data})
    r = requests.put(url, params=qp, json=data, timeout=HTTP_TIMEOUT)
    log("PIPEDRIVE PUT response", {"status": r.status_code, "body_preview": r.text[:2500]})
    r.raise_for_status()
    return r.json()

def pd_find_org_by_regno(regno: str) -> Optional[int]:
    term = (regno or "").strip()
    if not term:
        return None

    # Try organizations/search first
    try:
        js = pd_get("organizations/search", params={"term": term, "exact_match": 1})
        items = (((js or {}).get("data") or {}).get("items") or [])
        if items:
            org_id = items[0].get("item", {}).get("id")
            log("ORG found via organizations/search (regno)", {"term": term, "org_id": org_id, "items_count": len(items)})
            return int(org_id) if org_id else None
    except Exception as e:
        log("organizations/search failed (regno) -> fallback", {"error": str(e)})

    # Fallback organizations/find
    try:
        js = pd_get("organizations/find", params={"term": term, "start": 0, "limit": 10})
        data = (js or {}).get("data") or []
        if data:
            org_id = data[0].get("id")
            log("ORG found via organizations/find (regno)", {"term": term, "org_id": org_id, "items_count": len(data)})
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
        v = val
        if isinstance(v, str):
            v = v.strip()
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

    # PG nitrile logic (same rule as you described earlier)
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

# -----------------------------------------------------------------------------
# ROUTES
# -----------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"ok": True, "env": env_status()}

@app.post("/")
async def pubsub_push(request: Request):
    """
    Eventarc->Cloud Run Pub/Sub push handler.
    Expects: {"message":{"data":"base64(json)"}} where json has document_id and optionally deal_id.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    log("INCOMING / body", body)

    payload, dbg_decode = decode_pubsub_payload(body)
    log("DECODE debug", dbg_decode)

    if not payload:
        return JSONResponse({"ok": True, "skipped": "no_data", "decode": dbg_decode}, status_code=200)

    doc_id, deal_id, dbg_extract = extract_doc_and_deal(payload)
    log("EXTRACT debug", dbg_extract)

    if not doc_id:
        return JSONResponse({"ok": True, "skipped": "no_document_id", "payload": payload, "extract": dbg_extract}, status_code=200)

    # PROCESS
    try:
        result = process_document(doc_id, deal_id, payload)
        return JSONResponse({"ok": True, "result": result}, status_code=200)
    except Exception as e:
        log("PROCESS FAILED", {"error": str(e), "trace": traceback.format_exc()})
        return JSONResponse({"ok": False, "error": str(e), "trace": traceback.format_exc()}, status_code=500)

# -----------------------------------------------------------------------------
# CORE
# -----------------------------------------------------------------------------
def process_document(document_id: int, deal_id: Optional[int], raw_payload: Dict[str, Any]) -> Dict[str, Any]:
    log("PROCESS START", {"document_id": document_id, "deal_id": deal_id, "ts": now_iso(), "raw_payload": raw_payload})

    sale_root, sale_meta = get_sale_xml(document_id)

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

    # compute 12m
    sales_rows = list_sales_365d(client_id)
    total_12m = compute_12m_total(sales_rows)
    log("12M computed", {"client_id": client_id, "total_12m": total_12m})

    # detect nitrile (best-effort; refine later)
    nitrile_present, nitrile_sum, nitrile_dbg = detect_nitrile(sale_root)

    # find org
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
        }

    existing_org = pd_get_org(org_id)
    log("EXISTING ORG snapshot", {
        "org_id": org_id,
        "name": existing_org.get("name"),
        "regno_field": existing_org.get(PD_ORG_REGNO_KEY),
        "email_field": existing_org.get(PD_ORG_EMAIL_KEY),
        "pg_nitrile_date": existing_org.get(PD_PG_NITRILE_DATE_KEY),
    })

    payload = build_org_update_payload(
        client=client,
        total_12m=total_12m,
        sale_meta=sale_meta,
        existing_org=existing_org,
        nitrile_present=nitrile_present,
        nitrile_sum=nitrile_sum,
    )

    if not payload:
        return {"document_id": document_id, "deal_id": deal_id, "org_id": org_id, "updated": False, "reason": "payload_empty"}

    pd_resp = pd_put(f"organizations/{org_id}", payload)

    result = {
        "document_id": document_id,
        "deal_id": deal_id,
        "org_id": org_id,
        "updated": True,
        "payload_keys": list(payload.keys()),
        "paytraq": {
            "sale_meta": sale_meta,
            "client_id": client_id,
            "client_name": client.get("Name"),
        },
        "computed": {
            "total_12m": total_12m,
            "nitrile_present": nitrile_present,
            "nitrile_sum": nitrile_sum,
            "nitrile_matches": (nitrile_dbg or {}).get("matches", []),
        },
        "pipedrive_success": bool((pd_resp or {}).get("success", True)),
        "pipedrive_response_preview": pd_resp,
    }

    log("PROCESS DONE", result)
    return result
