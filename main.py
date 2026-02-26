import os
import json
import base64
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple, List

import requests
from flask import Flask, request, jsonify
import xml.etree.ElementTree as ET

app = Flask(__name__)

# ----------------------------
# CONFIG
# ----------------------------
PAYTRAQ_BASE_URL = os.getenv("PAYTRAQ_BASE_URL", "https://go.paytraq.com")
PAYTRAQ_API_KEY = os.getenv("PAYTRAQ_API_KEY") or os.getenv("API_KEY")
PAYTRAQ_API_TOKEN = os.getenv("PAYTRAQ_API_TOKEN") or os.getenv("API_TOKEN")

PIPEDRIVE_BASE_URL = os.getenv("PIPEDRIVE_BASE_URL", "https://api.pipedrive.com/v1")
PIPEDRIVE_API_TOKEN = os.getenv("PIPEDRIVE_API_TOKEN")

# Pipedrive custom field keys (from your saved mapping)
PD_ORG_EMAIL_KEY = "faac2c792221bf18216ef17eae1941feef9a17cc"
PD_ORG_COUNTRY_KEY = "0905a8eedcb78f85063132a4aa37b76c0fc7da1d"
PD_ORG_REGNO_KEY = "259f90917e748590024b17a61fa5014a685fc3e6"
PD_ORG_VAT_KEY = "abf60c765911b83a0e0243483c2bf3ee680f6b0c"
PD_ORG_SHIPADDR_KEY = "dae3df0edeadce95ba223719cc18141795387de8"
PD_ORG_PHONE_KEY = "4b4db855bb2ac128d585e2d84c554eb099e588f7"
PD_ORG_API_UPDATED_KEY = "aefa60a6cdc10e98eb5235f9f2d5a7bf421c1cdb"
PD_ORG_12M_SUM_KEY = "0b79b8878b6eebe6ab289a60a34cd7340b28899b"

PD_PG_NITRILE_SUM_KEY = "4abca39441adff414bbc87e0853ef15c42784c14"
PD_PG_NITRILE_DATE_KEY = "5160be434e5c47525f5ffba46a2e0eef63de6c59"

# How to detect nitrile gloves in sale line items:
# Provide comma-separated SKU codes OR keywords via env, so you can tweak without code changes.
NITRILE_SKUS = [s.strip() for s in (os.getenv("NITRILE_SKUS", "")).split(",") if s.strip()]
NITRILE_KEYWORDS = [s.strip().lower() for s in (os.getenv("NITRILE_KEYWORDS", "nitril,nitrile").split(",")) if s.strip()]

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))

# ----------------------------
# HELPERS (logging)
# ----------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def log(title: str, obj: Any = None) -> None:
    print("\n" + "=" * 80)
    print(title)
    if obj is not None:
        try:
            if isinstance(obj, (dict, list)):
                print(json.dumps(obj, ensure_ascii=False, indent=2))
            else:
                print(str(obj))
        except Exception:
            print(repr(obj))
    print("=" * 80 + "\n")

def short_xml(xml_text: str, limit: int = 1500) -> str:
    xml_text = (xml_text or "").strip()
    if len(xml_text) <= limit:
        return xml_text
    return xml_text[:limit] + "\n...[TRUNCATED]...\n"

def safe_float(x: Optional[str]) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0

def get_env_ok() -> Dict[str, Any]:
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

# ----------------------------
# PAYTRAQ
# ----------------------------
def paytraq_get(path: str, params: Optional[Dict[str, Any]] = None) -> str:
    if not PAYTRAQ_API_KEY or not PAYTRAQ_API_TOKEN:
        raise RuntimeError("PAYTRAQ_API_KEY / PAYTRAQ_API_TOKEN nav uzlikti env!")

    url = f"{PAYTRAQ_BASE_URL.rstrip('/')}/api/{path.lstrip('/')}"
    qp = {"APIKey": PAYTRAQ_API_KEY, "APIToken": PAYTRAQ_API_TOKEN}
    if params:
        qp.update(params)

    log("PayTraq GET -> request", {"url": url, "params": qp})
    r = requests.get(url, params=qp, timeout=HTTP_TIMEOUT)
    log("PayTraq GET -> response", {"status": r.status_code, "headers": dict(r.headers), "body_preview": short_xml(r.text)})
    r.raise_for_status()
    return r.text

