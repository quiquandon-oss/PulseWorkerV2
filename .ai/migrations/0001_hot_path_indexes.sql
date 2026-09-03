-- D1 performance/index migration -- 2026-09-02
--
-- Purely additive schema change. No worker.js changes, no query text
-- changes, no model/selection/cron/Challenger/Analyst Relay behavior
-- changes. Indexes never alter what a SELECT returns, only its cost --
-- verified by the full 507/507 test suite passing unmodified before and
-- after this migration was applied.
--
-- Context: production D1 (sentiment-history, f91ca980-b886-423a-bd6f-
-- f3baea46d181) had only 3 explicit indexes prior to this migration
-- (history(ts), technical_eval(ts), selection_decisions(coin,
-- horizon_hours, ts)). Every hot-path query against predictions,
-- link_predictions, eth_predictions, challenger_predictions, btc_data,
-- link_data, and eth_data was an unindexed scan. This surfaced during
-- investigation of a Sept 1, 2026 D1 free-tier daily-limit notification
-- (Cloudflare began enforcing the 5M rows-read / 100K rows-written daily
-- caps that same day) -- see .ai/audits/ for the broader LINK/ETH
-- selection-starvation investigation this was adjacent to, but NOT
-- part of. Per explicit instruction, this migration does not claim to
-- resolve that starvation -- the time-budget hypothesis for it remains
-- unproven. This migration's own justification is the D1 rows-read
-- budget, evidenced independently below, not the starvation incident.
--
-- Applied directly to production D1 via the Cloudflare D1 MCP tool
-- (this repo has no formal migrations runner -- see convert.js/
-- STATUS.md's own precedent of out-of-band schema application). This
-- file is the audit-trail record, not an executable migration script.
--
-- ============================================================
-- Candidate 1: (horizon_hours, ts) on the three core prediction tables
-- Serves: selectBestVariant's latestCore + coreHistory queries,
-- fetchEligibilityCounts's 3 core-table subqueries, fetchVariantRowsByTable's
-- core-table scoring query, BTC's runPrediction calibration-history query.
-- Call frequency: 48/day (6 coin/horizon x 8 ticks) for selectBestVariant
-- alone, +16/day for BTC's extra calibration query.
-- Estimated unindexed cost: ~210,000 rows/day (each query-shape scanning
-- up to full table size: predictions=1006, link_predictions=947,
-- eth_predictions=250 rows at time of migration).
-- ============================================================
CREATE INDEX idx_predictions_horizon_ts ON predictions(horizon_hours, ts);
CREATE INDEX idx_link_predictions_horizon_ts ON link_predictions(horizon_hours, ts);
CREATE INDEX idx_eth_predictions_horizon_ts ON eth_predictions(horizon_hours, ts);

-- ============================================================
-- Candidate 2: (coin, horizon_hours, ts) on challenger_predictions
-- Serves: the same query family's challenger-table side (challenger_flat
-- /tilted/calibrated + BTC/LINK's momentum comparison). Every query also
-- filters by coin, so an unindexed scan touches the full ~1500-row table
-- to find the ~1/6th (per coin+horizon) that matches.
-- Call frequency: 48/day, same cadence as candidate 1.
-- Estimated unindexed cost: ~250,000-280,000 rows/day -- the single
-- largest of the four candidates.
-- ============================================================
CREATE INDEX idx_challenger_predictions_coin_horizon_ts ON challenger_predictions(coin, horizon_hours, ts);

-- ============================================================
-- Candidate 3: (ts) on the three price-history tables
-- Serves ONLY the bounded queries against these tables --
-- runChallengerPrediction's `WHERE ts <= ? ORDER BY ts DESC LIMIT 200`
-- (48/day) and backfill's rare per-row nearest-price lookup.
-- Estimated saving: ~70,000 rows/day.
--
-- Explicitly does NOT help runPrediction/runLinkPrediction/
-- runEthPrediction's own unbounded `ORDER BY ts ASC` full-history reads
-- (no WHERE clause at all -- nothing for an index to seek on). That cost
-- (~76,000 rows/day estimated) remains untouched by this migration; it
-- would require bounding the k-NN algorithm's own history window, which
-- is a model change, explicitly out of scope here. Recorded so a future
-- reader doesn't assume this migration addressed it.
-- ============================================================
CREATE INDEX idx_btc_data_ts ON btc_data(ts);
CREATE INDEX idx_link_data_ts ON link_data(ts);
CREATE INDEX idx_eth_data_ts ON eth_data(ts);

-- ============================================================
-- Candidate 4: partial indexes for unresolved backfill lookups
-- Serves: all 5 backfill* functions' unresolved-row queries. Worst
-- offender: backfillChallengerPredictions, called redundantly up to
-- 6x/tick (48/day), each scanning the full challenger_predictions table
-- to find a backlog that is consistently 0-2 rows.
-- Estimated unindexed cost: ~107,000 rows/day.
--
-- Confirmed immediately after creation: rows_written on each partial
-- index (the actual number of currently-unresolved rows it had to index)
-- was 7 (predictions), 4 (link_predictions), 11 (eth_predictions), 6
-- (challenger_predictions) -- matching the "backlog is tiny" evidence
-- exactly and validating this as the highest-ROI of the four candidates.
-- ============================================================
CREATE INDEX idx_predictions_unresolved ON predictions(target_ts) WHERE realized_up IS NULL;
CREATE INDEX idx_link_predictions_unresolved ON link_predictions(target_ts) WHERE realized_up IS NULL;
CREATE INDEX idx_eth_predictions_unresolved ON eth_predictions(target_ts) WHERE realized_up IS NULL;
CREATE INDEX idx_challenger_predictions_unresolved ON challenger_predictions(target_ts) WHERE resolved_ts IS NULL;

-- ============================================================
-- Total estimated reduction: ~650,000-800,000 rows/day (~13-16% of the
-- daily 5M free-tier cap). Material, not complete -- the unbounded
-- full-history reads (candidate 3's explicit blind spot, plus the same
-- pattern on history/technical_eval) remain the largest untouched cost
-- and were not addressed by this migration.
--
-- Verification: all 11 indexes confirmed present via
-- `SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index'`
-- immediately after application. Full test suite (507/507) run
-- unmodified before and after -- zero behavioral change, as expected
-- for a pure index addition.
-- ============================================================
