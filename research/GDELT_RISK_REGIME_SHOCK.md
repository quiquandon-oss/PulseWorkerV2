# GDELT geopolitical component: Risk Regime Shock (research only)

This is the geopolitical input of the **Risk Regime Shock** NEW_SIGNAL candidate (role REGIME_MODIFIER,
discovered on Event #15). It is a research feature. It is **not** a V1 source, has **no weight**, creates
**no methodology version**, and does not replace V1's `geopolitics` or `macrogeo`.

Code: `research/gdelt_geo_shock.py` (pure logic), `research/gdelt_research_run.py` (one read-only run),
tests `research/test_gdelt_geo_shock.py`. Result artifact: `research/results/gdelt_risk_regime_shock.json`.
The learning candidate reads the artifact via `learning/research-components.js` (display only).

## Where it sits

```
Risk Regime Shock (REGIME_MODIFIER, no combined weighting yet)
 ├── GDELT geopolitical shock      <- this build
 ├── oil shock                     NOT_STARTED
 ├── Treasury shock                NOT_STARTED
 ├── equity / volatility shock     NOT_STARTED
 ├── USD / rate shock              NOT_STARTED
 └── liquidation shock             NOT_STARTED
```

## Source

| | |
|---|---|
| Dataset | GDELT 2.0 Event Database, official raw export files |
| Files | `http://data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.export.CSV.zip` |
| Index | `http://data.gdeltproject.org/gdeltv2/masterfilelist.txt` (size + MD5 + URL per file) |
| Docs | Event Codebook V2.0, CAMEO manual (`http://data.gdeltproject.org/documentation/`) |
| Cost | Free, open data; GDELT asks for citation |
| API key | None for raw files |
| Update | Every 15 minutes |
| History | GDELT 2.0 export files from 2015-02-18 (GDELT 1.0 daily files at `/events/` go further back, at daily resolution only) |

These facts come from GDELT's published documentation as built into the parser (61-column 2.0 layout). They
could **not** be re-read from this build's container: its network policy denies `gdeltproject.org` and
`data.gdeltproject.org` (HTTP 403 at the proxy). The first live run re-verifies them: the run
fails loudly if the master list or any file's size and MD5 do not match.

## Filter (`gdelt-geo-filter-v1`)

An event counts only if **both** of these hold:

- its CAMEO root is one of the conflict categories below;
- its QuadClass is 3 (verbal conflict) or 4 (material conflict).

| Root | Category | Why |
|---|---|---|
| 12 | diplomatic_failure | rejected negotiation or mediation (e.g. stalled peace talks) |
| 13 | threat | includes 138x, threats of military force |
| 14 | political_unrest | political conflict; noisy, kept as its own category |
| 15 | force_posture | mobilisation, alerts |
| 16 | sanctions_reduction | includes 163, embargo / sanctions |
| 17 | coercion | includes 171x, seizure of property (e.g. vessels) |
| 18 | assault | includes bombings (terrorism usually lands here) |
| 19 | armed_conflict | military force |
| 20 | mass_violence | unconventional mass violence |

Escalation is the subset: roots 15, 18, 19 and 20, plus codes 138x, 163 and 171x.

Optional dimensions are reported but are not required for an event to count:
- **Geographic relevance:** the action location (FIPS) or the actors (CAMEO country) are in the Gulf / Middle-East energy corridor.
- **Major-power involvement:** USA, RUS or CHN is one of the actors.
- **Terrorism actor type:** `TER`.
- **Energy context:** a keyword in the SOURCEURL slug (hormuz, tanker, oil, …). The export has no article text, so this is weak by construction.

Nothing is keyed to "Iran" or "Hormuz". Event #15 would qualify through the categories alone: U.S.–Iran
threats are roots 13/138, strikes are 18/19, talks without progress are 12, and tanker or shipping
seizures are 17/171. The corridor dimension then marks the oil-supply relevance.

**False-positive risks:**
- protests (root 14) and verbal conflict are high-volume and noisy;
- GDELT's machine coding misclassifies some articles;
- conflict far from energy or markets (local wars) raises `geo_share` without market relevance; the corridor feature and later validation must separate this;
- URL keywords match unrelated stories ("oil" in cooking oil).

## Goldstein is not severity

GoldsteinScale is a fixed score per CAMEO event **type**: every "fight" event gets −10, whatever happened.
It says nothing about how large or market-relevant a particular instance is. The component uses only its
**sign**, as a direction check next to QuadClass. It never multiplies or sums Goldstein values. A test asserts
that changing every Goldstein value from −10 to −0.1 changes no feature.

## Time and look-ahead

- **Event time vs discovery time.** `SQLDATE` is the day the event reportedly happened and is often days
  earlier than when it was reported. `DATEADDED` is the 15-minute batch in which GDELT first saw the event.
  The component places every event at **DATEADDED**. Placing it at SQLDATE would move later news back in time.
- **Availability.** A batch stamped `B` is usable at time `T` only if `B + 15 min <= T`. Fifteen minutes is one
  full update cycle, which is conservative.
- **Rejected rows.** A row is dropped rather than repaired if its DATEADDED differs from its file's batch, or if
  its SQLDATE is after its DATEADDED.
- **Scoring.** `score_at(series, T)` reads only batches that satisfy the cut. A test fills every later batch with
  extreme values and asserts the score is identical.
- **Known limitation.** Export counts (NumMentions, NumSources, NumArticles) are first-seen counts. The separate
  mentions table, which records later re-mentions with their own times, is not used yet. It would add
  persistence and breadth but roughly triples download size.

## Features (`gdelt-geo-features-v1`)

All features are computed over the last hour (4 batches), point-in-time.

| Feature | Requirement | Why |
|---|---|---|
| `geo_share` = conflict events ÷ all GDELT events | geopolitical_event_count | Dividing by total volume removes time-of-day and ingestion swings |
| `escalation_share` | conflict/escalation intensity | Force, threats of force, sanctions and seizures are the categories that move risk premia |
| `corridor_escalation` (count) | geographic relevance | Escalation located in, or involving actors of, the energy corridor |
| `source_breadth` (distinct domains) | unique sources | Separates a story many outlets carry from one outlet's burst |

Also reported, but not part of the score:
- `geo_mentions` (unique mentions);
- `geo_articles` (distinct URLs);
- `negative_direction_events` (negative Goldstein sign);
- acceleration: last hour's share ÷ the median hourly share over the 72 h baseline;
- persistence: longest run of elevated readings;
- category counts.

**Normalisation.** Each feature becomes the empirical percentile of its current value within its own
trailing 72 h of hourly values. The baseline must have ≥ 90 % of its batches present, otherwise the status is
`INSUFFICIENT_BASELINE`.

**geo_shock_score (0–100)** is the **median** of the four percentiles. This is rank aggregation with no fitted
coefficient. It is a neutral placeholder; any weighting must come from validation.

**"Elevated"** means score ≥ 90, i.e. the top decile of its own recent history. This is a reporting
convention, not a validated threshold. Synthetic tests show isolated quiet-period top-decile readings
(≤ 5 %), which is why persistence is reported next to the score.

## Run safety

- **Required files are computed** from the analysis times: the Event #15 grid ±24 h and the V1 observations
  within ±36 h, each with its 1 h window and 72 h baseline. That is 575 files for Event #15.
- **Hard cap** of 700 files per run.
- **Integrity:** every file is checked against the master list's size and MD5.
- **Caching:** files are cached under their official name, so nothing is downloaded twice; a corrupt cache entry is refetched once.
- **Retries:** up to 3 per request, with backoff.
- **Missing files** (404 or not listed) are recorded, never filled.
- **Coverage** of all stored V1 observations is measured from the master list (file availability) without downloading those files.

## Data-collection specification (future collection, not built)

| | |
|---|---|
| SOURCE | GDELT 2.0 Event Database |
| ACCESS | Official raw export CSV files |
| COST | €0 |
| API KEY | Not required |
| UPDATE / COLLECTION | Every 15 minutes, one export file per batch |
| HISTORICAL | 2015+ |

Minimal storage is **one row per 15-minute batch**: about 35,000 rows a year, never raw events. If and when
collection is approved, it would be a single research table:

```
research_gdelt_geo_batches(
  batch_ts INTEGER PRIMARY KEY,            -- DATEADDED batch (UTC ms)
  file_url TEXT, file_size INTEGER, file_md5 TEXT, fetched_ts INTEGER,
  filter_version TEXT, feature_version TEXT,
  total_events INTEGER, geo_events INTEGER, geo_mentions INTEGER, geo_articles INTEGER, geo_sources INTEGER,
  escalation_events INTEGER, corridor_events INTEGER, corridor_escalation_events INTEGER,
  negative_direction_events INTEGER, categories_json TEXT, rejects_json TEXT
)
```

- `file_md5` and the two version columns make every row reproducible from the official file.
- Features and scores are recomputed from these rows, so they are not stored.
- **No migration is part of this build.** Nothing is collected until a human approves the plan.

## Running it

```
python3 research/gdelt_research_run.py --v1 <read-only V1 extract.json> --cache <dir> \
  --out research/results/gdelt_risk_regime_shock.json
```

`--scope history` additionally scores every stored V1 observation and measures false positives across
the whole V1 history. That needs about 4,100 files, so it must be requested explicitly with
`--max-files`; the master list's byte total is recorded first. Elevated periods outside the Event #15
window are reported as **candidate** false positives, with their dominant categories for human review.
GDELT has no ground truth, so nothing is labelled true or false automatically. Earlier runs written to the
same artifact are kept under `previous_runs`.

The V1 extract is a read-only `SELECT` of stored V1 observation times plus the seven context readings
(geopolitics, macrogeo, oil, yield10y, nasdaq, sp500, usd). It is not committed. If GDELT is unreachable, the
artifact status is `LIVE_FETCH_FAILED` and it contains no GDELT results.