def parse_xml(xml_text: str) -> ET.Element:
    # PayTraq returns XML
    return ET.fromstring(xml_text.encode("utf-8", errors="ignore"))

def xml_find_text(root: ET.Element, xpath: str) -> Optional[str]:
    el = root.find(xpath)
    if el is None:
        return None
    if el.text is None:
        return None
    t = el.text.strip()
    return t if t != "" else None

def get_sale(document_id: int) -> Tuple[ET.Element, Dict[str, Any]]:
    # GET /api/sale/{DocumentID} (PayTraq docs) :contentReference[oaicite:1]{index=1}
    xml_text = paytraq_get(f"sale/{document_id}")
    root = parse_xml(xml_text)

    # Extract minimal fields we need
    doc_date = xml_find_text(root, "./Header/Document/DocumentDate")
    doc_ref = xml_find_text(root, "./Header/Document/DocumentRef")
    client_id = xml_find_text(root, "./Header/Document/Client/ClientID")
    client_name = xml_find_text(root, "./Header/Document/Client/ClientName")
    total = xml_find_text(root, "./Header/Total")
    include_tax = xml_find_text(root, "./Header/IncludeTax")

    meta = {
        "DocumentID": str(document_id),
        "DocumentDate": doc_date,
        "DocumentRef": doc_ref,
        "ClientID": client_id,
        "ClientName": client_name,
        "Total": total,
        "IncludeTax": include_tax,
    }
    log("PayTraq sale -> parsed meta", meta)
    return root, meta

def get_client(client_id: int) -> Dict[str, Any]:
    # GET /api/client/{ClientID} :contentReference[oaicite:2]{index=2}
    xml_text = paytraq_get(f"client/{client_id}")
    root = parse_xml(xml_text)

    client = {
        "ClientID": xml_find_text(root, "./ClientID"),
        "Name": xml_find_text(root, "./Name"),
        "Email": xml_find_text(root, "./Email"),
        "RegNumber": xml_find_text(root, "./RegNumber"),
        "VatNumber": xml_find_text(root, "./VatNumber"),
        "Phone": xml_find_text(root, "./Phone"),
        "LegalAddress_Address": xml_find_text(root, "./LegalAddress/Address"),
        "LegalAddress_Zip": xml_find_text(root, "./LegalAddress/Zip"),
        "LegalAddress_Country": xml_find_text(root, "./LegalAddress/Country"),
    }
    log("PayTraq client -> parsed", client)
    return client

def list_sales_for_client_365d(client_id: int) -> List[Dict[str, Any]]:
    # GET /api/sales?ClientID=...&date_from=YYYY-MM-DD&date_till=YYYY-MM-DD :contentReference[oaicite:3]{index=3}
    date_till = datetime.now(timezone.utc).date()
    date_from = (date_till - timedelta(days=365))
    params = {
        "ClientID": str(client_id),
        "date_from": date_from.isoformat(),
        "date_till": date_till.isoformat(),
    }
    xml_text = paytraq_get("sales", params=params)
    root = parse_xml(xml_text)

    rows: List[Dict[str, Any]] = []
    for sale in root.findall("./Sale"):
        doc_id = xml_find_text(sale, "./Header/Document/DocumentID")
        doc_date = xml_find_text(sale, "./Header/Document/DocumentDate")
        doc_status = xml_find_text(sale, "./Header/Document/DocumentStatus")
        total = xml_find_text(sale, "./Header/Total")
        include_tax = xml_find_text(sale, "./Header/IncludeTax")
        rows.append({
            "DocumentID": doc_id,
            "DocumentDate": doc_date,
            "DocumentStatus": doc_status,
            "Total": total,
            "IncludeTax": include_tax,
        })

    log("PayTraq sales list (365d) -> count", {"count": len(rows), "sample_first_5": rows[:5]})
    return rows

