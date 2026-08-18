#!/usr/bin/env python3
"""
Fetch one NVKBA tournament from Fishing Chaos's public Firestore API and
normalize it into data/nvkba.db.

Usage:
    python pull_tournament.py <tournament_id_or_share_url> [--year Y --event-name N --lake L
                                                             --event-date D --share-url U]
                                                            [--delay SECONDS] [--force]

Metadata (year/event-name/lake/event-date/share-url) is normally supplied by
pull_all.py from data/tournaments.csv. When run standalone without those
flags, values are inferred from the tournament document itself where
possible.

Idempotent: re-running replaces this tournament's rows rather than
duplicating them. Raw Firestore responses are cached to
data/raw/{tournament_id}/*.json so re-normalizing doesn't require re-hitting
the network; pass --force to refetch.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from firestore_client import FirestoreClient
from nvkba_common import (
    cache_path,
    extract_tournament_id,
    get_db,
    load_species_cache,
    read_cache,
    species_lookup,
    write_cache,
)


def pick_main_leaderboard(tournament_doc: dict, leaderboards: list) -> dict:
    """
    Pick the leaderboard that scores the tournament as a whole (best-5-length),
    as opposed to side pots like "KBF Biggest Pair" or "BIG BASS" that also use
    scoringType="aggregate"/"best" but with a different aggregateLimit.

    Preferred rule: scoringType="aggregate" AND nameDisplay matches the
    tournament's own nameDisplay -- true in every tournament sampled during
    discovery. Falls back to the aggregate leaderboard with the highest
    `ranks` value (main leaderboards have ~field-size ranks; side pots have
    ranks=1) if no name match is found.
    """
    aggregate_boards = [lb for lb in leaderboards if lb.get("scoringType") == "aggregate"]
    if not aggregate_boards:
        raise RuntimeError(
            f"No scoringType='aggregate' leaderboard found "
            f"(found: {[(lb.get('_id'), lb.get('scoringType')) for lb in leaderboards]})"
        )
    tournament_name = tournament_doc.get("nameDisplay")
    name_matches = [lb for lb in aggregate_boards if lb.get("nameDisplay") == tournament_name]
    if len(name_matches) == 1:
        return name_matches[0]
    if len(name_matches) > 1:
        # ambiguous by name; fall through to ranks-based tiebreak among the matches
        aggregate_boards = name_matches
    return max(aggregate_boards, key=lambda lb: lb.get("ranks") or 0)


def fetch_raw(client: FirestoreClient, tournament_id: str, force: bool) -> dict:
    """Fetch (or load from cache) all raw collections needed for one tournament."""
    names = ["tournament", "leaderboards", "leaderboard_ranked", "anglers_public", "entries_public"]
    if not force and all(cache_path(tournament_id, n).exists() for n in names):
        return {n: read_cache(tournament_id, n) for n in names}

    tournament_doc = client.get_document(f"tournaments/{tournament_id}")
    if tournament_doc is None:
        raise RuntimeError(f"Tournament {tournament_id} not found or not public")
    write_cache(tournament_id, "tournament", tournament_doc)

    leaderboards = list(client.list_collection(f"tournaments/{tournament_id}/leaderboards"))
    write_cache(tournament_id, "leaderboards", leaderboards)

    main_leaderboard_id = pick_main_leaderboard(tournament_doc, leaderboards)["_id"]

    leaderboard_ranked = list(
        client.list_collection(
            f"tournaments/{tournament_id}/leaderboards/{main_leaderboard_id}/leaderboard-ranked"
        )
    )
    write_cache(tournament_id, "leaderboard_ranked", {"leaderboard_id": main_leaderboard_id, "docs": leaderboard_ranked})

    anglers_public = list(client.list_collection(f"tournaments/{tournament_id}/tournament-anglers-public"))
    write_cache(tournament_id, "anglers_public", anglers_public)

    entries_public = list(client.list_collection(f"tournaments/{tournament_id}/tournament-entries-public"))
    write_cache(tournament_id, "entries_public", entries_public)

    return {
        "tournament": tournament_doc,
        "leaderboards": leaderboards,
        "leaderboard_ranked": {"leaderboard_id": main_leaderboard_id, "docs": leaderboard_ranked},
        "anglers_public": anglers_public,
        "entries_public": entries_public,
    }


def normalize(tournament_id: str, raw: dict, client: FirestoreClient, species_cache: dict, meta: dict):
    tdoc = raw["tournament"]
    leaderboard_ranked = raw["leaderboard_ranked"]["docs"]
    main_leaderboard_id = raw["leaderboard_ranked"]["leaderboard_id"]
    anglers_public = raw["anglers_public"]
    entries_public = raw["entries_public"]

    # tournament-anglers-public is sometimes present but wrong (seen on a rescheduled
    # two-day 2024 event with only 1 roster doc despite 48 anglers scoring) as well as
    # sometimes empty outright on pre-2024 events -- a roster smaller than the number
    # of anglers who actually scored can't be real, so fall back to "anglers who
    # scored" in either case.
    if anglers_public and len(anglers_public) >= len(leaderboard_ranked):
        field_size = len(anglers_public)
        field_size_is_exact = True
    else:
        field_size = len(leaderboard_ranked)
        field_size_is_exact = False

    tournament_row = {
        "tournament_id": tournament_id,
        "year": meta.get("year") or _infer_year(tdoc),
        "event_name": meta.get("event_name") or tdoc.get("nameDisplay"),
        "lake": meta.get("lake") or "",
        "event_date": meta.get("event_date") or (tdoc.get("startDate") or "")[:10],
        "share_url": meta.get("share_url") or f"https://share.fishingchaos.com/tournament/{tournament_id}",
        "main_leaderboard_id": main_leaderboard_id,
        "series_id": meta.get("series_id"),
        "field_size": field_size,
        "field_size_is_exact": field_size_is_exact,
        "total_fish_entries": len(entries_public),
    }

    # angler_id (tournament-scoped) -> (uid, name), sourced from whichever ranked/roster docs we have
    angler_id_to_identity = {}
    for doc in leaderboard_ranked:
        angler = doc.get("angler") or {}
        angler_id_to_identity[doc["_id"]] = (angler.get("uid") or doc["_id"], angler.get("nameDisplay") or "Unknown")
    for doc in anglers_public:
        if doc["_id"] not in angler_id_to_identity:
            name = " ".join(filter(None, [doc.get("nameFirst"), doc.get("nameLast")])) or "Unknown"
            angler_id_to_identity[doc["_id"]] = (doc.get("uid") or doc["_id"], name)

    results_rows = []
    counted_catchlog_ids = set()
    for doc in leaderboard_ranked:
        angler = doc.get("angler") or {}
        entries = doc.get("entries") or []
        for e in entries:
            catch_data = e.get("catchData") or {}
            if catch_data.get("id"):
                counted_catchlog_ids.add(catch_data["id"])
        lengths = [e.get("length") for e in entries if e.get("length") is not None]
        results_rows.append(
            {
                "tournament_id": tournament_id,
                "angler_uid": angler.get("uid") or doc["_id"],
                "angler_name": angler.get("nameDisplay") or "Unknown",
                "place": doc.get("rank"),
                "total_length_in": doc.get("score"),
                "fish_count": doc.get("entriesCount"),
                "big_fish_length_in": max(lengths) if lengths else None,
                "aoy_points": None,
            }
        )

    # Catches per angler_uid, from the full public entries collection (includes culled fish).
    # Some tournaments run an optional side pot for a different species (snakehead side
    # challenges are the common one) using the same tournament-entries-public collection.
    # Those catches aren't part of NVKBA's bass scoring, so only keep entries that are
    # actually associated with the main (best-5-length) leaderboard -- more robust than
    # filtering by species name, since side-pot naming isn't consistent across tournaments.
    catches_by_angler: dict[str, list] = {}
    for doc in entries_public:
        if doc.get("status") != "official":
            continue
        if main_leaderboard_id not in (doc.get("leaderboardIds") or []):
            continue
        angler_id = doc.get("anglerId")
        uid, name = angler_id_to_identity.get(angler_id, (angler_id, "Unknown"))
        catches_by_angler.setdefault(uid, {"name": name, "rows": []})
        species = species_lookup(client, doc.get("fishTypeId"), species_cache)
        catches_by_angler[uid]["rows"].append(
            {
                "length_in": doc.get("length"),
                "species": species,
                "catch_time": doc.get("catchTimestamp") or doc.get("timestamp"),
                "is_disqualified": bool(doc.get("isDisqualified")),
                "counted_in_best5": doc.get("catchLogId") in counted_catchlog_ids,
            }
        )

    # Fill in big_fish_length_in from ALL non-disqualified catches (not just the counted
    # best 5). Disqualified entries are kept in fish_catches for the record but excluded
    # here -- otherwise an obvious data-entry error (e.g. a 1475" "bass") that a judge
    # disqualified would still blow out this stat.
    big_fish_by_uid = {
        uid: max(
            (r["length_in"] for r in info["rows"] if r["length_in"] is not None and not r["is_disqualified"]),
            default=None,
        )
        for uid, info in catches_by_angler.items()
    }
    for row in results_rows:
        bf = big_fish_by_uid.get(row["angler_uid"])
        if bf is not None:
            row["big_fish_length_in"] = bf

    fish_catch_rows = []
    for uid, info in catches_by_angler.items():
        sorted_rows = sorted(info["rows"], key=lambda r: r["catch_time"] or "")
        for i, r in enumerate(sorted_rows, start=1):
            fish_catch_rows.append(
                {
                    "tournament_id": tournament_id,
                    "angler_uid": uid,
                    "angler_name": info["name"],
                    "fish_number": i,
                    "species": r["species"],
                    "length_in": r["length_in"],
                    "counted_in_best5": r["counted_in_best5"],
                    "catch_time": r["catch_time"],
                    "is_disqualified": r["is_disqualified"],
                }
            )

    return tournament_row, results_rows, fish_catch_rows


def _infer_year(tournament_doc: dict) -> int | None:
    start = tournament_doc.get("startDate")
    if start and len(start) >= 4:
        return int(start[:4])
    return None


def save_to_db(tournament_row, results_rows, fish_catch_rows):
    con = get_db()
    try:
        with con:
            con.execute(
                """
                INSERT INTO tournaments (tournament_id, year, event_name, lake, event_date, share_url,
                                          main_leaderboard_id, series_id, field_size, field_size_is_exact,
                                          total_fish_entries, pulled_at)
                VALUES (:tournament_id, :year, :event_name, :lake, :event_date, :share_url,
                        :main_leaderboard_id, :series_id, :field_size, :field_size_is_exact,
                        :total_fish_entries, datetime('now'))
                ON CONFLICT(tournament_id) DO UPDATE SET
                    year=excluded.year, event_name=excluded.event_name, lake=excluded.lake,
                    event_date=excluded.event_date, share_url=excluded.share_url,
                    main_leaderboard_id=excluded.main_leaderboard_id,
                    series_id=COALESCE(excluded.series_id, tournaments.series_id),
                    field_size=excluded.field_size, field_size_is_exact=excluded.field_size_is_exact,
                    total_fish_entries=excluded.total_fish_entries, pulled_at=excluded.pulled_at
                """,
                tournament_row,
            )
            con.execute("DELETE FROM results WHERE tournament_id = ?", (tournament_row["tournament_id"],))
            con.executemany(
                """
                INSERT INTO results (tournament_id, angler_uid, angler_name, place, total_length_in,
                                      fish_count, big_fish_length_in, aoy_points)
                VALUES (:tournament_id, :angler_uid, :angler_name, :place, :total_length_in,
                        :fish_count, :big_fish_length_in, :aoy_points)
                """,
                results_rows,
            )
            con.execute("DELETE FROM fish_catches WHERE tournament_id = ?", (tournament_row["tournament_id"],))
            con.executemany(
                """
                INSERT INTO fish_catches (tournament_id, angler_uid, angler_name, fish_number, species,
                                           length_in, counted_in_best5, catch_time, is_disqualified)
                VALUES (:tournament_id, :angler_uid, :angler_name, :fish_number, :species,
                        :length_in, :counted_in_best5, :catch_time, :is_disqualified)
                """,
                fish_catch_rows,
            )
    finally:
        con.close()


def pull_tournament(
    tournament_id_or_url: str,
    delay: float,
    force: bool,
    year=None,
    event_name=None,
    lake=None,
    event_date=None,
    share_url=None,
    series_id=None,
):
    tournament_id = extract_tournament_id(tournament_id_or_url)
    client = FirestoreClient(delay_seconds=delay)
    species_cache = load_species_cache()
    raw = fetch_raw(client, tournament_id, force)
    meta = {
        "year": year,
        "event_name": event_name,
        "lake": lake,
        "event_date": event_date,
        "share_url": share_url,
        "series_id": series_id,
    }
    tournament_row, results_rows, fish_catch_rows = normalize(tournament_id, raw, client, species_cache, meta)
    save_to_db(tournament_row, results_rows, fish_catch_rows)
    print(
        f"[{tournament_id}] {tournament_row['event_name']} ({tournament_row['year']}): "
        f"{len(results_rows)} scored anglers, {len(fish_catch_rows)} fish, "
        f"field_size={tournament_row['field_size']}"
        f"{'' if tournament_row['field_size_is_exact'] else ' (approx, roster unavailable)'}"
    )
    return tournament_row


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tournament", help="Tournament ID or share URL")
    ap.add_argument("--year", type=int)
    ap.add_argument("--event-name")
    ap.add_argument("--lake")
    ap.add_argument("--event-date")
    ap.add_argument("--share-url")
    ap.add_argument("--series-id")
    ap.add_argument("--delay", type=float, default=1.5, help="Seconds between HTTP requests (default 1.5)")
    ap.add_argument("--force", action="store_true", help="Refetch from network even if cached")
    args = ap.parse_args()

    pull_tournament(
        args.tournament,
        delay=args.delay,
        force=args.force,
        year=args.year,
        event_name=args.event_name,
        lake=args.lake,
        event_date=args.event_date,
        share_url=args.share_url,
        series_id=args.series_id,
    )


if __name__ == "__main__":
    main()
