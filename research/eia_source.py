"""
EIA research-source adapter -- Phase 1 of the Source Dialogue Lab design
(SOURCE DIALOGUE LAB design doc, approved Phase-1-only).

RESEARCH ONLY. This module never writes to history/btc_data/predictions/
selection_decisions/any V1 coefficient or source-configuration table, and
never defines a production V1 source key. Its only job is: given a raw
EIA API v2 data point, decide whether that category's publication timing
can be established RELIABLY, and if so, normalize it into a ledger-ready
observation with a correct, non-approximated `information_available_at`.
If it cannot be established reliably, this module refuses to produce a
usable observation for that category at all (`normalize_observation`
raises) -- there is no silent/partial/best-guess path.

=====================================================================
CRITICAL TEMPORAL GATE -- how each category was actually assessed
=====================================================================

`observation_period` (the period/date the value DESCRIBES) is never
equated with `information_available_at` (the moment that value became
publicly knowable) -- these are always two distinct fields.

This session verified live network access to BOTH www.eia.gov and
api.eia.gov is blocked by this development sandbox's own egress policy
(confirmed via a direct connection attempt -- gateway returned 403 to
CONNECT for both hosts, a hard organizational policy denial, not a
missing-tool limitation). No EIA documentation page or API response was
fetched live in this session. Every release-schedule and series-identity
claim below rests on independent secondary sources found via web search,
not a first-party fetch of EIA's own docs -- this is disclosed here and
in EIA_SERIES_REGISTRY's own `verified_in_this_session` field, never
hidden. GitHub Actions runners (where this adapter actually runs in
production) have ordinary internet access and are NOT subject to this
sandbox's block -- but `verify_registry_before_first_production_use()`
below still deliberately raises until a human has performed one real,
live confirmation, precisely because this session could not.

Per-category verdict (see EIA_SERIES_REGISTRY for the full detail):

- EIA_INVENTORY, EIA_PRODUCTION, EIA_DEMAND: all three are components of
  the Weekly Petroleum Status Report, whose Tables 1-14 (CSV/XLS -- the
  API's own underlying data) are released Wednesdays after 10:30 a.m.
  Eastern, shifted to Thursday for a week containing a federal holiday
  (a documented, decades-stable EIA policy, corroborated across multiple
  independent secondary sources). TEMPORALLY_SAFE = True, computed via
  `compute_wpsr_release_ts()` below, which itself refuses (raises) for
  any year its own federal-holiday table hasn't been reviewed for --
  never silently falls back to the base Wednesday rule for an unreviewed
  year.
- EIA_OIL, EIA_GAS (raw daily spot prices): secondary sources conflicted
  on the exact daily release time (one source: "7:30-8:30 a.m.
  typically"; another: "~10:00 a.m. Tuesday" -- apparently describing a
  DIFFERENT, weekly report, not the daily series). No single, reliable,
  citable time could be established in this session.
  TEMPORALLY_SAFE = False for Phase 1. A weekly-average variant of both
  commodities exists inside the same well-documented weekly report
  family (This Week In Petroleum / Natural Gas Weekly Update) and is a
  promising Phase-2 candidate, but its own exact release schedule was
  also not independently confirmed here -- left for a future,
  specifically-scoped follow-up, not implemented now.
- EIA_ELECTRICITY: Electric Power Monthly is documented only as released
  "during the last week of the month" -- no specific day, no time.
  TEMPORALLY_SAFE = False.
- EIA_ENERGY_SHOCK: DERIVED, never an EIA-published field. Computed only
  from the three TEMPORALLY_SAFE weekly-petroleum categories (never from
  OIL/GAS/ELECTRICITY, which are themselves unsafe) -- its own
  `information_available_at` is the max of its inputs' own, so it
  inherits safety from them rather than asserting a new one.
  TEMPORALLY_SAFE = True (scoped to safe inputs only).
- EIA_ENERGY_PRICE: NOT created in Phase 1, per explicit instruction
  (redundant with OIL/GAS/ELECTRICITY already existing as candidates).

$0: EIA API v2 is free to register for (no cost, no card -- confirmed via
web search of EIA's own registration page description) and is read-only
here, bound to explicit date ranges, never the full catalogue. Requires
one new credential, `EIA_API_KEY` (an environment variable read once at
call time and never logged, matching the existing `CLOUDFLARE_API_TOKEN`
handling convention in exp005/exp009's own run_experiment.py).
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PROVIDER = "EIA"

EIA_CATEGORIES = (
    "EIA_OIL",
    "EIA_GAS",
    "EIA_ELECTRICITY",
    "EIA_INVENTORY",
    "EIA_PRODUCTION",
    "EIA_DEMAND",
    "EIA_ENERGY_SHOCK",
)
# EIA_ENERGY_PRICE deliberately does not exist -- explicit instruction for
# Phase 1 (redundant with OIL/GAS/ELECTRICITY already covering "price").

_EASTERN = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------
# Weekly Petroleum Status Report release-schedule model
# ---------------------------------------------------------------------

WPSR_RELEASE_HOUR_ET = 10
WPSR_RELEASE_MINUTE_ET = 30

# Years this table's federal-holiday computation has actually been
# reviewed for. compute_wpsr_release_ts() refuses (raises) for any
# period whose release-week falls in a year outside this set -- adding a
# new year here is a deliberate, reviewed act, never an implicit
# extrapolation.
WPSR_HOLIDAY_TABLE_REVIEWED_YEARS = frozenset({2024, 2025, 2026, 2027})


def _nth_weekday_of_month(year, month, weekday, n):
    """The date of the n-th given weekday (Mon=0) in a month, 1-indexed."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday_of_month(year, month, weekday):
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)


