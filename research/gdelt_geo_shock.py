"""GDELT 2.0 geopolitical component for the "Risk Regime Shock" research signal.

RESEARCH ONLY. Nothing here is read by V1, changes a V1 source weight, or creates a methodology
version. It turns official GDELT 2.0 Event export files into a transparent, point-in-time geopolitical
shock component that the Risk Regime Shock candidate (learning candidate, NEW_SIGNAL, REGIME_MODIFIER)
can later be tested with. The other five Risk Regime Shock inputs (oil, Treasury, equity/volatility,
USD/rates, liquidations) are deliberately out of scope.

Design rules (see research/GDELT_RISK_REGIME_SHOCK.md for the full rationale):

* Official files only: http://data.gdeltproject.org/gdeltv2/<YYYYMMDDHHMMSS>.export.CSV.zip, listed with
  size + MD5 in http://data.gdeltproject.org/gdeltv2/masterfilelist.txt. No API key, no wrapper.
* Goldstein is NOT severity. GoldsteinScale is a fixed score per CAMEO event *type* (the theoretical
  stability impact of that kind of action), identical for every instance of the type. It is used only
  for its sign (conflictual vs cooperative), as a consistency check next to QuadClass, never as a
  magnitude.
* Time = discovery time. Every event is placed at DATEADDED (the 15-minute batch in which GDELT first
  saw it), never at SQLDATE (the reported event day, often backdated). A batch is usable at time T only
  if batch_ts + AVAILABILITY_LAG <= T. Rows whose DATEADDED disagrees with their file, or whose SQLDATE
  is after DATEADDED, are rejected as look-ahead risks.
* No invented weights. Features are counts and shares; normalisation is an empirical percentile against
  the same feature's own trailing point-in-time baseline; the candidate score is the *median* of the
  feature percentiles (rank aggregation, no fitted coefficients). It is a research feature, not a V1 input.
"""
from __future__ import annotations

import bisect
import csv
import hashlib
import io
import json
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

FILTER_VERSION = "gdelt-geo-filter-v1"
FEATURE_VERSION = "gdelt-geo-features-v1"

GDELT_SOURCE = {
    "name": "GDELT 2.0 Event Database",
    "publisher": "The GDELT Project",
    "data_page": "https://www.gdeltproject.org/data.html",
    "event_files_base": "http://data.gdeltproject.org/gdeltv2/",
    "file_pattern": "http://data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.export.CSV.zip",
    "master_file_list": "http://data.gdeltproject.org/gdeltv2/masterfilelist.txt",
    "last_update": "http://data.gdeltproject.org/gdeltv2/lastupdate.txt",
    "gdelt1_daily_events": "http://data.gdeltproject.org/events/",
    "codebook": "http://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf",
    "cameo_manual": "http://data.gdeltproject.org/documentation/CAMEO.Manual.1.1b3.pdf",
    "cost": "free (open data; GDELT asks for citation)",
    "api_key_required": False,
    "update_frequency": "15 minutes",
    "history_from": "2015-02-18 (GDELT 2.0 event export files)",
    "format": "tab-delimited, 61 columns, zipped, no header",
}

