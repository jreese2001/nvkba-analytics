"""Shared helpers for the NVKBA pipeline scripts."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from firestore_client import FirestoreClient

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "nvkba.db"
RAW_DIR = ROOT / "data" / "raw"
SCHEMA_PATH = ROOT / "scripts" / "schema.sql"
SPECIES_CACHE_PATH = RAW_DIR / "_fish_types_cache.json"

TOURNAMENT_ID_RE = re.compile(r"/tournament/([A-Za-z0-9_-]+)")
SERIES_ID_RE = re.compile(r"/tournament-series/([A-Za-z0-9_-]+)")

# Rough venue grouping for the angler "strength by venue type" breakdown.
# Standing-water lakes/reservoirs (including the multi-lake "Battle of Five
# Lakes" events) vs. freshwater smallmouth rivers vs. tidal rivers. Extend
# this if a new lake name shows up that doesn't fit -- unmapped lakes are
# left out of the category breakdown rather than guessed.
VENUE_CATEGORIES = {
    "Lake Anna": "Lakes",
    "Lake Frederick": "Lakes",
    "Occoquan Reservoir": "Lakes",
    "Multiple (Battle of Five Lakes)": "Lakes",
    "Shenandoah River": "Freshwater Rivers (Smallmouth)",
    "Upper Potomac River": "Freshwater Rivers (Smallmouth)",
    "Tidal Potomac River": "Tidal Rivers",
    "Tidal Rappahannock River": "Tidal Rivers",
    "Rappahannock River": "Tidal Rivers",
}
VENUE_CATEGORY_ORDER = ["Lakes", "Freshwater Rivers (Smallmouth)", "Tidal Rivers"]


def categorize_lake(lake: str) -> str | None:
    return VENUE_CATEGORIES.get(lake)


def extract_tournament_id(url_or_id: str) -> str:
    """Accept a full share/app URL or a bare tournament ID; return the ID."""
    m = TOURNAMENT_ID_RE.search(url_or_id)
    return m.group(1) if m else url_or_id.strip()


def extract_series_id(url_or_id: str) -> str:
    m = SERIES_ID_RE.search(url_or_id)
    return m.group(1) if m else url_or_id.strip()


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_new = not DB_PATH.exists()
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    if is_new:
        with open(SCHEMA_PATH) as f:
            con.executescript(f.read())
    else:
        # Always (re)apply view definitions so schema/view edits take effect
        # on the next pull without requiring a fresh database.
        with open(SCHEMA_PATH) as f:
            sql = f.read()
        for stmt in sql.split(";"):
            # Drop leading full-line SQL comments before checking the statement type.
            lines = stmt.strip().splitlines()
            while lines and lines[0].strip().startswith("--"):
                lines.pop(0)
            s = "\n".join(lines).strip()
            if s.upper().startswith(("CREATE VIEW", "DROP VIEW", "CREATE INDEX")):
                con.execute(s)
    return con


def species_lookup(client: FirestoreClient, fish_type_id: str | None, cache: dict) -> str | None:
    if not fish_type_id:
        return None
    if fish_type_id in cache:
        return cache[fish_type_id]
    doc = client.get_document(f"fish-types/{fish_type_id}")
    name = doc.get("nameDisplay") if doc else None
    cache[fish_type_id] = name
    save_species_cache(cache)
    return name


def load_species_cache() -> dict:
    if SPECIES_CACHE_PATH.exists():
        with open(SPECIES_CACHE_PATH) as f:
            return json.load(f)
    return {}


def save_species_cache(cache: dict):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with open(SPECIES_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def cache_path(tournament_id: str, name: str) -> Path:
    d = RAW_DIR / tournament_id
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.json"


def read_cache(tournament_id: str, name: str):
    p = cache_path(tournament_id, name)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return None


def write_cache(tournament_id: str, name: str, data):
    p = cache_path(tournament_id, name)
    with open(p, "w") as f:
        json.dump(data, f, indent=2, default=str)