def _observed(d):
    """US federal holidays falling on a weekend are observed on the
    nearest weekday (Saturday -> Friday, Sunday -> Monday) -- the
    standard, long-standing US federal observance rule."""
    if d.weekday() == 5:  # Saturday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d


def us_federal_holidays(year):
    """The fixed set of US federal holidays that can shift the WPSR
    Wednesday release to Thursday, for one calendar year. Deterministic,
    no external data -- the same 11 holidays every year, only their
    calendar dates change."""
    return {
        _observed(date(year, 1, 1)),                    # New Year's Day
        _nth_weekday_of_month(year, 1, 0, 3),            # MLK Day (3rd Mon)
        _nth_weekday_of_month(year, 2, 0, 3),            # Presidents Day (3rd Mon)
        _last_weekday_of_month(year, 5, 0),              # Memorial Day (last Mon)
        _observed(date(year, 6, 19)),                    # Juneteenth
        _observed(date(year, 7, 4)),                     # Independence Day
        _nth_weekday_of_month(year, 9, 0, 1),            # Labor Day (1st Mon)
        _nth_weekday_of_month(year, 10, 0, 2),           # Columbus Day (2nd Mon)
        _observed(date(year, 11, 11)),                   # Veterans Day
        _nth_weekday_of_month(year, 11, 3, 4),           # Thanksgiving (4th Thu)
        _observed(date(year, 12, 25)),                   # Christmas Day
    }