# GDELT 2.0 event export columns (Event Codebook V2.0), in file order.
EXPORT_COLUMNS = (
    "GLOBALEVENTID", "SQLDATE", "MonthYear", "Year", "FractionDate",
    "Actor1Code", "Actor1Name", "Actor1CountryCode", "Actor1KnownGroupCode", "Actor1EthnicCode",
    "Actor1Religion1Code", "Actor1Religion2Code", "Actor1Type1Code", "Actor1Type2Code", "Actor1Type3Code",
    "Actor2Code", "Actor2Name", "Actor2CountryCode", "Actor2KnownGroupCode", "Actor2EthnicCode",
    "Actor2Religion1Code", "Actor2Religion2Code", "Actor2Type1Code", "Actor2Type2Code", "Actor2Type3Code",
    "IsRootEvent", "EventCode", "EventBaseCode", "EventRootCode", "QuadClass", "GoldsteinScale",
    "NumMentions", "NumSources", "NumArticles", "AvgTone",
    "Actor1Geo_Type", "Actor1Geo_FullName", "Actor1Geo_CountryCode", "Actor1Geo_ADM1Code", "Actor1Geo_ADM2Code",
    "Actor1Geo_Lat", "Actor1Geo_Long", "Actor1Geo_FeatureID",
    "Actor2Geo_Type", "Actor2Geo_FullName", "Actor2Geo_CountryCode", "Actor2Geo_ADM1Code", "Actor2Geo_ADM2Code",
    "Actor2Geo_Lat", "Actor2Geo_Long", "Actor2Geo_FeatureID",
    "ActionGeo_Type", "ActionGeo_FullName", "ActionGeo_CountryCode", "ActionGeo_ADM1Code", "ActionGeo_ADM2Code",
    "ActionGeo_Lat", "ActionGeo_Long", "ActionGeo_FeatureID",
    "DATEADDED", "SOURCEURL",
)
COL = {name: i for i, name in enumerate(EXPORT_COLUMNS)}
FIELDS_USED = (
    "GLOBALEVENTID", "SQLDATE", "Actor1CountryCode", "Actor2CountryCode", "Actor1Type1Code", "Actor2Type1Code",
    "IsRootEvent", "EventCode", "EventRootCode", "QuadClass", "GoldsteinScale", "NumMentions", "NumSources",
    "NumArticles", "ActionGeo_CountryCode", "ActionGeo_FullName", "DATEADDED", "SOURCEURL",
)

BATCH_MINUTES = 15
# A batch is treated as available 15 minutes after its timestamp: one full update cycle, which is
# conservative relative to GDELT's normal publication latency. Shorter lags are a later, measurable choice.
AVAILABILITY_LAG = timedelta(minutes=15)

# ---- Event filter (reusable CAMEO categories; not specific to any one event) ----
# CAMEO root codes (two digits). QuadClass 3 = verbal conflict, 4 = material conflict.
CATEGORY_BY_ROOT = {
    "12": "diplomatic_failure",    # REJECT (incl. rejecting negotiation / mediation)
    "13": "threat",                # THREATEN (incl. 138 threaten military force)
    "14": "political_unrest",      # PROTEST
    "15": "force_posture",         # EXHIBIT FORCE POSTURE (mobilisation, alerts)
    "16": "sanctions_reduction",   # REDUCE RELATIONS (incl. 163 embargo / sanctions)
    "17": "coercion",              # COERCE (seizure, blockade, arrests)
    "18": "assault",               # ASSAULT (incl. bombings)
    "19": "armed_conflict",        # FIGHT (military force)
    "20": "mass_violence",         # UNCONVENTIONAL MASS VIOLENCE
}
ESCALATION_ROOTS = frozenset({"15", "18", "19", "20"})          # force used or force posture
ESCALATION_CODES = frozenset({"138", "1381", "1382", "1383", "1384", "1385", "163", "171", "1711", "1712"})
# 138x = threaten military force; 163 = impose embargo/sanctions; 171x = seize/damage property (e.g. vessels).

# Optional geographic relevance: the Gulf / Middle East energy corridor (FIPS 10-4 country codes as used by
# GDELT geography fields). Configurable; the filter itself does not require it.
ENERGY_CORRIDOR_FIPS = frozenset({
    "IR", "IZ", "SA", "KU", "QA", "BA", "AE", "MU", "YM", "IS", "LE", "SY", "JO", "EG", "TU",
})
# CAMEO actor country codes for the same corridor, plus the large powers whose involvement turns a regional
# conflict into a global risk-off driver.
CORRIDOR_ACTORS = frozenset({
    "IRN", "IRQ", "SAU", "KWT", "QAT", "BHR", "ARE", "OMN", "YEM", "ISR", "LBN", "SYR", "JOR", "EGY", "TUR",
})
MAJOR_POWER_ACTORS = frozenset({"USA", "RUS", "CHN"})
# Optional context enrichment from the SOURCEURL slug only (GDELT export carries no article text).
ENERGY_URL_KEYWORDS = ("hormuz", "tanker", "oil", "crude", "opec", "pipeline", "strait", "shipping", "brent")


