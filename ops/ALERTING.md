# Operations alerting

One workflow, `.github/workflows/prediction-staleness-alert.yml`, runs three read-only checks every three hours
and reports what it finds once.

## What is checked

| Check | Source | Finding |
|---|---|---|
| `predictions` | `GET /prediction-freshness` | a series is not FRESH, fewer than 6 of the 8 scheduled runs in 24 h, price data older than 4 h |
| `workflows` | GitHub run history | a scheduled workflow has not succeeded recently, or keeps failing (`ops/workflow_health.py`) |
| `usage` | `GET /d1-usage` | D1 rows read today, against the daily limit the route reports (5,000,000) |

A check that cannot read its source is a finding, never a pass.

### Usage levels (`ops/d1_usage_alert.py`)

| Level | Condition | Alerts |
|---|---|---|
| healthy | none of the below | no |
| warning | more than 60% of the limit used, or a reliable projection for the day above 85% | yes |
| critical | the rows read today have reached the limit (D1 stops serving reads until 00:00 UTC) | yes |
| stale | no measurement in the last 2 hours, or nothing recorded today after 02:00 UTC | yes |
| unknown | the payload cannot be interpreted | yes |

Why these thresholds: the eight prediction runs alone read about 1.4M rows a day (28% of the limit), so 60% used
means dashboard and research reads have already added more than the scheduled work itself; with a check every
three hours it leaves at least one more check before the limit unless consumption exceeds 2M rows in three hours.
85% projected leaves a 15% margin for the part of the day not yet seen.

**Projection.** Every hourly scheduled run is recorded, so the number of hourly runs recorded today is the number
of hours the total covers. Fully measured day: `total × 24 / hours elapsed`, **reliable** from 3 h into the day.
Partial-day recording (first deployment, recording switched off for a while): `measured hourly rate × 24`,
**provisional**; it is printed and shown on the dashboard but does not raise the alert, because the hours that
were not recorded are unknown. The share actually used always alerts.

The totals are a lower bound: requests reading fewer than 50 rows are not recorded.

## How an alert reaches you

State lives in **one GitHub issue labelled `ops-alert`** in this repository.

| Situation | What happens | Notification |
|---|---|---|
| New problem | the issue is opened; the run is red | issue opened + failed run |
| Same problems on a later run | the issue body's "Last seen" is updated; the run is green | none |
| The set of problems changes | a comment is added; the run is red | comment + failed run |
| Checks pass again | a "Recovered" comment is added and the issue is closed; the run is green | comment / close |
| GitHub's issue API cannot be reached | the run is red | failed run |

"Same problems" ignores figures: 62% used followed by 64% used is one alert; warning followed by critical, or a
second series going stale, is a change.

So: **an open `ops-alert` issue means an alert is active; a red run means an alert is new or changed.**

### Notification configuration

Nothing is sent outside GitHub, and no secret is involved: the workflow uses the run's own `GITHUB_TOKEN`
(`issues: write`). Delivery depends on the GitHub notification settings of the people who should be told:

1. **Watch this repository** with "All activity", or "Custom" including **Issues**
   (repository page, Watch). Owners watch their own repositories by default.
2. In personal settings, Notifications, enable **email** (and/or the mobile app) for "Watching".
3. For failed-run emails, enable "Actions: failed workflows only" under Notifications, System. GitHub sends
   those to whoever started the run: for runs the Worker starts, the owner of `RESEARCH_DISPATCH_TOKEN`.

There is no Slack, PagerDuty or SMS destination. Adding one means a new secret and a step in the workflow;
it is not configured.

## Triggers

- The production Worker's hourly cron starts the workflow at hours 1, 4, 7, … UTC (one hour after each
  prediction run) with `RESEARCH_DISPATCH_TOKEN`.
- The workflow's own schedule (`25 */3 * * *`) is the fallback, and the only trigger left if the Worker's cron
  itself stops. GitHub fires it irregularly.

## Testing the whole path

Run the workflow manually with the `simulate` input. It feeds a fixed scenario through the real check and
de-duplication code and uses the label **`ops-alert-test`**, so the real alert is never touched:

`healthy`, `usage_warning`, `usage_critical`, `usage_stale`, `predictions_stale`, `fetch_failure`.

Sequence used to verify a release: `usage_warning` (red, issue opened) → `usage_warning` (green, no new
notification) → `usage_critical` (red, comment) → `healthy` (green, issue closed).

## What to do when it fires

- **usage warning / critical**: open the dashboard's System status, or `GET /d1-usage`, and look at the largest
  entries. Reads stop at the limit and resume at 00:00 UTC. `ROUTE_CACHE`, `FRESHNESS_CACHE` and the tick read
  memo are the existing controls; do not disable them.
- **predictions**: `GET /prediction-freshness` shows which series stopped and the last commit that wrote it.
- **workflows**: the message names the workflow; open its run history.
