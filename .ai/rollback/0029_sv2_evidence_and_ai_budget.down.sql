-- Rollback of 0029. Export the tables first; this deletes collected evidence and the Neuron ledger.
-- Disable first: SV2_COLLECTOR_ENABLED = "0" and SENTIMENT_V2_AI_MODEL = "" (or SV2_GLM_DAILY_NEURONS = "0").
DROP TABLE IF EXISTS sv2_ai_neurons;
DROP TABLE IF EXISTS sv2_collection_runs;
DROP TABLE IF EXISTS sv2_source_state;
DROP TABLE IF EXISTS sv2_evidence;
