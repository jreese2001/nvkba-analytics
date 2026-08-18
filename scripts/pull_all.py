#!/usr/bin/env python3
"""
Read data/tournaments.csv and pull any tournament not already in the DB
(or all of them, with --force). Then applies AOY points from
data/series.csv, if present.

This is the script GitHub Actions runs on every push that touches
data/tournaments.csv.

Usage:
    python pull_all.py [--delay SECONDS] [--force]
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nvkba_common import ROOT, extract_tournament_id, get_db
from pull_series import SERIES_CSV, pull_series
from pull_tournament import pull_tournament

TOURNAMENTS_CSV = ROOT / "data" / "tournaments.csv"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--delay", type=float, default=1.5, help="Seconds between HTTP requests")
    ap.add_argument("--force", action="store_true", help="Re-pull tournaments already in the DB")
    args = ap.parse_args()

    if not TOURNAMENTS_CSV.exists():
        print(f"No {TOURNAMENTS_CSV} found; nothing to do.")
        return

    con = get_db()
    known_ids = {r[0] for r in con.execute("SELECT tournament_id FROM tournaments").fetchall()}
    con.close()

    with open(TOURNAMENTS_CSV, newline="") as f:
        rows = list(csv.DictReader(f))

    pulled = 0
    for row in rows:
        share_url = row["share_url"].strip()
        tournament_id = extract_tournament_id(share_url)
        if tournament_id in known_ids and not args.force:
            continue
        pull_tournament(
            share_url,
            delay=args.delay,
            force=args.force,
            year=int(row["year"]) if row.get("year") else None,
            event_name=row.get("event_name") or None,
            lake=row.get("lake") or None,
            event_date=row.get("tournament_date") or None,
            share_url=share_url,
        )
        pulled += 1

    print(f"pull_all: {pulled} tournament(s) pulled ({len(rows)} total in registry).")

    if SERIES_CSV.exists():
        with open(SERIES_CSV, newline="") as f:
            series_rows = list(csv.DictReader(f))
        for row in series_rows:
            url = row.get("share_url") or row.get("series_id")
            if url:
                year = int(row["year"]) if row.get("year") else None
                pull_series(url, delay=args.delay, year=year)
    else:
        print(f"No {SERIES_CSV}; skipping AOY points backfill.")


if __name__ == "__main__":
    main()