def compute_12m_total(sales_rows: List[Dict[str, Any]]) -> float:
    # exclude voided/reversed
    bad_status = {"voided", "reversed"}
    total = 0.0
    for r in sales_rows:
        st = (r.get("DocumentStatus") or "").strip().lower()
        if st in bad_status:
            continue
        total += safe_float(r.get("Total"))
    return round(total, 2)

def detect_nitrile_and_sum(sale_root: ET.Element) -> Tuple[bool, float, Dict[str, Any]]:
    """
    Return (is_present, sum_total, debug)
    sum_total: sum of LineTotal for matching items (fallback: 0 if can't parse)
    """
    matches: List[Dict[str, Any]] = []
    sum_total = 0.0

    # We try to extract line items with:
    # - ItemCode (if present)
    # - Description / ItemDescription
    # - LineTotal
    for li in sale_root.findall("./LineItems/LineItem"):
        item_code = xml_find_text(li, "./Item/ItemCode") or xml_find_text(li, "./ItemCode")
        desc = (xml_find_text(li, "./Description") or "") + " " + (xml_find_text(li, "./ItemDescription") or "")
        desc_l = desc.lower().strip()
        line_total = xml_find_text(li, "./LineTotal") or "0"

        hit = False
        if item_code and item_code in NITRILE_SKUS:
            hit = True
        if not hit and any(k in desc_l for k in NITRILE_KEYWORDS):
            hit = True

        if hit:
            lt = safe_float(line_total)
            sum_total += lt
            matches.append({
                "ItemCode": item_code,
                "Description": desc.strip(),
                "LineTotal": line_total,
            })

    debug = {"matches": matches, "sum_total": round(sum_total, 2)}
    log("Detect nitrile -> debug", debug)
    return (len(matches) > 0), round(sum_total, 2), debug

# ----------------------------
# PIPEDRIVE
# ----------------------------
def pd_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not PIPEDRIVE_API_TOKEN:
        raise RuntimeError("PIPEDRIVE_API_TOKEN nav uzlikts env!")

    url = f"{PIPEDRIVE_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    qp = {"api_token": PIPEDRIVE_API_TOKEN}
    if params:
        qp.update(params)

    log("Pipedrive GET -> request", {"url": url, "params": qp})
    r = requests.get(url, params=qp, timeout=HTTP_TIMEOUT)
    log("Pipedrive GET -> response", {"status": r.status_code, "body_preview": (r.text[:1500] + ("...[TRUNCATED]" if len(r.text) > 1500 else ""))})
    r.raise_for_status()
    return r.json()

def pd_put(path: str, data: Dict[str, Any]) -> Dict[str, Any]:
    if not PIPEDRIVE_API_TOKEN:
        raise RuntimeError("PIPEDRIVE_API_TOKEN nav uzlikts env!")

    url = f"{PIPEDRIVE_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    qp = {"api_token": PIPEDRIVE_API_TOKEN}

    log("Pipedrive PUT -> request", {"url": url, "params": qp, "data": data})
    r = requests.put(url, params=qp, json=data, timeout=HTTP_TIMEOUT)
    log("Pipedrive PUT -> response", {"status": r.status_code, "body_preview": (r.text[:2000] + ("...[TRUNCATED]" if len(r.text) > 2000 else ""))})
    r.raise_for_status()
    return r.json()

