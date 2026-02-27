import os
import re
import time
from typing import Optional, Dict, Any, List

import requests
from google.cloud import firestore

PIPEDRIVE_API_TOKEN = os.getenv("PIPEDRIVE_API_TOKEN")
FIRESTORE_COLLECTION = os.getenv("FIELD_KEYS_COLLECTION", "pipedrive_field_keys")

GCP_PROJECT = (
    os.getenv("GOOGLE_CLOUD_PROJECT")
    or os.getenv("GCP_PROJECT")
    or os.getenv("PROJECT_ID")
)

_fs = firestore.Client(project=GCP_PROJECT) if GCP_PROJECT else firestore.Client()


def _norm_name(name: str) -> str:
    # Stabils doc_id: noņem liekas atstarpes, normalizē slash
    name = (name or "").replace("/", "_").strip()
    name = re.sub(r"\s+", " ", name)
    return name


def _doc_id(entity: str, field_name: str) -> str:
    return f"{entity}|{_norm_name(field_name)}"


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


def _pd_get_org_fields() -> List[Dict[str, Any]]:
    if not PIPEDRIVE_API_TOKEN:
        raise RuntimeError("Missing env var: PIPEDRIVE_API_TOKEN")

    r = requests.get(
        "https://api.pipedrive.com/v1/organizationFields",
        params={"api_token": PIPEDRIVE_API_TOKEN},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Pipedrive list fields failed: {r.status_code} {r.text[:300]}")
    return (r.json() or {}).get("data") or []


def _pd_find_field_by_name(field_name: str, field_type: str) -> Optional[Dict[str, Any]]:
    """
    Atrod Pipedrive laukus pēc name.
    Ja ir vairāki, izvēlas:
      1) tādu pašu field_type (ja iespējams)
      2) vecāko (pēc add_time) vai mazāko id
    """
    target = (field_name or "").strip()
    fields = _pd_get_org_fields()

    matches = [f for f in fields if (f.get("name") or "").strip() == target]
    if not matches:
        return None

    # prefer same type
    same_type = [m for m in matches if (m.get("field_type") or "").strip() == field_type]
    pool = same_type or matches

    def sort_key(x: Dict[str, Any]):
        add_time = x.get("add_time") or ""  # ISO-ish string
        fid = x.get("id") or 10**18
        return (add_time, fid)

    pool.sort(key=sort_key)
    return pool[0]


def _pd_key_exists(key: str) -> bool:
    if not key:
        return False
    fields = _pd_get_org_fields()
    return any(str(f.get("key")) == str(key) for f in fields)


def create_org_field_in_pipedrive(field_name: str, field_type: str) -> str:
    r = requests.post(
        "https://api.pipedrive.com/v1/organizationFields",
        params={"api_token": PIPEDRIVE_API_TOKEN},
        json={"name": field_name, "field_type": field_type},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Pipedrive create field failed: {r.status_code} {r.text[:300]}")

    data = (r.json() or {}).get("data") or {}
    key = data.get("key")
    if not key:
        raise RuntimeError("Pipedrive create field returned no key")
    return key


def get_or_create_org_field_key(field_name: str, field_type: str) -> str:
    # 1) Firestore cache
    cached = get_cached_key("org", field_name)
    if cached:
        # ja cache key vairs neeksistē Pipedrive (kā tu tikko izdzēsi), atjaunojam
        if _pd_key_exists(cached):
            return cached

    # 2) Pipedrive lookup pēc name (NEVEIDO dublikātu)
    existing = _pd_find_field_by_name(field_name, field_type)
    if existing and existing.get("key"):
        key = str(existing["key"])
        set_cached_key("org", field_name, key, field_type)
        return key

    # 3) Only if not found anywhere → create
    key = create_org_field_in_pipedrive(field_name, field_type)
    set_cached_key("org", field_name, key, field_type)
    return key
