-- PR4: internet evidence collection. One-to-many from research_events, as
-- documented as deferred in PR1's README.
--
-- CORRECTION from review: UNIQUE(content_hash) alone would incorrectly
-- prevent a single article from being legitimately associated with more
-- than one PR3 event (a real, valid case -- an article's publication_ts
-- can fall inside the frozen [-48h,+24h] window of two separate events).
-- content_hash is retained as a plain, non-unique column (useful for
-- global cross-event dedup/analysis later), while uniqueness is scoped to
-- (event_id, content_hash) -- one association per event, multiple
-- associations across events remain possible.
CREATE TABLE research_event_evidence (
  evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES research_events(event_id),
  feed_url TEXT NOT NULL,
  article_url TEXT NOT NULL,
  publisher TEXT NOT NULL,
  publication_ts INTEGER NOT NULL,
  collection_ts INTEGER NOT NULL,
  headline TEXT NOT NULL,           -- source-provided headline, preserved
                                     -- verbatim -- not an extracted fact
  keyword_score REAL,               -- nullable: optional enrichment,
                                     -- never a prerequisite for storage
  evidence_relation TEXT NOT NULL,  -- PRE_EVENT | POST_EVENT | SAME_WINDOW,
                                     -- derived from the three timestamps
                                     -- below, reproducible from them
  content_hash TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_evidence_event_content ON research_event_evidence(event_id, content_hash);
CREATE INDEX idx_evidence_event_id ON research_event_evidence(event_id);
CREATE INDEX idx_evidence_content_hash ON research_event_evidence(content_hash);