def pd_find_org_by_regno(regno: str) -> Optional[int]:
    # Try /organizations/search (newer), fallback to /organizations/find
    term = regno.strip()
    if not term:
        return None

    # 1) search
    try:
        js = pd_get("organizations/search", params={"term": term, "exact_match": 1})
        items = (((js or {}).get("data") or {}).get("items") or [])
        if items:
            org_id = items[0].get("item", {}).get("id")
            log("Pipedrive org found via organizations/search", {"term": term, "org_id": org_id, "items_count": len(items)})
            return int(org_id) if org_id else None
    except Exception as e:
        log("Pipedrive organizations/search failed (will fallback)", {"error": str(e)})

    # 2) fallback find
    try:
        js = pd_get("organizations/find", params={"term": term, "start": 0, "limit": 10})
        data = (js or {}).get("data") or []
        if data:
            org_id = data[0].get("id")
            log("Pipedrive org found via organizations/find", {"term": term, "org_id": org_id, "items_count": len(data)})
            return int(org_id) if org_id else None
    except Exception as e:
        log("Pipedrive organizations/find failed", {"error": str(e)})

    log("Pipedrive org NOT found by regno", {"regno": regno})
    return None

def pd_find_org_by_email(email: str) -> Optional[int]:
    email = (email or "").strip()
    if not email:
        return None

    # Try search orgs by term=email
    try:
        js = pd_get("organizations/search", params={"term": email, "exact_match": 1})
        items = (((js or {}).get("data") or {}).get("items") or [])
        if items:
            org_id = items[0].get("item", {}).get("id")
            log("Pipedrive org found via organizations/search (email)", {"email": email, "org_id": org_id})
            return int(org_id) if org_id else None
    except Exception as e:
        log("Pipedrive org email search failed", {"error": str(e)})

    # Fallback to /organizations/find with search_by_email
    try:
        js = pd_get("organizations/find", params={"term": email, "search_by_email": 1, "start": 0, "limit": 10})
        data = (js or {}).get("data") or []
        if data:
            org_id = data[0].get("id")
            log("Pipedrive org found via organizations/find (email)", {"email": email, "org_id": org_id})
            return int(org_id) if org_id else None
    except Exception as e:
        log("Pipedrive org email find failed", {"error": str(e)})

    log("Pipedrive org NOT found by email", {"email": email})
    return None

def pd_get_org(org_id: int) -> Dict[str, Any]:
    js = pd_get(f"organizations/{org_id}")
    return (js or {}).get("data") or {}

def build_pd_org_update_payload(
    paytraq_client: Dict[str, Any],
    total_12m: float,
    nitrile_present: bool,
    nitrile_sum: float,
    sale_meta: Dict[str, Any],
    existing_org: Dict[str, Any],
) -> Dict[str, Any]:
    """
    IMPORTANT: we skip empty values and print what we skip.
    """
    payload: Dict[str, Any] = {}

    def set_if(value: Optional[str], key: str, label: str) -> None:
        v = (value or "").strip() if isinstance(value, str) else value
        if v is None or v == "":
            print(f"[SKIP] {label}: empty")
            return
        payload[key] = v
        print(f"[SET] {label} -> {key} = {v}")

    # Primary identifiers
    set_if(paytraq_client.get("RegNumber"), PD_ORG_REGNO_KEY, "RegNumber")
    set_if(paytraq_client.get("Email"), PD_ORG_EMAIL_KEY, "Email")
    set_if(paytraq_client.get("VatNumber"), PD_ORG_VAT_KEY, "VatNumber")
    set_if(paytraq_client.get("Phone"), PD_ORG_PHONE_KEY, "Phone")
    set_if(paytraq_client.get("LegalAddress_Country"), PD_ORG_COUNTRY_KEY, "Country")

    # Shipping address (if you want legal address to fill shipping as fallback, do it here)
    ship_addr = paytraq_client.get("LegalAddress_Address")
    ship_zip = paytraq_client.get("LegalAddress_Zip")
    ship_country = paytraq_client.get("LegalAddress_Country")
    ship_full = " ".join([x for x in [ship_addr, ship_zip, ship_country] if x and str(x).strip()])
    set_if(ship_full, PD_ORG_SHIPADDR_KEY, "Shipping address (from LegalAddress)")

    # 12m sum
    payload[PD_ORG_12M_SUM_KEY] = total_12m
    print(f"[SET] 12m sum -> {PD_ORG_12M_SUM_KEY} = {total_12m}")

    # API updated timestamp
    payload[PD_ORG_API_UPDATED_KEY] = now_iso()
    print(f"[SET] API updated -> {PD_ORG_API_UPDATED_KEY} = {payload[PD_ORG_API_UPDATED_KEY]}")

    # PG nitrile logic: date only if (a) nitrile_present and (b) org date empty or older than doc date
    if nitrile_present:
        payload[PD_PG_NITRILE_SUM_KEY] = nitrile_sum
        print(f"[SET] PG Sum Cimdi nitrila -> {PD_PG_NITRILE_SUM_KEY} = {nitrile_sum}")

        doc_date = sale_meta.get("DocumentDate")
        if doc_date:
            current_pd_date = existing_org.get(PD_PG_NITRILE_DATE_KEY)
            should_set_date = False
            if not current_pd_date:
                should_set_date = True
                print("[PG DATE] current empty -> will set")
            else:
                try:
                    # Pipedrive custom date usually "YYYY-MM-DD"
                    cur = datetime.fromisoformat(str(current_pd_date)).date()
                    new = datetime.fromisoformat(str(doc_date)).date()
                    if cur < new:
                        should_set_date = True
                        print(f"[PG DATE] current {cur} < new {new} -> will set")
                    else:
                        print(f"[PG DATE] current {cur} >= new {new} -> keep current")
                except Exception:
                    print("[PG DATE] could not parse current/new date -> will set to be safe")
                    should_set_date = True

            if should_set_date:
                payload[PD_PG_NITRILE_DATE_KEY] = doc_date
                print(f"[SET] PG Date Cimdi nitrila -> {PD_PG_NITRILE_DATE_KEY} = {doc_date}")
        else:
            print("[PG DATE] DocumentDate missing -> skip date update")
    else:
        print("[PG] nitrile not present in this sale -> skip PG fields")

    log("Pipedrive org update payload (final)", payload)
    return payload

