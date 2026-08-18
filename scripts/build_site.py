#!/usr/bin/env python3
"""
Render the static site in site/ from data/nvkba.db.

Usage:
    python build_site.py
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jinja2 import Environment, FileSystemLoader

from nvkba_common import DB_PATH, ROOT, VENUE_CATEGORY_ORDER, categorize_lake

TEMPLATES_DIR = ROOT / "templates"
SITE_DIR = ROOT / "site"


def slugify(text: str) -> str:
    text = (text or "unknown").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "unknown"


def rows_as_dicts(con, sql, params=()):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def build():
    if not DB_PATH.exists():
        print(f"No database at {DB_PATH}; run pull_all.py first.")
        sys.exit(1)

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    if SITE_DIR.exists():
        shutil.rmtree(SITE_DIR)
    SITE_DIR.mkdir(parents=True)
    (SITE_DIR / "tournaments").mkdir()
    (SITE_DIR / "lakes").mkdir()
    (SITE_DIR / "anglers").mkdir()
    (SITE_DIR / "aoy").mkdir()
    (SITE_DIR / "head-to-head").mkdir()
    (SITE_DIR / "explore").mkdir()

    shutil.copy(TEMPLATES_DIR / "style.css", SITE_DIR / "style.css")
    shutil.copy(TEMPLATES_DIR / "sortable.js", SITE_DIR / "sortable.js")

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=False)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    tournaments = rows_as_dicts(
        con,
        """
        SELECT s.*, t.share_url
        FROM v_tournament_stats s
        JOIN tournaments t ON t.tournament_id = s.tournament_id
        ORDER BY s.event_date DESC
        """,
    )
    for t in tournaments:
        t["lake_slug"] = slugify(t["lake"])

    lakes = rows_as_dicts(con, "SELECT * FROM v_lake_stats ORDER BY lake")
    for l in lakes:
        l["slug"] = slugify(l["lake"])

    anglers = rows_as_dicts(con, "SELECT * FROM v_angler_summary ORDER BY angler_name COLLATE NOCASE")

    totals = con.execute(
        "SELECT COUNT(*) FROM tournaments"
    ).fetchone()[0]
    total_anglers = con.execute("SELECT COUNT(DISTINCT angler_uid) FROM results").fetchone()[0]
    total_fish = con.execute("SELECT COUNT(*) FROM fish_catches").fetchone()[0]
    years = sorted({t["year"] for t in tournaments})

    # ---- index.html ----
    render(env, "index.html", SITE_DIR / "index.html", root="", generated_at=generated_at,
           total_tournaments=totals, total_anglers=total_anglers, total_fish=total_fish,
           years=years, recent_tournaments=tournaments[:10], lakes=lakes)

    # ---- tournaments/index.html ----
    by_year = {}
    for t in tournaments:
        by_year.setdefault(t["year"], []).append(t)
    by_year_sorted = sorted(by_year.items(), key=lambda kv: kv[0], reverse=True)
    render(env, "tournaments_index.html", SITE_DIR / "tournaments" / "index.html", root="../",
           generated_at=generated_at, tournaments=tournaments, by_year=by_year_sorted)

    # ---- tournaments/{id}.html ----
    culling_rows = rows_as_dicts(con, "SELECT * FROM v_culling")
    culling_by_tournament: dict[str, list] = {}
    for c in culling_rows:
        culling_by_tournament.setdefault(c["tournament_id"], []).append(c)

    for t in tournaments:
        results = rows_as_dicts(
            con,
            "SELECT * FROM results WHERE tournament_id = ? ORDER BY place ASC",
            (t["tournament_id"],),
        )
        tc = culling_by_tournament.get(t["tournament_id"], [])
        culling_summary = None
        if tc:
            culling_summary = {
                "avg_caught": sum(c["total_fish_caught"] for c in tc) / len(tc),
                "avg_counted": sum(c["fish_counted"] for c in tc) / len(tc),
            }
        culled_by_angler = {c["angler_uid"]: c["fish_culled"] for c in tc}
        render(
            env, "tournament.html", SITE_DIR / "tournaments" / f"{t['tournament_id']}.html", root="../",
            generated_at=generated_at, t=t, results=results,
            chart_labels=[f"#{r['place']} {r['angler_name']}" for r in results],
            chart_scores=[r["total_length_in"] for r in results],
            culling=culling_summary, culled_by_angler=culled_by_angler,
        )

    # ---- lakes/index.html ----
    render(env, "lakes_index.html", SITE_DIR / "lakes" / "index.html", root="../",
           generated_at=generated_at, lakes=lakes)

    # ---- lakes/{slug}.html ----
    big_fish_rows = rows_as_dicts(con, "SELECT * FROM v_tournament_big_fish")
    big_fish_by_tournament = {b["tournament_id"]: b for b in big_fish_rows}

    for l in lakes:
        lake_tournaments = [t for t in tournaments if t["lake"] == l["lake"]]
        lake_tournaments.sort(key=lambda t: t["event_date"] or "")
        for t in lake_tournaments:
            t["big_fish"] = big_fish_by_tournament.get(t["tournament_id"])
        stdevs = [t["top10_stdev_length"] for t in lake_tournaments if t["top10_stdev_length"] is not None]
        avg_top10_stdev = sum(stdevs) / len(stdevs) if stdevs else None
        render(
            env, "lake.html", SITE_DIR / "lakes" / f"{l['slug']}.html", root="../",
            generated_at=generated_at, lake=l["lake"], stats=l,
            tournaments=sorted(lake_tournaments, key=lambda t: t["event_date"] or "", reverse=True),
            chart_labels=[f"{t['year']} {t['event_date']}" for t in lake_tournaments],
            chart_winning=[t["winning_length"] for t in lake_tournaments],
            chart_median=[t["median_length"] for t in lake_tournaments],
            avg_top10_stdev=avg_top10_stdev,
        )

    # ---- anglers/index.html ----
    render(env, "anglers_index.html", SITE_DIR / "anglers" / "index.html", root="../",
           generated_at=generated_at, anglers=anglers)

    # ---- anglers/{uid}.html ----
    tournament_by_id = {t["tournament_id"]: t for t in tournaments}
    all_results = rows_as_dicts(con, "SELECT * FROM v_results_enriched")
    for r in all_results:
        r["lake_slug"] = slugify(r["lake"])
    results_by_angler = {}
    for r in all_results:
        results_by_angler.setdefault(r["angler_uid"], []).append(r)

    field_scores_by_tournament = {}
    for r in all_results:
        field_scores_by_tournament.setdefault(r["tournament_id"], []).append(r["total_length_in"])

    aoy_rows_all = rows_as_dicts(con, "SELECT * FROM v_aoy_standings ORDER BY year DESC")
    aoy_by_angler = {}
    for a in aoy_rows_all:
        aoy_by_angler.setdefault(a["angler_uid"], []).append(a)

    field_scores_by_lake: dict[str, list] = {}
    field_scores_by_category: dict[str, list] = {}
    for r in all_results:
        if r["total_length_in"] is not None:
            field_scores_by_lake.setdefault(r["lake"], []).append(r["total_length_in"])
            cat = categorize_lake(r["lake"])
            if cat:
                field_scores_by_category.setdefault(cat, []).append(r["total_length_in"])

    for a in anglers:
        uid = a["angler_uid"]
        history = sorted(results_by_angler.get(uid, []), key=lambda r: r["event_date"] or "", reverse=True)
        consistency = compute_consistency(history, field_scores_by_tournament)
        cutline_pct = compute_cutline_pct(history)
        lake_breakdown = compute_lake_breakdown(history, field_scores_by_lake)
        category_breakdown = compute_category_breakdown(history, field_scores_by_category)
        render(
            env, "angler.html", SITE_DIR / "anglers" / f"{uid}.html", root="../",
            generated_at=generated_at, angler=a, history=history,
            chart_labels=[h["event_date"] for h in reversed(history)],
            chart_scores=[h["total_length_in"] for h in reversed(history)],
            chart_places=[h["place"] for h in reversed(history)],
            chart_top10=[is_top10_pct(h) for h in reversed(history)],
            consistency=consistency, cutline_pct=cutline_pct,
            aoy_rows=aoy_by_angler.get(uid, []),
            lake_breakdown=lake_breakdown,
            category_breakdown=category_breakdown,
        )

    # ---- aoy/index.html ----
    aoy_years = sorted({a["year"] for a in aoy_rows_all}, reverse=True)
    standings_by_year = {y: [] for y in aoy_years}
    for a in aoy_rows_all:
        standings_by_year[a["year"]].append(a)
    for y in standings_by_year:
        standings_by_year[y].sort(key=lambda r: r["season_rank"] or 9999)
    render(env, "aoy.html", SITE_DIR / "aoy" / "index.html", root="../",
           generated_at=generated_at, years=aoy_years, standings_by_year=standings_by_year)

    # ---- head-to-head/index.html ----
    compact_results = [
        {
            "angler_uid": r["angler_uid"],
            "tournament_id": r["tournament_id"],
            "event_name": r["event_name"],
            "event_date": r["event_date"],
            "place": r["place"],
            "total_length_in": r["total_length_in"],
        }
        for r in all_results
    ]
    render(env, "head-to-head.html", SITE_DIR / "head-to-head" / "index.html", root="../",
           generated_at=generated_at,
           anglers=[{"angler_uid": a["angler_uid"], "angler_name": a["angler_name"]} for a in anglers],
           results=compact_results)

    # ---- explore/index.html ----
    explore_rows = [
        {
            "tournament_id": r["tournament_id"],
            "event_name": r["event_name"],
            "event_date": r["event_date"],
            "year": r["year"],
            "lake": r["lake"],
            "angler_uid": r["angler_uid"],
            "angler_name": r["angler_name"],
            "place": r["place"],
            "field_size": r["field_size"],
            "total_length_in": r["total_length_in"],
            "big_fish_length_in": r["big_fish_length_in"],
            "aoy_points": r["aoy_points"],
        }
        for r in all_results
    ]
    render(env, "explore.html", SITE_DIR / "explore" / "index.html", root="../",
           generated_at=generated_at, rows=explore_rows, years=years,
           lakes=sorted({l["lake"] for l in lakes}))

    con.close()
    print(f"Site built: {len(tournaments)} tournaments, {len(anglers)} anglers, {len(lakes)} lakes -> {SITE_DIR}")


def compute_consistency(history, field_scores_by_tournament):
    """Own stdev of total_length_in / average field stdev across the same tournaments."""
    import statistics

    scores = [h["total_length_in"] for h in history if h["total_length_in"] is not None]
    if len(scores) < 2:
        return None
    own_sd = statistics.pstdev(scores)
    field_sds = []
    for h in history:
        field_scores = [s for s in field_scores_by_tournament.get(h["tournament_id"], []) if s is not None]
        if len(field_scores) >= 2:
            field_sds.append(statistics.pstdev(field_scores))
    if not field_sds:
        return None
    avg_field_sd = sum(field_sds) / len(field_sds)
    if avg_field_sd == 0:
        return None
    return own_sd / avg_field_sd


def compute_lake_breakdown(history, field_scores_by_lake):
    """Per-lake avg place/length for this angler, vs. the field average at that lake."""
    by_lake: dict[str, list] = {}
    for h in history:
        by_lake.setdefault(h["lake"], []).append(h)

    rows = []
    for lake, events in by_lake.items():
        places = [e["place"] for e in events if e["place"] is not None]
        lengths = [e["total_length_in"] for e in events if e["total_length_in"] is not None]
        if not lengths:
            continue
        avg_length = sum(lengths) / len(lengths)
        field_scores = field_scores_by_lake.get(lake, [])
        field_avg = sum(field_scores) / len(field_scores) if field_scores else None
        rows.append(
            {
                "lake": lake,
                "lake_slug": events[0]["lake_slug"],
                "events": len(events),
                "avg_place": sum(places) / len(places) if places else None,
                "avg_length": avg_length,
                "field_avg_length": field_avg,
                "diff_vs_field": (avg_length - field_avg) if field_avg is not None else None,
            }
        )
    rows.sort(key=lambda r: r["events"], reverse=True)
    return rows


def compute_category_breakdown(history, field_scores_by_category):
    """Same idea as compute_lake_breakdown but grouped into venue types
    (Lakes / Freshwater Rivers / Tidal Rivers) instead of individual lakes."""
    by_cat: dict[str, list] = {}
    for h in history:
        cat = categorize_lake(h["lake"])
        if cat:
            by_cat.setdefault(cat, []).append(h)

    rows = []
    for cat in VENUE_CATEGORY_ORDER:
        events = by_cat.get(cat)
        if not events:
            continue
        places = [e["place"] for e in events if e["place"] is not None]
        lengths = [e["total_length_in"] for e in events if e["total_length_in"] is not None]
        if not lengths:
            continue
        avg_length = sum(lengths) / len(lengths)
        field_scores = field_scores_by_category.get(cat, [])
        field_avg = sum(field_scores) / len(field_scores) if field_scores else None
        rows.append(
            {
                "category": cat,
                "events": len(events),
                "avg_place": sum(places) / len(places) if places else None,
                "avg_length": avg_length,
                "field_avg_length": field_avg,
                "diff_vs_field": (avg_length - field_avg) if field_avg is not None else None,
            }
        )
    return rows


def is_top10_pct(result_row) -> bool:
    """True if this result placed in the top 10% of that tournament's field."""
    place, field_size = result_row.get("place"), result_row.get("field_size")
    if not place or not field_size:
        return False
    return place <= max(1, round(field_size * 0.10))


def compute_cutline_pct(history):
    """Fraction of tournaments where angler finished at/above the field median place."""
    events = [h for h in history if h.get("field_size")]
    if not events:
        return None
    above = sum(1 for h in events if h["place"] and h["place"] <= (h["field_size"] / 2))
    return above / len(events)


def render(env, template_name, out_path, **context):
    template = env.get_template(template_name)
    out_path.write_text(template.render(**context))


if __name__ == "__main__":
    build()
