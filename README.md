# NVKBA Analytics

Tournament results, standings, and trends for the Northern Virginia Kayak Bass
Anglers trail, pulled from [Fishing Chaos](https://fishingchaos.com) and
published as a static site.

**Scoring**: NVKBA scores total **length in inches** of an angler's best 5
fish (not weight). Fishing Chaos already computes this best-5 aggregate
server-side, along with each tournament's AOY points and the season-cumulative
AOY standings -- this pipeline stores those computed values rather than
re-deriving them. See [discovery/sample_response.json](discovery/sample_response.json)
for how the schema and formulas were verified against real tournament data.

## Everyday workflow

Adding a new tournament to the site is one line and a push:

1. Grab the tournament's Fishing Chaos share link.
2. Add a row to [data/tournaments.csv](data/tournaments.csv): `id,year,event_name,lake,share_url,tournament_date`.
3. Commit and push to `main`.

GitHub Actions ([.github/workflows/rebuild.yml](.github/workflows/rebuild.yml))
picks up the change, re-pulls all registered tournaments plus AOY points from
`data/series.csv`, rebuilds `data/nvkba.db`, renders the static site, and
deploys it to GitHub Pages.

A new season's AOY series can be added the same way via `data/series.csv`
(`year,series_name,share_url`, pointing at the series' Fishing Chaos share
link) -- this backfills `results.aoy_points` for tournaments already in the
registry and publishes the season's standings under `/aoy/`.

## Running locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python3 scripts/pull_all.py --delay 1.5   # fetch + normalize into data/nvkba.db
python3 scripts/build_site.py             # render site/ from the db
python3 -m http.server 8934 --directory site   # preview locally
```

`data/nvkba.db` and `data/raw/` are gitignored build artifacts -- the pipeline
rebuilds them fresh from Fishing Chaos every run (see below on why that's
cheap now), so there's nothing to keep in sync manually.

## How the data pulls work

Fishing Chaos is a Firestore-backed app, and the collections this project
reads are **public and unauthenticated** -- no API key or login needed (see
`discovery/` for how this was confirmed, including a scan of Fishing Chaos's
terms of service for any scraping restrictions -- none were found, but this
wasn't an exhaustive legal review).

Per tournament, the puller makes about 5 requests:
- the tournament document itself
- its `leaderboards` list (to find the one that scores the whole event,
  `scoringType="aggregate"` with a name matching the tournament -- side pots
  like "BIG BASS" and "KBF Biggest Pair" use the same scoring type with a
  different `aggregateLimit`)
- that leaderboard's `leaderboard-ranked` collection -- one doc per scored
  angler, with rank, best-5 total length, and the actual 5 counted fish
- `tournament-anglers-public` -- the full registered-angler roster (field size)
- `tournament-entries-public` -- every official catch, not just the counted 5,
  used to fill in `fish_catches` with a `counted_in_best5` flag

This is a big reduction from what the original brief assumed: field size and
individual fish data are both public directly, so **no per-angler page
crawling is needed** -- a full historical backfill of ~40-48 tournaments costs
roughly 200-250 requests, not the 2,000-2,900 originally estimated. Because of
that, `.github/workflows/rebuild.yml` just re-pulls everything in the registry
on every run rather than persisting `data/nvkba.db` between runs -- simpler,
and still only takes a few minutes even at full historical scale. A
configurable delay (`--delay`, default 1.5s) keeps requests polite; no rate
limiting was observed during discovery.

**Known limitation**: `tournament-anglers-public` (the roster used for field
size) is empty for some pre-2024 tournaments even though those events clearly
had registered anglers -- this looks like a Fishing Chaos data gap for older
events, not something recoverable from the public API. For those tournaments,
`field_size` falls back to "anglers who logged at least one fish" and
`field_size_is_exact` is set to `0`; the site flags this on affected
tournament pages. Zero-fish-rate stats are unavailable for those events.

## Database

SQLite at `data/nvkba.db` (see [scripts/schema.sql](scripts/schema.sql)).
Base tables: `tournaments`, `results`, `fish_catches`, `aoy_standings`.
Analytics views: `v_tournament_stats`, `v_lake_stats`, `v_angler_summary`,
`v_aoy_standings`, `v_tournament_big_fish`, `v_culling`, `v_results_enriched`.

## Project layout

```
data/
  tournaments.csv     # registry: id,year,event_name,lake,share_url,tournament_date
  series.csv           # registry: year,series_name,share_url (for AOY points)
  nvkba.db             # generated, gitignored
  raw/                 # cached raw Firestore responses, gitignored
scripts/
  firestore_client.py  # minimal public Firestore REST client
  nvkba_common.py       # shared paths/db helpers
  pull_tournament.py    # fetch + normalize one tournament
  pull_series.py         # fetch AOY points/standings for a series
  pull_all.py             # loop over the registries
  build_site.py            # render site/ from nvkba.db
  schema.sql                # tables + analytics views
templates/                  # Jinja2 templates + site/style.css source
site/                        # generated static site output, gitignored
discovery/
  sample_response.json      # example Firestore payloads from Phase 1 discovery
.github/workflows/rebuild.yml
```