# ----------------------------
# INPUT PARSING (HTTP / PubSub)
# ----------------------------
def extract_document_id_from_request(req_json: Dict[str, Any]) -> Optional[int]:
    """
    Supports:
    - {"DocumentID": 123} or {"document_id":123} or {"id":123}
    - Pub/Sub push: {"message":{"data":"base64(json)"}} where decoded json contains above
    - direct base64: {"data":"base64(json)"}
    """
    if not isinstance(req_json, dict):
        return None

    for k in ["DocumentID", "document_id", "doc_id", "id"]:
        if k in req_json:
            try:
                return int(req_json[k])
            except Exception:
                pass

    # Pub/Sub push format
    msg = req_json.get("message") if isinstance(req_json.get("message"), dict) else None
    if msg and "data" in msg:
        try:
            raw = base64.b64decode(msg["data"]).decode("utf-8", errors="ignore")
            log("PubSub decoded data (raw)", raw)
            inner = json.loads(raw)
            return extract_document_id_from_request(inner)
        except Exception as e:
            log("Failed to decode PubSub message.data", {"error": str(e), "trace": traceback.format_exc()})

    # generic base64 data
    if "data" in req_json and isinstance(req_json["data"], str):
        try:
            raw = base64.b64decode(req_json["data"]).decode("utf-8", errors="ignore")
            log("Decoded data (raw)", raw)
            inner = json.loads(raw)
            return extract_document_id_from_request(inner)
        except Exception as e:
            log("Failed to decode req_json.data", {"error": str(e), "trace": traceback.format_exc()})

    return None

# ----------------------------
# ROUTES
# ----------------------------
@app.get("/health")
def health():
    return jsonify({"ok": True, "env": get_env_ok()}), 200

@app.post("/debug")
def debug():
    """
    Simple manual test:
    curl -s -X POST "$URL/debug" -H "Content-Type: application/json" -d '{"DocumentID":15584268}' | python -m json.tool
    """
    req_json = request.get_json(silent=True) or {}
    log("DEBUG request JSON", req_json)
    doc_id = extract_document_id_from_request(req_json)
    if not doc_id:
        return jsonify({"ok": False, "error": "DocumentID not found in payload", "got": req_json}), 400
    result = process_document(doc_id)
    return jsonify({"ok": True, "result": result}), 200