@dataclass(frozen=True)
class EventRow:
    event_id: int
    sql_date: str
    date_added: str
    root: str
    code: str
    quad: int
    goldstein: Optional[float]
    mentions: int
    sources: int
    articles: int
    is_root: bool
    actor1_country: str
    actor2_country: str
    actor1_type: str
    actor2_type: str
    action_country: str
    action_name: str
    url: str


def _int(v: str, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v: str) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def batch_ts_from_name(name: str) -> datetime:
    """'20260928030000.export.CSV.zip' -> 2026-09-28 03:00 UTC."""
    stamp = name.rsplit("/", 1)[-1][:14]
    return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def parse_export_lines(lines: Iterable[str], batch_stamp: str, rejects: Optional[Counter] = None) -> Iterator[EventRow]:
    """Parse tab-delimited GDELT 2.0 export lines belonging to the batch `batch_stamp` (YYYYMMDDHHMMSS).

    Rows are rejected (and counted in `rejects`) rather than repaired: wrong column count, missing event id,
    DATEADDED different from the file's batch (would place information in the wrong batch), or an event day
    after the discovery time (future-dated, a look-ahead risk)."""
    rej = rejects if rejects is not None else Counter()
    reader = csv.reader(lines, delimiter="\t", quoting=csv.QUOTE_NONE)
    for cells in reader:
        if not cells or (len(cells) == 1 and not cells[0].strip()):
            continue
        if len(cells) != len(EXPORT_COLUMNS):
            rej["bad_column_count"] += 1
            continue
        eid = _int(cells[COL["GLOBALEVENTID"]], -1)
        if eid < 0:
            rej["bad_event_id"] += 1
            continue
        added = cells[COL["DATEADDED"]].strip()
        if added != batch_stamp:
            rej["dateadded_not_this_batch"] += 1
            continue
        sql_date = cells[COL["SQLDATE"]].strip()
        if len(sql_date) == 8 and sql_date > added[:8]:
            rej["event_date_after_discovery"] += 1
            continue
        root = cells[COL["EventRootCode"]].strip().zfill(2)
        yield EventRow(
            event_id=eid, sql_date=sql_date, date_added=added, root=root,
            code=cells[COL["EventCode"]].strip(), quad=_int(cells[COL["QuadClass"]]),
            goldstein=_float(cells[COL["GoldsteinScale"]]),
            mentions=max(0, _int(cells[COL["NumMentions"]])), sources=max(0, _int(cells[COL["NumSources"]])),
            articles=max(0, _int(cells[COL["NumArticles"]])), is_root=cells[COL["IsRootEvent"]].strip() == "1",
            actor1_country=cells[COL["Actor1CountryCode"]].strip(), actor2_country=cells[COL["Actor2CountryCode"]].strip(),
            actor1_type=cells[COL["Actor1Type1Code"]].strip(), actor2_type=cells[COL["Actor2Type1Code"]].strip(),
            action_country=cells[COL["ActionGeo_CountryCode"]].strip(), action_name=cells[COL["ActionGeo_FullName"]].strip(),
            url=cells[COL["SOURCEURL"]].strip(),
        )


