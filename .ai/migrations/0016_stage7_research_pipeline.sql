-- Stage 7: AI-assisted internet research + full-source event sentiment
-- recalculation. Read/write only against these three new tables --
-- never research_events/research_event_evidence (Stage 6's own tables,
-- preserved unchanged), never predictions/history/challenger_predictions
-- (V1/V2 production tables), never a global source-weight or model
-- parameter of any kind.
--
-- NOT APPLIED to production D1 by this change. This file is left for
-- explicit, separate authorization before it is run against the shared
-- database, per the same convention 0015's own trailing comment
-- established for a proposed-but-unapplied migration.
--
-- Three tables, one per Stage 7 lifecycle object (never collapsed):
--   stage7_research_requests  -- the structured ask (Step D/E)
--   stage7_research_responses -- the human-registered AI finding (Step H)
--   stage7_event_sentiment    -- the recalculation record (Step I)

CREATE TABLE stage7_research_requests (
  request_id TEXT PRIMARY KEY,        -- deterministic: stage7-req-<event_id>-<n>
  event_id INTEGER NOT NULL REFERENCES research_events(event_id),
  created_ts INTEGER NOT NULL,
  updated_ts INTEGER NOT NULL,
  schema_version TEXT NOT NULL,
  status TEXT NOT NULL,               -- PENDING_RESEARCH | RESEARCH_REQUEST_PUBLISHED |
                                       -- FAILED_RETRYABLE | FAILED_PERMANENT |
                                       -- RESEARCH_RESPONSE_RECEIVED | RESEARCH_VALIDATION |
                                       -- RESEARCH_COMPLETED | INTEGRATION_REVIEW |
                                       -- APPROVED_FOR_IMPLEMENTATION | INTEGRATED | REJECTED
                                       -- FAILED_PERMANENT: publish_attempts reached
                                       -- MAX_PUBLISH_ATTEMPTS (run_stage7.py) without ever
                                       -- succeeding -- never auto-retried again; needs a
                                       -- human to investigate (e.g. a revoked GITHUB_TOKEN
                                       -- or a protected branch), same as FAILED_RETRYABLE
                                       -- is never auto-retried once INTEGRATED/REJECTED.
  sufficiency_status TEXT NOT NULL,   -- INSUFFICIENT | CONFLICTING | INSUFFICIENT_EVIDENCE
                                       -- (never SUFFICIENT -- a request is only ever
                                       -- created for one of the other three, see
                                       -- research/stage7_evidence_sufficiency.py)
  reasons_json TEXT NOT NULL,         -- why the evidence is insufficient/conflicting
  questions_json TEXT NOT NULL,       -- unanswered research questions, event-specific
  missing_categories_json TEXT NOT NULL,
  historical_cutoff_ts INTEGER NOT NULL,
  evidence_snapshot_json TEXT NOT NULL, -- Stage 6 evidence_ids + a stable summary at
                                         -- request-creation time, so a later evidence
                                         -- change is visible as a diff, not silently lost
  github_path TEXT,                   -- set once the request file is actually published
  github_published_ts INTEGER,
  github_publish_error TEXT,          -- non-null => FAILED_RETRYABLE (or FAILED_PERMANENT
                                       -- once publish_attempts is exhausted); retried on
                                       -- the next scheduled run while still FAILED_RETRYABLE
  publish_attempts INTEGER NOT NULL DEFAULT 1, -- counts the initial attempt (at request
                                       -- creation) plus every subsequent retry; see
                                       -- stage7_github_publisher.decide_retry_outcome()
  input_fingerprint TEXT NOT NULL     -- hash of (event_id, sorted evidence_ids,
                                       -- sufficiency_status) -- a scheduled re-run
                                       -- recomputes this and skips creating a new
                                       -- request when it is unchanged (idempotency)
);
-- Only one NON-TERMINAL request may exist per event at a time -- this is
-- the idempotency guard repeated scheduled executions rely on. A new
-- request for the same event is only possible once the prior one reached
-- INTEGRATED or REJECTED.
CREATE UNIQUE INDEX idx_stage7_requests_active_event
  ON stage7_research_requests(event_id)
  WHERE status NOT IN ('INTEGRATED', 'REJECTED');