@app.post("/run")
def run():
    """
    Main entrypoint for Cloud Run / scheduler / pubsub push.
    Accepts JSON or Pub/Sub push body.
    """
    req_json = request.get_json(silent=True) or {}
    log("RUN request JSON", req_json)

    doc_id = extract_document_id_from_request(req_json)
    if not doc_id:
        return jsonify({"ok": False, "error": "DocumentID not found in payload", "got_keys": list(req_json.keys())}), 400

    try:
        result = process_document(doc_id)
        return jsonify({"ok": True, "document_id": doc_id, "result": result}), 200
    except Exception as e:
        log("RUN FAILED", {"error": str(e), "trace": traceback.format_exc()})
        return jsonify({"ok": False, "document_id": doc_id, "error": str(e), "trace": traceback.format_exc()}), 500

# ----------------------------
# CORE
# ----------------------------
def process_document(document_id: int) -> Dict[str, Any]:
    log("PROCESS START", {"document_id": document_id, "ts": now_iso()})

    sale_root, sale_meta = get_sale(document_id)

    client_id_str = sale_meta.get("ClientID")
    if not client_id_str:
        raise RuntimeError("PayTraq sale meta: ClientID missing")
    client_id = int(client_id_str)

    client = get_client(client_id)

    # compute 12m total
    sales_rows = list_sales_for_client_365d(client_id)
    total_12m = compute_12m_total(sales_rows)
    log("Computed 12m total", {"client_id": client_id, "total_12m": total_12m})

    # detect nitrile
    nitrile_present, nitrile_sum, nitrile_debug = detect_nitrile_and_sum(sale_root)

    regno = (client.get("RegNumber") or "").strip()
    email = (client.get("Email") or "").strip()

    # find org
    org_id = None
    if regno:
        org_id = pd_find_org_by_regno(regno)
    if not org_id and email:
        org_id = pd_find_org_by_email(email)
    if not org_id:
        raise RuntimeError(f"Pipedrive Organization not found (regno={regno!r}, email={email!r})")

    existing_org = pd_get_org(org_id)
    log("Existing Pipedrive org (snapshot)", {
        "org_id": org_id,
        "name": existing_org.get("name"),
        "regno_field": existing_org.get(PD_ORG_REGNO_KEY),
        "email_field": existing_org.get(PD_ORG_EMAIL_KEY),
        "pg_nitrile_date": existing_org.get(PD_PG_NITRILE_DATE_KEY),
    })

    payload = build_pd_org_update_payload(
        paytraq_client=client,
        total_12m=total_12m,
        nitrile_present=nitrile_present,
        nitrile_sum=nitrile_sum,
        sale_meta=sale_meta,
        existing_org=existing_org,
    )

    if not payload:
        log("Nothing to update -> payload empty", {"org_id": org_id})
        return {"org_id": org_id, "updated": False, "reason": "payload empty"}

    pd_resp = pd_put(f"organizations/{org_id}", payload)

    result = {
        "org_id": org_id,
        "updated": True,
        "payload_keys": list(payload.keys()),
        "paytraq": {
            "DocumentID": sale_meta.get("DocumentID"),
            "DocumentDate": sale_meta.get("DocumentDate"),
            "DocumentRef": sale_meta.get("DocumentRef"),
            "ClientID": client.get("ClientID"),
            "ClientName": client.get("Name"),
        },
        "computed": {
            "total_12m": total_12m,
            "nitrile_present": nitrile_present,
            "nitrile_sum": nitrile_sum,
            "nitrile_matches": (nitrile_debug or {}).get("matches", []),
        },
        "pipedrive_response_ok": bool((pd_resp or {}).get("success", True)),
    }

    log("PROCESS DONE", result)
    return result

if __name__ == "__main__":
    # local dev
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