def parse_export_zip(data: bytes, file_name: str, rejects: Optional[Counter] = None) -> List[EventRow]:
    batch_stamp = stamp(batch_ts_from_name(file_name))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not members:
            raise ValueError(f"{file_name}: no CSV member")
        with zf.open(members[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
            return list(parse_export_lines(text, batch_stamp, rejects))


# ---- Classification ----
@dataclass(frozen=True)
class Classified:
    relevant: bool
    category: Optional[str]
    escalation: bool
    conflictual: bool          # QuadClass 3/4 AND Goldstein sign negative (direction only)
    corridor: bool             # geographic relevance (optional dimension)
    major_power: bool
    energy_context: bool       # SOURCEURL slug enrichment (weak, optional)
    terror_actor: bool
    reasons: Tuple[str, ...] = field(default_factory=tuple)


def classify(e: EventRow) -> Classified:
    category = CATEGORY_BY_ROOT.get(e.root)
    conflict_quad = e.quad in (3, 4)
    goldstein_negative = e.goldstein is not None and e.goldstein < 0
    relevant = category is not None and conflict_quad
    reasons = []
    if category:
        reasons.append(f"cameo_root_{e.root}:{category}")
    if conflict_quad:
        reasons.append(f"quadclass_{e.quad}")
    escalation = relevant and (e.root in ESCALATION_ROOTS or e.code in ESCALATION_CODES)
    countries = {e.actor1_country, e.actor2_country}
    corridor = e.action_country in ENERGY_CORRIDOR_FIPS or bool(countries & CORRIDOR_ACTORS)
    major_power = bool(countries & MAJOR_POWER_ACTORS)
    slug = (urlparse(e.url).path or "").lower()
    energy_context = any(k in slug for k in ENERGY_URL_KEYWORDS)
    terror = "TER" in (e.actor1_type, e.actor2_type)
    return Classified(relevant, category, escalation, relevant and goldstein_negative, corridor, major_power,
                      energy_context, terror, tuple(reasons))


def source_domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


# ---- Per-batch aggregation ----
def aggregate_batch(rows: Sequence[EventRow], seen_ids: Optional[set] = None) -> Dict:
    """Counts for one 15-minute batch. Duplicate GLOBALEVENTIDs (within the batch, or already seen in an
    earlier batch when `seen_ids` is shared across a run) are counted once."""
    seen = seen_ids if seen_ids is not None else set()
    total = 0
    dup = 0
    cat = Counter()
    agg = Counter()
    articles, domains, corridor_domains = set(), set(), set()
    for e in rows:
        if e.event_id in seen:
            dup += 1
            continue
        seen.add(e.event_id)
        total += 1
        c = classify(e)
        if not c.relevant:
            continue
        agg["geo_events"] += 1
        agg["geo_mentions"] += e.mentions
        agg["material_conflict"] += e.quad == 4
        agg["verbal_conflict"] += e.quad == 3
        agg["escalation_events"] += c.escalation
        agg["negative_direction_events"] += c.conflictual
        agg["corridor_events"] += c.corridor
        agg["corridor_escalation_events"] += c.corridor and c.escalation
        agg["corridor_major_power_events"] += c.corridor and c.major_power
        agg["energy_context_events"] += c.energy_context
        agg["terror_actor_events"] += c.terror_actor
        cat[c.category] += 1
        if e.url:
            articles.add(e.url)
            d = source_domain(e.url)
            if d:
                domains.add(d)
                if c.corridor:
                    corridor_domains.add(d)
    out = {k: int(agg.get(k, 0)) for k in (
        "geo_events", "geo_mentions", "material_conflict", "verbal_conflict", "escalation_events",
        "negative_direction_events", "corridor_events", "corridor_escalation_events", "corridor_major_power_events",
        "energy_context_events", "terror_actor_events")}
    out.update(total_events=total, duplicate_events=dup, geo_articles=len(articles), geo_sources=len(domains),
               corridor_sources=len(corridor_domains), categories=dict(sorted(cat.items())))
    return out


# ---- Point-in-time features and score ----
# Each feature is a count or share over the last hour (4 batches). Shares divide by GDELT's total event volume
# in the same hour, which removes time-of-day and ingestion-volume swings from the signal.
FEATURES = (
    ("geo_share", "share of all GDELT events that are conflict-category events (geopolitical_event_count, volume-adjusted)"),
    ("escalation_share", "share of all events that are force / force-posture / military-threat / sanctions / seizure events (conflict_escalation_intensity)"),
    ("corridor_escalation", "escalation events located in, or involving actors of, the Gulf / Middle East energy corridor (geographic_relevance)"),
    ("source_breadth", "distinct publishing domains reporting conflict events (geopolitical_unique_sources)"),
)
WINDOW_BATCHES = 4                 # 1 hour
BASELINE_HOURS = 72                # trailing point-in-time baseline
MIN_BASELINE_COVERAGE = 0.9        # share of baseline batches that must exist
ELEVATED_PCT = 90.0                # reporting convention: top decile of the feature's own trailing history
FRESHNESS_MAX = timedelta(minutes=45)  # newest usable batch must be at most this old at T (after the lag)


def _window_sum(series: Dict[datetime, Dict], end: datetime, n: int, key: str) -> Tuple[int, int]:
    s = 0
    present = 0
    for i in range(n):
        b = series.get(end - timedelta(minutes=BATCH_MINUTES * i))
        if b is not None:
            present += 1
            s += b.get(key, 0)
    return s, present


def window_features(series: Dict[datetime, Dict], end: datetime) -> Optional[Dict[str, float]]:
    """Features for the hour ending with batch `end` (inclusive). None if any batch of the hour is missing."""
    total, present = _window_sum(series, end, WINDOW_BATCHES, "total_events")
    if present < WINDOW_BATCHES or total <= 0:
        return None
    geo, _ = _window_sum(series, end, WINDOW_BATCHES, "geo_events")
    esc, _ = _window_sum(series, end, WINDOW_BATCHES, "escalation_events")
    cor, _ = _window_sum(series, end, WINDOW_BATCHES, "corridor_escalation_events")
    src, _ = _window_sum(series, end, WINDOW_BATCHES, "geo_sources")   # per-batch distinct domains, summed
    return {"geo_share": geo / total, "escalation_share": esc / total, "corridor_escalation": float(cor),
            "source_breadth": float(src), "geo_events": float(geo), "total_events": float(total)}


def percentile_rank(value: float, history: Sequence[float]) -> float:
    """Empirical percentile (0-100) of `value` within `history`; ties count half."""
    if not history:
        return float("nan")
    below = sum(1 for h in history if h < value)
    equal = sum(1 for h in history if h == value)
    return 100.0 * (below + 0.5 * equal) / len(history)


def usable_batch_at(series_keys: Sequence[datetime], t: datetime) -> Optional[datetime]:
    """Newest batch whose data was available at time t (batch_ts + lag <= t)."""
    cutoff = t - AVAILABILITY_LAG
    best = None
    for k in series_keys:
        if k <= cutoff and (best is None or k > best):
            best = k
    return best


def score_at(series: Dict[datetime, Dict], t: datetime) -> Dict:
    """Point-in-time geo shock component at time t. Only batches available at t are read; every returned
    value states which batch it used and why it may be unavailable."""
    keys = sorted(k for k in series if k + AVAILABILITY_LAG <= t)   # hard no-look-ahead cut
    res = {"t": t.isoformat(), "status": None, "batch_used": None, "availability_lag_min": AVAILABILITY_LAG.seconds // 60}
    if not keys:
        res["status"] = "NO_DATA"
        return res
    end = keys[-1]
    res["batch_used"] = end.isoformat()
    if t - end > FRESHNESS_MAX:
        res["status"] = "STALE"
        return res
    current = window_features(series, end)
    if current is None:
        res["status"] = "INCOMPLETE_WINDOW"
        return res
    n_base = BASELINE_HOURS * 60 // BATCH_MINUTES
    base_ends = [end - timedelta(minutes=BATCH_MINUTES * (i + WINDOW_BATCHES)) for i in range(n_base)]
    present = sum(1 for b in base_ends if b in series)
    res["baseline_coverage"] = round(present / n_base, 4)
    if present / n_base < MIN_BASELINE_COVERAGE:
        res["status"] = "INSUFFICIENT_BASELINE"
        res["features"] = {k: round(v, 6) for k, v in current.items()}
        return res
    hist = [f for f in (window_features(series, b) for b in base_ends) if f is not None]
    pct = {name: round(percentile_rank(current[name], [h[name] for h in hist]), 2) for name, _ in FEATURES}
    res.update(status="OK", features={k: round(v, 6) for k, v in current.items()}, percentiles=pct,
               geo_shock_score=round(median(pct.values()), 2), baseline_windows=len(hist))
    res["elevated"] = res["geo_shock_score"] >= ELEVATED_PCT
    return res


def persistence_hours(scores: Sequence[Dict]) -> float:
    """Longest run of consecutive elevated readings, in hours (readings assumed 15 minutes apart)."""
    best = run = 0
    for s in scores:
        run = run + 1 if s.get("elevated") else 0
        best = max(best, run)
    return best * BATCH_MINUTES / 60


def acceleration(series: Dict[datetime, Dict], end: datetime) -> Optional[float]:
    """Conflict share of the last hour relative to the median hourly share over the trailing baseline
    (ratio; 1.0 = normal). Point-in-time: uses only batches up to `end`."""
    cur = window_features(series, end)
    if cur is None:
        return None
    n_base = BASELINE_HOURS * 60 // BATCH_MINUTES
    hist = [window_features(series, end - timedelta(minutes=BATCH_MINUTES * (i + WINDOW_BATCHES))) for i in range(0, n_base, WINDOW_BATCHES)]
    vals = [h["geo_share"] for h in hist if h is not None]
    if not vals or median(vals) == 0:
        return None
    return round(cur["geo_share"] / median(vals), 4)


# ---- Required files (dynamic, minimal) ----
def required_batches(analysis_times: Iterable[datetime]) -> List[datetime]:
    """Every 15-minute batch needed to score each analysis time: its 1-hour window plus the 72-hour baseline,
    all strictly before the availability cut. Nothing beyond the latest analysis time is ever requested."""
    need = set()
    span = timedelta(hours=BASELINE_HOURS) + timedelta(minutes=BATCH_MINUTES * WINDOW_BATCHES)
    for t in analysis_times:
        cutoff = t - AVAILABILITY_LAG
        end = cutoff.replace(minute=(cutoff.minute // BATCH_MINUTES) * BATCH_MINUTES, second=0, microsecond=0)
        b = end
        while b >= end - span:
            need.add(b)
            b -= timedelta(minutes=BATCH_MINUTES)
    return sorted(need)


def export_url(b: datetime) -> str:
    return f"{GDELT_SOURCE['event_files_base']}{stamp(b)}.export.CSV.zip"


def parse_master_list(text: str) -> Dict[str, Tuple[int, str]]:
    """masterfilelist.txt lines 'size md5 url' -> {url: (size, md5)} for export files only."""
    out = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 3 and parts[2].endswith(".export.CSV.zip") and parts[0].isdigit():
            out[parts[2]] = (int(parts[0]), parts[1].lower())
    return out


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


# ---- V1 coverage ----
def v1_coverage(v1_ts_ms: Sequence[int], available_batches: Iterable[datetime]) -> Dict:
    """How many stored V1 observations could be given a point-in-time GDELT reading.

    `available_batches` are the batches known to exist (from masterfilelist or local files). An observation
    is ALIGNED if a usable batch (after the availability lag) is at most FRESHNESS_MAX old and its whole
    1-hour window exists; it HAS_CONTEXT if additionally >= 90% of its 72-hour baseline exists."""
    avail = set(available_batches)
    keys = sorted(avail)
    counts = Counter()
    n_base = BASELINE_HOURS * 60 // BATCH_MINUTES
    for ms in v1_ts_ms:
        t = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        i = bisect.bisect_right(keys, t - AVAILABILITY_LAG) - 1
        if i < 0:
            counts["excluded_no_gdelt_before"] += 1
            continue
        end = keys[i]
        if t - end > FRESHNESS_MAX:
            counts["excluded_stale_or_missing_batches"] += 1
            continue
        if any((end - timedelta(minutes=BATCH_MINUTES * j)) not in avail for j in range(WINDOW_BATCHES)):
            counts["excluded_incomplete_window"] += 1
            continue
        counts["aligned"] += 1
        present = sum(1 for j in range(n_base) if end - timedelta(minutes=BATCH_MINUTES * (j + WINDOW_BATCHES)) in avail)
        if present / n_base >= MIN_BASELINE_COVERAGE:
            counts["with_context"] += 1
        else:
            counts["excluded_insufficient_baseline"] += 1
    n = len(v1_ts_ms)
    return {
        "v1_observations": n,
        "aligned": counts["aligned"],
        "with_sufficient_context": counts["with_context"],
        "excluded": {k: counts[k] for k in ("excluded_no_gdelt_before", "excluded_stale_or_missing_batches",
                                           "excluded_incomplete_window", "excluded_insufficient_baseline")},
        # The availability lag removes ambiguous batches by construction; no observation is dropped for it.
        "excluded_lookahead_ambiguity": 0,
        "coverage_pct": round(100.0 * counts["with_context"] / n, 2) if n else 0.0,
    }


# ---- Elevated periods / false-positive analysis ----
PERSISTENT_MIN = timedelta(hours=1)   # an elevated period shorter than 1 hour is "isolated"


def elevated_periods(scores: Sequence[Dict]) -> List[Dict]:
    """Contiguous runs of elevated readings on a 15-minute grid (non-OK readings break a run)."""
    out, cur = [], None
    for s in scores:
        if s.get("status") == "OK" and s.get("elevated"):
            if cur is None:
                cur = {"start": s["t"], "end": s["t"], "points": 0, "max_score": 0.0}
            cur["end"] = s["t"]
            cur["points"] += 1
            cur["max_score"] = max(cur["max_score"], s["geo_shock_score"])
        elif cur is not None:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    for p in out:
        p["hours"] = p["points"] * BATCH_MINUTES / 60
        p["persistent"] = p["hours"] >= PERSISTENT_MIN.seconds / 3600
    return out


def false_positive_summary(scores: Sequence[Dict], series: Dict[datetime, Dict], exclude: Optional[Tuple[datetime, datetime]] = None) -> Dict:
    """Elevated-period statistics. Periods inside `exclude` (the Event #15 window) are counted separately;
    everything else is a *candidate* false positive: GDELT has no ground truth, so each one is listed with its
    dominant categories for human review rather than labelled true or false automatically."""
    ok = [s for s in scores if s.get("status") == "OK"]
    periods = elevated_periods(scores)

    def in_ex(p):
        return exclude is not None and exclude[0] <= datetime.fromisoformat(p["start"]) <= exclude[1]

    def cats(p):
        lo, hi = datetime.fromisoformat(p["start"]), datetime.fromisoformat(p["end"])
        c = Counter()
        for b, v in series.items():
            if lo - timedelta(hours=1) < b + AVAILABILITY_LAG <= hi:
                c.update(v.get("categories", {}))
        return dict(c.most_common(3))

    outside = [p for p in periods if not in_ex(p)]
    rep = sorted(outside, key=lambda p: (-p["points"], p["start"]))[:5] + [p for p in outside if not p["persistent"]][:3]
    seen, reps = set(), []
    for p in rep:
        if p["start"] not in seen:
            seen.add(p["start"])
            reps.append({**p, "top_categories": cats(p)})
    return {
        "scored_points": len(ok),
        "elevated_points": sum(1 for s in ok if s["elevated"]),
        "elevated_rate": round(sum(1 for s in ok if s["elevated"]) / len(ok), 4) if ok else None,
        "periods": len(periods),
        "isolated_periods": sum(1 for p in periods if not p["persistent"]),
        "persistent_periods": sum(1 for p in periods if p["persistent"]),
        "periods_in_event_window": len(periods) - len(outside),
        "candidate_false_positive_periods": len(outside),
        "representative_candidates": reps,
    }


def historical_scores(series: Dict[datetime, Dict], v1_ts_ms: Sequence[int]) -> Dict:
    """geo_shock_score at each stored V1 observation time (point-in-time), with status counts."""
    rows = []
    for ms in v1_ts_ms:
        t = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        r = score_at(series, t)
        rows.append({"ts": ms, "status": r["status"], "geo_shock_score": r.get("geo_shock_score"), "elevated": r.get("elevated")})
    vals = sorted(r["geo_shock_score"] for r in rows if r["geo_shock_score"] is not None)
    return {
        "status_counts": dict(sorted(Counter(r["status"] for r in rows).items())),
        "scored": len(vals),
        "elevated": sum(1 for r in rows if r["elevated"]),
        "median_score": vals[len(vals) // 2] if vals else None,
        "p90_score": vals[int(0.9 * (len(vals) - 1))] if vals else None,
        "rows": rows,
    }


def dumps_deterministic(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False, default=str) + "\n"