CREATE INDEX idx_stage7_requests_event_id ON stage7_research_requests(event_id);
CREATE INDEX idx_stage7_requests_status ON stage7_research_requests(status);

CREATE TABLE stage7_research_responses (
  response_id TEXT PRIMARY KEY,       -- deterministic: stage7-resp-<request_id>
  request_id TEXT NOT NULL REFERENCES stage7_research_requests(request_id),
  provider TEXT,                      -- human-entered label (claude/chatgpt/gemini/grok/
                                       -- unknown) -- never inferred, never assumed
  submitted_ts INTEGER,               -- when Olivier says the AI actually produced this
  registered_ts INTEGER NOT NULL,     -- when it was entered into this system
  findings_json TEXT NOT NULL,        -- {summary, transmission_mechanism,
                                       --  contradictory_evidence, unresolved_questions,
                                       --  sentiment_assessment: POSITIVE|NEGATIVE|MIXED|
                                       --  INDETERMINATE, limitations}
  sources_json TEXT NOT NULL,         -- [{url, publisher, publication_date,
                                       --   retrieval_date, claim}], AI-reported, unverified
                                       --   until validation_status = VALIDATED
  confidence TEXT,                    -- qualitative, AI/human-reported, never invented
  validation_status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING | VALIDATED | REJECTED
  validated_ts INTEGER,
  validator_notes TEXT
);
-- One response per request -- a genuinely new research pass for the same
-- event happens through a NEW request (see the partial-unique-index
-- comment above), never by overwriting a prior response.
CREATE UNIQUE INDEX idx_stage7_responses_request_id ON stage7_research_responses(request_id);

CREATE TABLE stage7_event_sentiment (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES research_events(event_id),
  calculation_ts INTEGER NOT NULL,
  formula_version TEXT NOT NULL,      -- see research/stage7_sentiment_recalculation.py's
                                       -- own FORMULA_VERSION constant
  evidence_sufficiency TEXT NOT NULL, -- SUFFICIENT | INSUFFICIENT | CONFLICTING |
                                       -- INSUFFICIENT_EVIDENCE
  sentiment_label TEXT,               -- POSITIVE | NEGATIVE | MIXED | INDETERMINATE | NULL.
                                       -- NULL means "no defensible assessment yet" --
                                       -- never coerced to a fabricated neutral value.
  sentiment_score REAL,               -- Stage 7's OWN 0-100 scale (100=POSITIVE,
                                       -- 0=NEGATIVE, 50=MIXED, NULL=INDETERMINATE/none)
                                       -- -- explicitly NOT V1's composite formula, which
                                       -- has no defined input for per-event news evidence.
                                       -- Only ever set from a VALIDATED research response's
                                       -- own sentiment_assessment -- never derived from
                                       -- evidence counts/keyword_score by this system.
  v1_macro_context_json TEXT NOT NULL, -- V1's OWN real history.score/sources_json rows
                                        -- nearest the event, read verbatim (never
                                        -- recomputed) -- see fetch_v1_macro_context()
  evidence_interpretation_json TEXT NOT NULL, -- per-source classify_event_source_
                                               -- interpretation() results, reused
                                               -- verbatim from event_source_evidence_join.py
  contributing_evidence_ids_json TEXT NOT NULL,
  excluded_evidence_json TEXT NOT NULL,       -- [{evidence_id, reason}]
  duplicate_handling_json TEXT NOT NULL,      -- content_hash groups collapsed, if any
  ai_research_response_id TEXT REFERENCES stage7_research_responses(response_id),
  previous_sentiment_id INTEGER REFERENCES stage7_event_sentiment(id),
  input_fingerprint TEXT NOT NULL     -- same purpose as the requests table's column --
                                       -- an unchanged fingerprint means the scheduled
                                       -- run must not insert a new, redundant row
);
CREATE INDEX idx_stage7_sentiment_event_id ON stage7_event_sentiment(event_id);
CREATE INDEX idx_stage7_sentiment_calculation_ts ON stage7_event_sentiment(calculation_ts);