def compute_wpsr_release_ts(period_end_date):
    """The Weekly Petroleum Status Report's own release instant (as a ms
    epoch, UTC) for the week ENDING on `period_end_date` (a `date`,
    always a Friday per EIA's own weekly convention). Wednesday
    10:30 a.m. Eastern, shifted to Thursday same time if that Wednesday
    is a US federal holiday.

    Raises ValueError -- never silently falls back to the base rule --
    if the release week's year has not been reviewed for this table
    (see WPSR_HOLIDAY_TABLE_REVIEWED_YEARS)."""
    if period_end_date.weekday() != 4:
        raise ValueError(f"WPSR period_end_date must be a Friday, got {period_end_date} (weekday {period_end_date.weekday()})")
    release_wednesday = period_end_date + timedelta(days=5)
    if release_wednesday.year not in WPSR_HOLIDAY_TABLE_REVIEWED_YEARS:
        raise ValueError(
            f"WPSR holiday table has not been reviewed for {release_wednesday.year} -- "
            f"refusing to guess a release date rather than risk an unverified assumption. "
            f"Extend WPSR_HOLIDAY_TABLE_REVIEWED_YEARS only after reviewing that year's actual "
            f"EIA holiday release schedule."
        )
    holidays = us_federal_holidays(release_wednesday.year)
    release_date = release_wednesday + timedelta(days=1) if release_wednesday in holidays else release_wednesday
    release_dt_et = datetime(
        release_date.year, release_date.month, release_date.day,
        WPSR_RELEASE_HOUR_ET, WPSR_RELEASE_MINUTE_ET, tzinfo=_EASTERN,
    )
    return int(release_dt_et.astimezone(timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------------
# Series registry -- WHAT each category is, and its temporal verdict.
# Route/series-identifier values are the best-available PUBLICLY
# DOCUMENTED EIA identifiers as of this build (long-standing, widely
# referenced legacy series IDs) -- explicitly NOT live-verified against
# a real API call in this session (see module docstring). Every entry
# carries its own `verified_in_this_session` flag so this limitation can
# never be silently forgotten by a future reader of this file.
# ---------------------------------------------------------------------

EIA_SERIES_REGISTRY = {
    "EIA_INVENTORY": {
        "provider": PROVIDER,
        "frequency": "weekly",
        "series_reference": "Weekly U.S. Ending Stocks of Crude Oil excluding SPR (Weekly Petroleum Status Report)",
        "series_id_candidate": "PET.WCESTUS1.W",
        "landing_page": "https://www.eia.gov/petroleum/weekly/",
        "release_schedule": "WPSR_WEDNESDAY_1030_ET",
        "temporally_safe": True,
        "verified_in_this_session": False,
    },
    "EIA_PRODUCTION": {
        "provider": PROVIDER,
        "frequency": "weekly",
        "series_reference": "Weekly U.S. Field Production of Crude Oil (Weekly Petroleum Status Report)",
        "series_id_candidate": "PET.WCRFPUS2.W",
        "landing_page": "https://www.eia.gov/petroleum/weekly/",
        "release_schedule": "WPSR_WEDNESDAY_1030_ET",
        "temporally_safe": True,
        "verified_in_this_session": False,
    },
    "EIA_DEMAND": {
        "provider": PROVIDER,
        "frequency": "weekly",
        "series_reference": "Weekly U.S. Product Supplied of Petroleum Products (demand proxy, Weekly Petroleum Status Report)",
        "series_id_candidate": "PET.WRPUPUS2.W",
        "landing_page": "https://www.eia.gov/petroleum/weekly/",
        "release_schedule": "WPSR_WEDNESDAY_1030_ET",
        "temporally_safe": True,
        "verified_in_this_session": False,
    },
    "EIA_OIL": {
        "provider": PROVIDER,
        "frequency": "daily",
        "series_reference": "Cushing, OK WTI Spot Price FOB (daily)",
        "series_id_candidate": "PET.RWTC.D",
        "landing_page": "https://www.eia.gov/petroleum/data.php",
        "release_schedule": None,
        "temporally_safe": False,
        "verified_in_this_session": False,
        "unsafe_reason": (
            "Secondary sources conflicted on the exact daily release time "
            "(one: 7:30-8:30am typical; another: ~10:00am Tuesday, which "
            "appears to describe a different, weekly report, not this "
            "daily series). No single reliable, citable release time was "
            "established -- eia.gov/api.eia.gov were both unreachable "
            "from this session's sandbox to resolve the conflict directly."
        ),
    },
    "EIA_GAS": {
        "provider": PROVIDER,
        "frequency": "daily",
        "series_reference": "Henry Hub Natural Gas Spot Price (daily)",
        "series_id_candidate": "NG.RNGWHHD.D",
        "landing_page": "https://www.eia.gov/naturalgas/weekly/",
        "release_schedule": None,
        "temporally_safe": False,
        "verified_in_this_session": False,
        "unsafe_reason": (
            "Same conflicting-secondary-source problem as EIA_OIL -- no "
            "single reliable, citable daily release time was established "
            "in this session."
        ),
    },
    "EIA_ELECTRICITY": {
        "provider": PROVIDER,
        "frequency": "monthly",
        "series_reference": "Electric Power Monthly",
        "series_id_candidate": None,
        "landing_page": "https://www.eia.gov/electricity/monthly/",
        "release_schedule": None,
        "temporally_safe": False,
        "verified_in_this_session": False,
        "unsafe_reason": (
            "Only documented, via secondary sources, as released 'during "
            "the last week of the month' -- no specific day or time is "
            "given anywhere found in this session, so a per-observation "
            "information_available_at cannot be established reliably."
        ),
    },
    "EIA_ENERGY_SHOCK": {
        "provider": PROVIDER,
        "frequency": "weekly",
        "series_reference": "Deterministic derived research signal -- NOT an EIA-published field",
        "series_id_candidate": None,
        "landing_page": None,
        "release_schedule": "DERIVED_MAX_OF_INPUTS",
        "temporally_safe": True,
        "verified_in_this_session": False,
        "derived": True,
        "derived_from": ("EIA_INVENTORY", "EIA_PRODUCTION", "EIA_DEMAND"),
    },
}


def verify_registry_before_first_production_use():
    """Deliberate hard stop. This session could not reach eia.gov/
    api.eia.gov to confirm EIA_SERIES_REGISTRY's series_id_candidate
    values or release-schedule claims against EIA's own live API/docs
    (see module docstring). This function must be replaced with a real
    verification (a live API call against each series_id_candidate,
    confirming it resolves and returns the expected units/frequency)
    before this adapter is ever pointed at production -- it always
    raises until that has actually happened, so this limitation cannot
    be silently forgotten or skipped."""
    raise NotImplementedError(
        "EIA_SERIES_REGISTRY has not been verified against a live EIA API "
        "call. Perform that verification (see this function's own "
        "docstring) and only then replace this function's body -- never "
        "delete this guard without having actually done so."
    )


# ---------------------------------------------------------------------
# Fetch (read-only, parameterized by explicit date range -- never the
# full catalogue) and parse
# ---------------------------------------------------------------------

def fetch_eia_series(route, params, api_key, timeout=30):
    """Read-only GET against the EIA API v2. `route` and `params` are
    supplied by the caller (never guessed here) -- see
    verify_registry_before_first_production_use(). The API key is read
    once, used only as a query parameter (EIA's own required auth
    mechanism, no header-based alternative documented), and never
    logged, printed, or included in any exception message this function
    raises."""
    query = dict(params)
    query["api_key"] = api_key
    qs = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in query.items())
    url = f"https://api.eia.gov/v2/{route}?{qs}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as e:
        redacted = str(e).replace(api_key, "***")
        raise RuntimeError(f"EIA API request failed: HTTP {e.code} {redacted}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"EIA API request failed: {e.reason}") from e
    return parse_eia_response(body)


def parse_eia_response(raw_body):
    """Parses the EIA API v2 response envelope: {"response": {"data": [
    {"period": ..., "value": ..., ...}, ...]}}. Returns the data array,
    or raises if the envelope shape doesn't match -- never silently
    returns an empty/partial result for a malformed response."""
    parsed = json.loads(raw_body)
    response = parsed.get("response")
    if not isinstance(response, dict) or "data" not in response:
        raise RuntimeError(f"EIA API response missing expected response.data shape: {parsed}")
    data = response["data"]
    if not isinstance(data, list):
        raise RuntimeError(f"EIA API response.data is not a list: {data!r}")
    return data


def parse_weekly_period(period_str):
    """EIA weekly series report `period` as the week-ENDING date (a
    Friday), ISO format 'YYYY-MM-DD'. Returns a `date`. Raises on any
    other shape -- monthly ('YYYY-MM') periods are never accepted here
    since no monthly category is TEMPORALLY_SAFE in Phase 1."""
    parsed = date.fromisoformat(period_str)
    if parsed.weekday() != 4:
        raise ValueError(f"Expected a weekly EIA period ending on a Friday, got {period_str} (weekday {parsed.weekday()})")
    return parsed


def classify_direction(current_value, previous_value):
    """UP/DOWN/FLAT from an exact comparison against the immediately
    preceding observation of the SAME series -- never a magnitude
    threshold (no invented significance level). None if no previous
    observation exists yet (never fabricated as FLAT)."""
    if previous_value is None:
        return None
    if current_value > previous_value:
        return "UP"
    if current_value < previous_value:
        return "DOWN"
    return "FLAT"


def normalize_observation(category, period_str, raw_value, collection_ts, previous_value=None):
    """The one function that turns a raw EIA data point into a ledger-
    ready observation. Refuses (raises ValueError) for any category not
    marked TEMPORALLY_SAFE in EIA_SERIES_REGISTRY -- there is no
    best-guess/partial path. Never invents a publication timestamp: for
    the weekly-petroleum categories it is always computed via
    compute_wpsr_release_ts(), which itself refuses for an unreviewed
    year rather than approximate.

    Returns a dict with exactly: source_key, provider, category,
    observation_period, information_available_at, observation_time,
    direction, raw_value, reference_value, evidence_reference,
    collection_ts."""
    if category not in EIA_SERIES_REGISTRY:
        raise ValueError(f"Unknown EIA category: {category}")
    entry = EIA_SERIES_REGISTRY[category]
    if not entry["temporally_safe"]:
        raise ValueError(
            f"{category} is not TEMPORALLY_SAFE (reason: {entry.get('unsafe_reason')}) -- "
            f"refusing to normalize it as usable research evidence."
        )
    if entry.get("derived"):
        raise ValueError(f"{category} is derived -- use normalize_derived_energy_shock() instead.")

    period_end_date = parse_weekly_period(period_str)
    information_available_at = compute_wpsr_release_ts(period_end_date)
    observation_time = int(datetime(period_end_date.year, period_end_date.month, period_end_date.day, tzinfo=timezone.utc).timestamp() * 1000)

    if information_available_at <= observation_time:
        # Structural guard, not expected to ever fire given the model
        # above (publication always trails the period it describes) --
        # fails loudly rather than silently accepting a look-ahead.
        raise RuntimeError(
            f"{category}: computed information_available_at ({information_available_at}) "
            f"is not after observation_time ({observation_time}) -- refusing a possible "
            f"look-ahead leak."
        )

    return {
        "source_key": category.lower(),
        "provider": PROVIDER,
        "category": category,
        "observation_period": period_str,
        "information_available_at": information_available_at,
        "observation_time": observation_time,
        "direction": classify_direction(float(raw_value), previous_value),
        "raw_value": float(raw_value),
        "reference_value": json.dumps({
            "series_reference": entry["series_reference"],
            "series_id_candidate": entry["series_id_candidate"],
            "verified_in_this_session": entry["verified_in_this_session"],
        }),
        "evidence_reference": entry["landing_page"],
        "collection_ts": collection_ts,
    }


def normalize_derived_energy_shock(period_str, inputs, collection_ts):
    """`inputs` is a dict of already-normalized observations keyed by
    the 3 safe categories EIA_ENERGY_SHOCK derives from (EIA_INVENTORY/
    EIA_PRODUCTION/EIA_DEMAND for the SAME period_str). Raises if any
    required input is missing -- a shock observation is never computed
    from a partial input set. information_available_at is the MAX of
    the inputs' own (the shock cannot be knowable before every input
    that defines it is itself knowable)."""
    entry = EIA_SERIES_REGISTRY["EIA_ENERGY_SHOCK"]
    required = entry["derived_from"]
    missing = [k for k in required if k not in inputs]
    if missing:
        raise ValueError(f"EIA_ENERGY_SHOCK missing required inputs: {missing}")
    for key, obs in inputs.items():
        if obs["observation_period"] != period_str:
            raise ValueError(f"EIA_ENERGY_SHOCK input {key} observation_period {obs['observation_period']} != {period_str}")

    information_available_at = max(obs["information_available_at"] for obs in inputs.values())
    observation_time = max(obs["observation_time"] for obs in inputs.values())

    # A deterministic, threshold-free composite: flags a shock only when
    # EVERY input direction agrees (all UP or all DOWN) -- never a
    # magnitude-based significance test, matching this project's own
    # discipline of not inventing new statistical thresholds in a
    # descriptive/derived signal.
    directions = {obs["direction"] for obs in inputs.values()}
    if directions == {"UP"}:
        shock_direction = "UP"
    elif directions == {"DOWN"}:
        shock_direction = "DOWN"
    else:
        shock_direction = None

    return {
        "source_key": "eia_energy_shock",
        "provider": PROVIDER,
        "category": "EIA_ENERGY_SHOCK",
        "observation_period": period_str,
        "information_available_at": information_available_at,
        "observation_time": observation_time,
        "direction": shock_direction,
        "raw_value": None,
        "reference_value": json.dumps({
            "derived": True,
            "derived_from": list(required),
            "per_input_direction": {k: inputs[k]["direction"] for k in required},
        }),
        "evidence_reference": None,
        "collection_ts": collection_ts,
    }
