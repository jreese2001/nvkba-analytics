"""
Minimal read-only client for the public Firestore REST API used by Fishing Chaos
(project "fc-pwa"). No authentication is required for the collections this
project reads — see discovery/sample_response.json for how that was verified.

Handles pagination, a polite delay between requests, and light retry on
transient errors. Values come back in Firestore's typed-field wire format
({"stringValue": ...}, {"integerValue": ...}, etc) — `decode_fields` converts
a document's `fields` map into plain Python values.
"""
from __future__ import annotations

import time
from typing import Any, Iterator

import requests

BASE_URL = "https://firestore.googleapis.com/v1/projects/fc-pwa/databases/(default)/documents"

DEFAULT_DELAY_SECONDS = 1.5
DEFAULT_TIMEOUT = 30
MAX_RETRIES = 4


class FirestoreClient:
    def __init__(self, delay_seconds: float = DEFAULT_DELAY_SECONDS):
        self.delay_seconds = delay_seconds
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "nvkba-analytics/1.0 (+https://github.com/)"})

    def _sleep(self):
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)

    def _get(self, url: str, params: dict | None = None) -> dict:
        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = 2 ** attempt
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Failed GET {url} after {MAX_RETRIES} attempts") from last_exc

    def get_document(self, path: str) -> dict | None:
        """Fetch a single document. Returns decoded fields dict, or None if 404/403."""
        url = f"{BASE_URL}/{path}"
        data = self._get(url)
        self._sleep()
        if "error" in data:
            if data["error"]["code"] in (403, 404):
                return None
            raise RuntimeError(f"Firestore error on {path}: {data['error']}")
        return decode_fields(data.get("fields", {}))

    def list_collection(self, path: str, page_size: int = 300) -> Iterator[dict]:
        """
        Yield decoded documents from a (sub)collection, following pagination.
        Each yielded dict has decoded fields plus '_id' (the document ID).
        Yields nothing (no error) if the collection is private (403) or empty.
        """
        url = f"{BASE_URL}/{path}"
        page_token = None
        while True:
            params = {"pageSize": page_size}
            if page_token:
                params["pageToken"] = page_token
            data = self._get(url, params=params)
            self._sleep()
            if "error" in data:
                if data["error"]["code"] in (403, 404):
                    return
                raise RuntimeError(f"Firestore error listing {path}: {data['error']}")
            for doc in data.get("documents", []):
                doc_id = doc["name"].rsplit("/", 1)[-1]
                fields = decode_fields(doc.get("fields", {}))
                fields["_id"] = doc_id
                yield fields
            page_token = data.get("nextPageToken")
            if not page_token:
                return


def decode_value(value: dict) -> Any:
    if "stringValue" in value:
        return value["stringValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return value["doubleValue"]
    if "booleanValue" in value:
        return value["booleanValue"]
    if "nullValue" in value:
        return None
    if "timestampValue" in value:
        return value["timestampValue"]
    if "referenceValue" in value:
        return value["referenceValue"]
    if "geoPointValue" in value:
        gp = value["geoPointValue"]
        return {"lat": gp.get("latitude"), "lng": gp.get("longitude")}
    if "arrayValue" in value:
        return [decode_value(v) for v in value["arrayValue"].get("values", [])]
    if "mapValue" in value:
        return decode_fields(value["mapValue"].get("fields", {}))
    return None


def decode_fields(fields: dict) -> dict:
    return {k: decode_value(v) for k, v in fields.items()}
