-- OPTIONAL, owner decision: index btc_data(ts) so Sentiment V2's capped, newest-first price reads scan only the rows
-- they return (docs/SENTIMENT_V2_IMPLEMENTATION.md §5).
--
-- btc_data is written by V1 (the original PulseWorker). An index adds no data, changes no row and no query result, and
-- V1 keeps writing exactly as before (each insert also updates the index). If V1 already has an index whose first
-- column is ts, this is redundant and can be skipped. Without it, each V2 btc_data read scans the whole table
-- (about 3,000 rows on 2026-11-14, growing about 16 rows a day): roughly 1% of the free daily D1 read allowance.
--
-- Rollback: DROP INDEX IF EXISTS idx_btc_data_ts;

CREATE INDEX IF NOT EXISTS idx_btc_data_ts ON btc_data (ts);
