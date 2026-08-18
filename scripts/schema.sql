-- NVKBA analytics schema.
-- Scoring is total LENGTH IN INCHES of an angler's best 5 fish (not weight) --
-- see discovery/sample_response.json. Fishing Chaos already computes the
-- best-5 aggregate and (for tournaments in a series) the AOY points; this
-- pipeline stores those computed values rather than re-deriving them.

CREATE TABLE IF NOT EXISTS tournaments (
    tournament_id TEXT PRIMARY KEY,
    year INTEGER NOT NULL,
    event_name TEXT NOT NULL,
    lake TEXT NOT NULL,
    event_date DATE,
    share_url TEXT NOT NULL,
    main_leaderboard_id TEXT,      -- the scoringType="aggregate" leaderboard used for scoring
    series_id TEXT,                -- tournament-series this event belongs to, if known (for AOY)
    field_size INTEGER,            -- registered angler count
    field_size_is_exact BOOLEAN,   -- FALSE when field_size is really "anglers who scored"
                                    -- (Fishing Chaos's public roster collection is empty for
                                    -- some pre-2024 tournaments; see discovery notes)
    total_fish_entries INTEGER,    -- count of tournament-entries-public docs (all official catches)
    pulled_at TEXT                 -- ISO timestamp of last successful pull
);

CREATE TABLE IF NOT EXISTS results (
    tournament_id TEXT NOT NULL,
    angler_uid TEXT NOT NULL,      -- stable Fishing Chaos user id, used for cross-tournament identity
    angler_name TEXT NOT NULL,     -- display name at time of pull
    place INTEGER,                 -- rank on the main (best-5-length) leaderboard
    total_length_in REAL,          -- best-5 aggregate score, inches
    fish_count INTEGER,            -- number of fish counted toward total_length_in (<=5)
    big_fish_length_in REAL,       -- longest counted fish for this angler at this tournament
    aoy_points REAL,                -- points from tournament-series-leaderboard-points, NULL if
                                    -- the tournament's series hasn't been pulled
    PRIMARY KEY (tournament_id, angler_uid),
    FOREIGN KEY (tournament_id) REFERENCES tournaments(tournament_id)
);

CREATE TABLE IF NOT EXISTS fish_catches (
    tournament_id TEXT NOT NULL,
    angler_uid TEXT NOT NULL,
    angler_name TEXT NOT NULL,
    fish_number INTEGER,           -- sequential per angler per tournament, ordered by catch time
    species TEXT,
    length_in REAL,
    counted_in_best5 BOOLEAN,      -- true if this fish counted toward total_length_in
    catch_time TEXT,
    is_disqualified BOOLEAN,
    FOREIGN KEY (tournament_id) REFERENCES tournaments(tournament_id)
);

-- Season-cumulative AOY standings, taken directly from Fishing Chaos's own
-- tournament-series-ranks collection rather than summed from per-event
-- points here: NVKBA's series appears to drop each angler's worst event(s)
-- from the season total (e.g. best 7-of-8 in the 2025 season), and that
-- drop logic isn't otherwise exposed to us -- see discovery notes. The
-- per-event contributions in results.aoy_points remain useful for
-- points-per-event trend charts; this table is the source of truth for
-- final standings/rank.
CREATE TABLE IF NOT EXISTS aoy_standings (
    series_id TEXT NOT NULL,
    year INTEGER NOT NULL,
    angler_uid TEXT NOT NULL,
    angler_name TEXT,              -- club-record display name; site should prefer
                                    -- the angler's tournament-results name when available
    season_points REAL,
    season_rank INTEGER,
    events_counted INTEGER,
    PRIMARY KEY (series_id, angler_uid)
);

CREATE INDEX IF NOT EXISTS idx_results_angler ON results(angler_uid);
CREATE INDEX IF NOT EXISTS idx_results_tournament ON results(tournament_id);
CREATE INDEX IF NOT EXISTS idx_fish_tournament_angler ON fish_catches(tournament_id, angler_uid);
CREATE INDEX IF NOT EXISTS idx_results_angler_name ON results(angler_name COLLATE NOCASE);

DROP VIEW IF EXISTS v_results_enriched;
CREATE VIEW v_results_enriched AS
SELECT
    r.tournament_id, r.angler_uid, r.angler_name, r.place, r.total_length_in,
    r.fish_count, r.big_fish_length_in, r.aoy_points,
    t.year, t.event_name, t.lake, t.event_date, t.field_size, t.field_size_is_exact
FROM results r
JOIN tournaments t ON t.tournament_id = r.tournament_id;

