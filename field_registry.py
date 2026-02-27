import os
import time
from typing import Optional, Dict, Any

import requests
from google.cloud import firestore

PIPEDRIVE_API_TOKEN = os.getenv("PIPEDRIVE_API_TOKEN")
FIRESTORE_COLLECTION = os.getenv("FIELD_KEYS_COLLECTION", "pipedrive_field_keys")

# Firestore client is safe to create once (library manages connections)
_fs = firestore.Client()


def _doc_id(entity: str, field_name: str) -> str:
    # stable id; Firestore doc ids can't contain "/"
    safe_name = field_name.replace("/", "_").strip()
    return f"{entity}|{safe_name}"


def get_cached_key(entity: str, field_name: str) -> Optional[str]:
    doc = _fs.collection(FIRESTORE_COLLECTION).document(_doc_id(entity, field_name)).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    return data.get("key")


def set_cached_key(entity: str, field_name: str, key: str, field_type: str) -> None:
    _fs.collection(FIRESTORE_COLLECTION).document(_doc_id(entity, field_name)).set(
        {
            "entity": entity,
            "field_name": field_name,
            "key": key,
            "field_type": field_type,
            "updated_at": int(time.time()),
        },
        merge=True,
    )


def create_org_field_in_pipedrive(field_name: str, field_type: str) -> str:
    """
    Create organization custom field in Pipedrive and return its 'key'.
    field_type examples: 'double', 'date', 'monetary'
    """
    if not PIPEDRIVE_API_TOKEN:
        raise RuntimeError("Missing env var: PIPEDRIVE_API_TOKEN")

    r = requests.post(
        "https://api.pipedrive.com/v1/organizationFields",
        params={"api_token": PIPEDRIVE_API_TOKEN},
        json={
            "name": field_name,
            "field_type": field_type,
        },
        timeout=30,
    )
    if r.status_code != 201 and r.status_code != 200:
        # do not print token; include status/text only
        raise RuntimeError(f"Pipedrive create field failed: {r.status_code} {r.text[:300]}")
    data = (r.json() or {}).get("data") or {}
    key = data.get("key")
    if not key:
        raise RuntimeError("Pipedrive create field returned no key")
    return key


def get_or_create_org_field_key(field_name: str, field_type: str) -> str:
    """
    1) Firestore cache
    2) Create in Pipedrive if missing
    3) Save to Firestore
    Returns field key.
    """
    cached = get_cached_key("org", field_name)
    if cached:
        return cached

    # Create new field
    key = create_org_field_in_pipedrive(field_name, field_type)
    set_cached_key("org", field_name, key, field_type)
    return key
