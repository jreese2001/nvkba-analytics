#!/usr/bin/env python3
"""
Fetch AOY points for a Fishing Chaos tournament series and backfill
results.aoy_points for any tournaments from that series already in the DB.

NVKBA's AOY points are computed by Fishing Chaos itself (not re-derived here)
-- see discovery/sample_response.json for how the formula was verified
(points = pointsMax - (rank-1) * pointsDecrement per tournament, with a
registration-points floor for anglers who registered but didn't score).

Usage:
    python pull_series.py <series_id_or_share_url> [--delay SECONDS]

Reads data/series.csv for registered series unless a single series is passed
on the command line. Safe to re-run; only touches results rows whose
tournament_id belongs to this series.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from firestore_client import FirestoreClient
from nvkba_common import ROOT, extract_series_id, get_db

SERIES_CSV = ROOT / "data" / "series.csv"


def pull_series(series_id_or_url: str, delay: float, year: int | None = None):
    series_id = extract_series_id(series_id_or_url)
    client = FirestoreClient(delay_seconds=delay)

    series_doc = client.get_document(f"tournament-series/{series_id}")
    if series_doc is None:
        raise RuntimeError(f"Series {series_id} not found or not public")

    leaderboards = list(client.list_collection(f"tournament-series/{series_id}/tournament-series-leaderboards"))

    con = get_db()
    try:
        known_tournament_ids = {
            r[0] for r in con.execute("SELECT tournament_id FROM tournaments").fetchall()
        }
        total_updates = 0
        for lb in leaderboards:
            tournament_id = lb.get("tournamentId")
            leaderboard_id = lb.get("_id")
            if tournament_id not in known_tournament_ids:
                continue  # tournament not pulled yet; will backfill on a later run

            points_docs = list(
                client.list_collection(
                    f"tournament-series/{series_id}/tournament-series-leaderboards/{leaderboard_id}"
                    f"/tournament-series-leaderboard-points"
                )
            )
            with con:
                con.execute(
                    "UPDATE tournaments SET series_id = ? WHERE tournament_id = ?",
                    (series_id, tournament_id),
                )
                for doc in points_docs:
                    uid = doc.get("uid")
                    points = doc.get("points")
                    if uid is None or points is None:
                        continue
                    cur = con.execute(
                        "UPDATE results SET aoy_points = ? WHERE tournament_id = ? AND angler_uid = ?",
                        (points, tournament_id, uid),
                    )
                    total_updates += cur.rowcount
            print(f"[{series_id}] {tournament_id}: {len(points_docs)} angler point records applied (per-event)")

        # Authoritative season standings (Fishing Chaos already applies whatever
        # drop-lowest / eligibility rules NVKBA configured for the series).
        rank_docs = list(client.list_collection(f"tournament-series/{series_id}/tournament-series-ranks"))
        resolved_year = year or _infer_year(rank_docs) or _infer_year_from_tournaments(con, series_id)
        with con:
            for doc in rank_docs:
                uid = doc.get("entrantId")
                if uid is None:
                    continue
                entrant = doc.get("entrant") or {}
                member = doc.get("member") or {}
                name = entrant.get("nameDisplay") or member.get("nameDisplay")
                tournaments = doc.get("tournaments") or []
                con.execute(
                    """
                    INSERT INTO aoy_standings (series_id, year, angler_uid, angler_name, season_points,
                                                season_rank, events_counted)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(series_id, angler_uid) DO UPDATE SET
                        year=excluded.year, angler_name=excluded.angler_name,
                        season_points=excluded.season_points, season_rank=excluded.season_rank,
                        events_counted=excluded.events_counted
                    """,
                    (series_id, resolved_year, uid, name, doc.get("points"), doc.get("rank"), len(tournaments)),
                )
        print(f"[{series_id}] {len(rank_docs)} season standings rows written (year={resolved_year})")
        print(f"[{series_id}] done. {total_updates} results rows updated with per-event aoy_points.")
    finally:
        con.close()


def _infer_year(rank_docs: list) -> int | None:
    for doc in rank_docs:
        ts = doc.get("timestamp")
        if ts and len(ts) >= 4:
            return int(ts[:4])
    return None


def _infer_year_from_tournaments(con, series_id: str) -> int | None:
    row = con.execute("SELECT year FROM tournaments WHERE series_id = ? LIMIT 1", (series_id,)).fetchone()
    return row[0] if row else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series", nargs="?", help="Series ID or share URL; omit to process data/series.csv")
    ap.add_argument("--delay", type=float, default=1.5)
    args = ap.parse_args()

    if args.series:
        pull_series(args.series, delay=args.delay)
        return

    if not SERIES_CSV.exists():
        print(f"No {SERIES_CSV} found and no series given on the command line; nothing to do.")
        return

    with open(SERIES_CSV, newline="") as f:
        for row in csv.DictReader(f):
            url = row.get("share_url") or row.get("series_id")
            if not url:
                continue
            year = int(row["year"]) if row.get("year") else None
            pull_series(url, delay=args.delay, year=year)


if __name__ == "__main__":
    main()