-- Per-tournament field-wide stats: winning score, median, low scorer, zero-fish rate.
DROP VIEW IF EXISTS v_tournament_stats;
CREATE VIEW v_tournament_stats AS
WITH ranked AS (
    SELECT tournament_id, total_length_in,
           ROW_NUMBER() OVER (PARTITION BY tournament_id ORDER BY total_length_in) AS rn,
           COUNT(*) OVER (PARTITION BY tournament_id) AS cnt
    FROM results
),
medians AS (
    SELECT tournament_id, AVG(total_length_in) AS median_length
    FROM ranked
    WHERE rn IN ((cnt + 1) / 2, (cnt + 2) / 2)
    GROUP BY tournament_id
),
top10 AS (
    SELECT tournament_id, total_length_in
    FROM (
        SELECT tournament_id, total_length_in,
               ROW_NUMBER() OVER (PARTITION BY tournament_id ORDER BY place ASC) AS rn2
        FROM results
    )
    WHERE rn2 <= 10
),
top10_stats AS (
    SELECT tournament_id,
           AVG(total_length_in) AS top10_avg_length,
           SQRT(MAX(AVG(total_length_in * total_length_in) - AVG(total_length_in) * AVG(total_length_in), 0.0)) AS top10_stdev_length,
           COUNT(*) AS top10_count
    FROM top10
    GROUP BY tournament_id
),
base AS (
    SELECT tournament_id,
           MAX(total_length_in) AS winning_length,
           MIN(total_length_in) AS lowest_scored_length,
           COUNT(*) AS scored_count,
           SUM(CASE WHEN fish_count = 0 OR fish_count IS NULL THEN 1 ELSE 0 END) AS scored_with_zero_fish
    FROM results
    GROUP BY tournament_id
)
SELECT
    t.tournament_id, t.year, t.event_name, t.lake, t.event_date,
    t.field_size, t.field_size_is_exact, t.total_fish_entries,
    b.winning_length, b.lowest_scored_length, b.scored_count,
    m.median_length,
    ts.top10_avg_length, ts.top10_stdev_length, ts.top10_count,
    CASE WHEN t.field_size_is_exact THEN t.field_size - b.scored_count ELSE NULL END AS zero_fish_count,
    CASE WHEN t.field_size_is_exact AND t.field_size > 0
         THEN CAST(t.field_size - b.scored_count AS REAL) / t.field_size
         ELSE NULL END AS zero_fish_pct
FROM tournaments t
LEFT JOIN base b ON b.tournament_id = t.tournament_id
LEFT JOIN medians m ON m.tournament_id = t.tournament_id
LEFT JOIN top10_stats ts ON ts.tournament_id = t.tournament_id;

-- Per-lake historical rollup.
DROP VIEW IF EXISTS v_lake_stats;
CREATE VIEW v_lake_stats AS
SELECT
    lake,
    COUNT(DISTINCT tournament_id) AS tournament_count,
    MIN(year) AS first_year,
    MAX(year) AS last_year,
    AVG(winning_length) AS avg_winning_length,
    AVG(median_length) AS avg_median_length,
    AVG(field_size) AS avg_field_size
FROM v_tournament_stats
GROUP BY lake;

-- Per-angler career summary across all pulled tournaments. An angler_uid can
-- appear under slightly different display names across events (nickname vs
-- club-record name, spelling fixes, etc) -- group by uid only and use the
-- name from their most recent tournament as the canonical display name.
DROP VIEW IF EXISTS v_angler_summary;
CREATE VIEW v_angler_summary AS
WITH latest_name AS (
    SELECT angler_uid, angler_name
    FROM (
        SELECT angler_uid, angler_name,
               ROW_NUMBER() OVER (
                   PARTITION BY angler_uid ORDER BY year DESC, event_date DESC
               ) AS rn
        FROM v_results_enriched
    )
    WHERE rn = 1
)
SELECT
    r.angler_uid,
    n.angler_name,
    COUNT(*) AS tournaments_fished,
    AVG(r.place) AS avg_place,
    MIN(r.place) AS best_place,
    AVG(r.total_length_in) AS avg_total_length_in,
    MAX(r.total_length_in) AS best_total_length_in,
    MAX(r.big_fish_length_in) AS career_big_fish_in,
    MIN(r.year) AS first_year,
    MAX(r.year) AS last_year
FROM v_results_enriched r
JOIN latest_name n ON n.angler_uid = r.angler_uid
GROUP BY r.angler_uid, n.angler_name;

-- AOY standings as published by Fishing Chaos (see aoy_standings table),
-- with display name preferring the angler's actual tournament-results name
-- over the club-membership record name where they differ.
DROP VIEW IF EXISTS v_aoy_standings;
CREATE VIEW v_aoy_standings AS
SELECT
    a.series_id, a.year, a.angler_uid,
    COALESCE(s.angler_name, a.angler_name) AS angler_name,
    a.season_points, a.season_rank, a.events_counted
FROM aoy_standings a
LEFT JOIN v_angler_summary s ON s.angler_uid = a.angler_uid;

-- Biggest fish per tournament (field-wide big-fish stat).
DROP VIEW IF EXISTS v_tournament_big_fish;
CREATE VIEW v_tournament_big_fish AS
SELECT tournament_id, angler_name, species, length_in, catch_time
FROM (
    SELECT tournament_id, angler_name, species, length_in, catch_time,
           ROW_NUMBER() OVER (PARTITION BY tournament_id ORDER BY length_in DESC) AS rn
    FROM fish_catches
    WHERE is_disqualified = 0 OR is_disqualified IS NULL
)
WHERE rn = 1;

-- Culling activity: fish caught beyond the counted best-5, per angler per tournament.
DROP VIEW IF EXISTS v_culling;
CREATE VIEW v_culling AS
SELECT
    tournament_id, angler_uid, angler_name,
    COUNT(*) AS total_fish_caught,
    SUM(CASE WHEN counted_in_best5 THEN 1 ELSE 0 END) AS fish_counted,
    SUM(CASE WHEN NOT counted_in_best5 THEN 1 ELSE 0 END) AS fish_culled
FROM fish_catches
WHERE is_disqualified = 0 OR is_disqualified IS NULL
GROUP BY tournament_id, angler_uid, angler_name;
