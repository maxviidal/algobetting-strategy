# Algobetting Strategy

Reproducible Python research for identifying and backtesting potential value in
professional tennis and NBA match-winner markets.

The repository contains three connected layers:

- `betting_core`: shared two-outcome pricing and probability calibration;
- `tennis_value`: tennis collection, normalization, calibration, signals, and
  backtests; and
- `basketball_value`: NBA historical collection, calibration, and reporting.

This software produces research signals. It does not place bets, promise
profits, or treat bookmaker prices as ground truth.

## Contents

1. [Current research workflows](#current-research-workflows)
2. [Repository structure](#repository-structure)
3. [Installation](#installation)
4. [Secrets and environment variables](#secrets-and-environment-variables)
5. [Tennis 2026 ATP/WTA 1000 calibration](#tennis-2026-atpwta-1000-calibration)
6. [Current tennis collection and evaluation](#current-tennis-collection-and-evaluation)
7. [ATP Wimbledon 2026 backtest](#atp-wimbledon-2026-backtest)
8. [NBA moneyline research](#nba-moneyline-research)
9. [Methodology](#methodology)
10. [Generated files and Git](#generated-files-and-git)
11. [Testing and development](#testing-and-development)
12. [Command reference](#command-reference)

## Current research workflows

| Workflow | Provider | Entry | Calibration | Candidate EV | Status |
|---|---|---:|---|---:|---|
| Tennis ATP/WTA 1000, 2026 | OddsPapi + local TennisData.App results | T-60 | Separate ATP/WTA logistic calibration | `EV_safe >= 4%` | Main tennis study |
| Tennis current odds | The Odds API | Collection time or explicit timestamp | None | Raw consensus EV `>= 4%` | Local research scanner |
| ATP Wimbledon 2026 | OddsPapi settlements and history | T-60 | None | Raw consensus EV `>= 4%` | Legacy single-event backtest |
| NBA 2023-24 pilot | BALLDONTLIE + The Odds API | T-60 | Logistic calibration | `EV_safe >= 5%` | Calibrated NBA pilot |
| NBA five-season research | BALLDONTLIE + The Odds API | T-60 | Disabled in locked profile | Raw consensus EV `>= 5%` | Larger, potentially paid study |

Tennis and NBA settings are intentionally separate. Do not use an NBA config
for tennis or assume that evidence from one sport validates the other.

## Repository structure

```text
.
├── configs/
│   ├── development.toml
│   ├── research.toml
│   ├── tennis_calibration_2026.toml
│   ├── basketball_pilot_2023_24.toml
│   └── basketball_research.toml
├── src/
│   ├── betting_core/
│   │   ├── two_way.py
│   │   └── calibration.py
│   ├── tennis_value/
│   │   ├── data/
│   │   ├── calibration_reporting.py
│   │   ├── oddspapi_research.py
│   │   ├── results_csv.py
│   │   ├── signals.py
│   │   └── cli.py
│   └── basketball_value/
├── tests/
├── data/          # generated provider data; ignored by Git
├── artifacts/     # generated fitted models; ignored by Git
└── reports/       # generated analysis exports; ignored by Git
```

## Installation

The project targets Python 3.12.

From the repository root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

On each later terminal session, return to the repository and reactivate the
environment:

```bash
cd /Users/maxvidal/Documents/algobetting-strategy
source .venv/bin/activate
```

Confirm that the command-line applications are available:

```bash
python -m tennis_value --help
python -m basketball_value --help
```

## Secrets and environment variables

Copy the example file once:

```bash
cp .env.example .env
```

Then edit `.env` locally. Never commit it.

```text
# The Odds API: current tennis and NBA historical odds
ODDS_API_KEY=your-key

# BALLDONTLIE: NBA schedules and results
BALLDONTLIE_API_KEY=your-key

# OddsPapi: tennis historical odds
ODDS_PAPI_KEY=your-key
ODDS_PAPI_BASE_URL=https://api.oddspapi.io/v4
```

The applications read keys at request time. API keys are not written to cached
payloads, manifests, reports, or terminal summaries.

## Tennis 2026 ATP/WTA 1000 calibration

This is the main tennis research workflow.

### Scope

The locked profile is `configs/tennis_calibration_2026.toml`.

ATP events:

- Indian Wells;
- Miami;
- Monte-Carlo;
- Madrid; and
- Rome.

WTA events:

- Doha;
- Dubai;
- Indian Wells;
- Miami;
- Madrid; and
- Rome.

Canada is not included in this fixed completed-event dataset.

An additional profile, `configs/tennis_main_tour_2026.toml`, expands the same
T-60 methodology to 81 completed individual main-tour singles events through
10 August 2026: 43 ATP and 38 WTA tournaments across hard, clay, and grass. It
includes ATP/WTA 250 events alongside 500s, 1000s, and the three completed
Grand Slams. Qualifying draws, team competitions, Challengers, and WTA 125
events remain out of scope. The original 1000-level profile is unchanged.

The extended profile deliberately retains the same 15 bookmakers in the same
order. That keeps the Pinnacle reference and offer universe comparable and
prevents positional historical-cache groups from being reinterpreted. A later
bookmaker-replacement experiment should use a new cache and separate profile.

The study uses:

- a fixed T-60 entry decision;
- a T-5 closing diagnostic;
- Pinnacle as the power-de-vigged reference probability;
- separate ATP and WTA calibration fits;
- whole-date chronological 60% training, 20% validation, and 20% test phases;
- 200 deterministic seven-day block-bootstrap refits;
- the fifth percentile as `p_safe`;
- a minimum of 200 training matches per tour;
- L2 regularization of `10` toward the identity calibration mapping;
- `EV_safe >= 0.04`; and
- quarter-Kelly staking based on `p_safe`.

Training uses one first-listed-player observation per eligible match. The
opponent probability is always its complement. All eligible matches train the
calibrator; threshold-selected bets are never used as the training sample.

### Bookmaker profile

The collector requests 15 bookmakers: Pinnacle plus 14 offered-price sources.
The order is locked into five provider-supported groups of three:

| Group | Bookmakers |
|---:|---|
| 0 | `pinnacle`, `bet365`, `betano` |
| 1 | `bwin`, `unibet`, `betway` |
| 2 | `coral`, `ladbrokes`, `leovegas` |
| 3 | `williamhill`, `paddypower`, `betfred` |
| 4 | `888sport`, `betsson`, `skybet` |

The request values are OddsPapi bookmaker slugs; consult the provider's
[sportsbook directory](https://oddspapi.io/sportsbooks/) when reviewing future
changes to this locked list.

The first two groups are the original six-book profile. Their existing cache
remains reusable; expanding to 15 books adds groups 2-4 without repeating
completed group 0-1 requests.

Collection does not guarantee that every bookmaker has a valid tennis price
for every fixture. HTTP 404/no-data responses are cached exactly and become
observable coverage exclusions. A match still requires Pinnacle plus at least
four other valid active books.

More bookmaker brands increase offered-price and execution coverage. They do
not increase the number of independent match outcomes used to fit calibration.
Several brands may also share ownership or pricing infrastructure, so 15 brand
names must not be interpreted as 15 independent information sources.

### OddsPapi request cost

OddsPapi accepts at most three bookmakers per historical request. Historical
requests are free and consume zero monthly quota, but they have a five-second
cooldown. The implementation waits 5.1 seconds.

Provider references: [historical-odds endpoint](https://oddspapi.io/us/docs/get-historical-odds)
and [request/quota rules](https://oddspapi.io/us/docs/requests-and-quota).

For `F` fixtures and `B` bookmakers:

```text
historical_requests = F * ceil(B / 3)
minimum_paced_time   = (historical_requests - 1) * 5.1 seconds
```

The current fixed manifest contains 971 fixtures:

| Book profile | Groups per fixture | Historical calls | Monetary cost | Monthly quota | Minimum paced time |
|---|---:|---:|---:|---:|---:|
| Original 6 | 2 | 1,942 | 0 | 0 | about 2 h 45 m |
| 11 + Pinnacle = 12 | 4 | 3,884 | 0 | 0 | about 5 h 30 m |
| 14 + Pinnacle = 15 | 5 | 4,855 | 0 | 0 | about 6 h 53 m |

Starting from an empty cache also requires one billable tournament-catalog call
and eleven billable fixture calls: 12 monthly requests in total. Once those are
cached, expanding from six to fifteen bookmakers adds no billable requests. It
adds 2,913 free historical calls, requiring about 4 hours 8 minutes after the
original six-book pull has completed.

The `/account` preflight is unmetered. The collector preserves a five-request
quota buffer because exhausting the monthly allowance also blocks free
historical endpoints.

### Step 1: run the preflight

```bash
python -m tennis_value preflight-tennis-calibration
```

The preflight:

- reads and sanitizes `/account` without persisting the response;
- validates every existing compressed cache file;
- counts missing catalog and fixture calls exactly;
- counts missing free historical groups;
- preserves the five-request quota buffer; and
- prints either `READY_TO_DOWNLOAD` or `BLOCKED_BY_QUOTA`.

Example of the important lines:

```text
Status: READY_TO_DOWNLOAD
Missing billable requests: 0
Missing free historical requests: ...
Exact confirmation value: 0
```

Always use the current printed confirmation value. Do not reuse an older value
after the cache changes.

### Step 2: fetch or resume history

If preflight prints an exact confirmation value of `0`:

```bash
python -m tennis_value fetch-tennis-calibration-history \
  --confirm-billable-requests 0
```

If it prints another number, substitute that exact number:

```bash
python -m tennis_value fetch-tennis-calibration-history \
  --confirm-billable-requests EXACT_VALUE_FROM_PREFLIGHT
```

Collection is intentionally slow. It enforces:

- at least 5.1 seconds between historical calls;
- at least 2.1 seconds between fixture calls;
- bounded HTTP 429 retries using `Retry-After`;
- lossless gzip JSON storage;
- checksums and atomic writes;
- exact fixture-level 404 records that resume as no-data tournaments;
- exact cached no-data responses; and
- resume without repeating completed files.

Do not start two collectors against the same cache concurrently. If the command
is interrupted, rerun preflight and then rerun the fetch command. Do not delete
the partial cache.

Raw files are stored under:

```text
data/oddspapi/tennis_1000_2026/
├── tournaments.json.gz
├── fixtures/
├── historical/
└── collection_manifest.json
```

To count cached historical groups without opening the payloads:

```bash
find data/oddspapi/tennis_1000_2026/historical \
  -type f -name '*.json.gz' | wc -l
```

For the 15-book profile, a complete 971-fixture cache contains 4,855 historical
group files. Cached 404/no-data responses still have files, so do not treat a
lower count as complete unless the collection manifest explicitly says so.

### Step 3: download results manually

OddsPapi supplies prices, not the local result labels used by this study.
Download the current-season files manually from
[TennisData.App](https://tennisdata.app/downloads/):

```text
2026-atp-season.csv
2026-wta-season.csv
```

Keep the files outside Git, for example:

```text
data/results/tennisdata/2026-atp-season.csv
data/results/tennisdata/2026-wta-season.csv
```

The importer records each file's SHA-256 checksum. It matches by tour,
tournament, date window, and unordered normalized player pair. Missing,
ambiguous, incomplete, retired, cancelled, or walkover outcomes are quarantined
instead of guessed.

For TennisData.App season exports, the importer reads `date_timestamp` as Unix
seconds in UTC. It also accepts the `date_human` format such as `02 Jan 2026`,
along with the existing generic date-column formats.

### Step 4: fit models and export the real report

This step is offline and makes no API requests:

```bash
python -m tennis_value export-tennis-calibration-report \
  --atp-results-csv data/results/tennisdata/2026-atp-season.csv \
  --wta-results-csv data/results/tennisdata/2026-wta-season.csv
```

The defaults are:

```text
Raw cache:  data/oddspapi/tennis_1000_2026/
Models:     artifacts/calibration/tennis/2026_1000/
Reports:    reports/tennis_calibration_2026/
```

Use explicit alternatives when needed:

```bash
python -m tennis_value export-tennis-calibration-report \
  --research-config configs/tennis_calibration_2026.toml \
  --model-config configs/research.toml \
  --cache-directory data/oddspapi/tennis_1000_2026 \
  --atp-results-csv /absolute/path/to/2026-atp-season.csv \
  --wta-results-csv /absolute/path/to/2026-wta-season.csv \
  --artifact-directory artifacts/calibration/tennis/2026_1000 \
  --output-directory reports/tennis_calibration_2026
```

### Step 5: inspect the outputs

| Output | Purpose |
|---|---|
| `matches_predictions.csv` | One row per fixture with phase, result, raw/calibrated probabilities, coverage, closing diagnostic, and quote age |
| `every_offer.csv` | Every bookmaker/player offer with raw EV, calibrated EV, safe EV, `p_safe`, quote age, CLV, and selection status |
| `selected_candidates.csv` | Only the selected validation/test candidates with Kelly stake and settlement |
| `calibration_bins.csv` | Predicted probability versus observed win rate by tour, phase, and probability type |
| `equity_curve.csv` | Ordered Kelly bankroll path, profit, and drawdown |
| `exclusions.csv` | Result-matching and market-coverage failures with explicit reasons |
| `identity_review.csv` | Repeated surname-plus-initial keys from OddsPapi and TennisData, retaining original names, opponents, games, dates, and tournaments for manual review |
| `summary.csv` | Compact top-level metrics for spreadsheet review |
| `summary.json` | Full metrics, phase/tour/bookmaker/surface breakdowns, assumptions, config, and checksums |
| `artifacts/.../atp.json` | ATP coefficients, bootstrap models, cutoff, config, match hash, and source checksums |
| `artifacts/.../wta.json` | WTA coefficients, bootstrap models, cutoff, config, match hash, and source checksums |

Start with `summary.json`, then inspect `exclusions.csv`,
`identity_review.csv`,
`matches_predictions.csv`, and `every_offer.csv` before interpreting ROI.

Result matching preserves both sources' original names. It uses surname plus
first initial as the broad candidate key, then honors a two-letter TennisData
abbreviation only when it is a real prefix of the OddsPapi given name (for
example, `Pliskova Ka.` to `Pliskova, Karolina` and `Pliskova Kr.` to
`Pliskova, Kristyna`). Repeated or unresolved identities remain visible in
`identity_review.csv` or are quarantined in `exclusions.csv` rather than guessed.

If either tour has fewer than 200 training matches or result matching is below
95%, that tour is marked `inconclusive`. The overall conclusion is always
`exploratory_only` because all fitting and evaluation data come from one
partial season.

## Current tennis collection and evaluation

This separate workflow uses The Odds API to store and evaluate current tennis
prices. It does not use the 2026 OddsPapi calibration artifact automatically.

### 1. List active tennis competitions

```bash
python -m tennis_value sports
```

### 2. Collect one competition

Replace the example key with a value printed by `sports`:

```bash
python -m tennis_value collect \
  --sport tennis_atp_example \
  --regions uk,eu
```

The default SQLite database is:

```text
data/tennis_value.sqlite3
```

### 3. Review unknown player identities

```bash
python -m tennis_value players pending
```

Approve exact names individually:

```bash
python -m tennis_value players approve \
  --name "Player One" \
  --name "Player Two"
```

Or explicitly approve every displayed pending name:

```bash
python -m tennis_value players approve --all
```

### 4. Normalize the stored response

```bash
python -m tennis_value normalize
```

### 5. Evaluate point-in-time prices

```bash
python -m tennis_value evaluate \
  --config configs/research.toml
```

To replay at an explicit UTC timestamp:

```bash
python -m tennis_value evaluate \
  --config configs/research.toml \
  --decision-at 2026-08-09T12:00:00Z
```

Use `--database` and `--response-id` on the relevant commands to select another
database or raw response.

Tennis accepts arbitrarily old pre-decision quotes. It always excludes future
quotes and rejects a bookmaker when its latest pre-decision state is inactive.
Quote age is diagnostic only; there is no freshness guarantee.

## ATP Wimbledon 2026 backtest

This is a legacy single-event OddsPapi workflow using tournament ID `2555` and
nine bookmakers. It remains available for comparison with earlier research.

Run or resume:

```bash
python -m tennis_value backtest-atp-wimbledon \
  --config configs/research.toml \
  --cache-directory data/oddspapi/wimbledon_atp_2026
```

Export its existing cache without API requests:

```bash
python -m tennis_value export-atp-wimbledon-csv \
  --config configs/research.toml \
  --cache-directory data/oddspapi/wimbledon_atp_2026 \
  --output-directory reports/wimbledon_atp_2026
```

The export creates:

```text
reports/wimbledon_atp_2026/wimbledon_atp_2026_matches.csv
reports/wimbledon_atp_2026/wimbledon_atp_2026_offers.csv
```

This workflow uses provider settlement data and uncalibrated quarter-Kelly. It
must not be presented as evidence that the calibrated ATP/WTA 1000 model works.

## NBA moneyline research

NBA collection can consume paid The Odds API historical credits. Do not run a
confirmed download until the current preflight and manifest have been reviewed.

### Choose a profile

Lower-cost calibrated pilot:

```text
configs/basketball_pilot_2023_24.toml
```

Larger five-season uncalibrated research profile:

```text
configs/basketball_research.toml
```

The examples below use the 2023-24 pilot.

### 1. Cache schedules/results and prepare the quota manifest

This first pass makes no paid odds requests because it omits
`--confirm-quota-cost`:

```bash
python -m basketball_value fetch-nba-history \
  --config configs/basketball_pilot_2023_24.toml \
  --cache-directory data/nba_moneyline
```

### 2. Run the no-spend preflight

```bash
python -m basketball_value preflight-nba-moneyline \
  --config configs/basketball_pilot_2023_24.toml \
  --cache-directory data/nba_moneyline \
  --output-directory reports/nba_2023_24
```

Interpretation:

- `NOT_READY`: inputs or configuration are incomplete;
- `READY_TO_PURCHASE`: the manifest is valid but current credits are
  insufficient; and
- `READY_TO_DOWNLOAD`: the exact current download fits available credits.

`READY_TO_PURCHASE` is not permission to download.

### 3. Review and explicitly confirm the exact cost

Inspect:

```text
data/nba_moneyline/quota_manifest.json
```

Then use the manifest's current exact `expected_credits` value:

```bash
python -m basketball_value fetch-nba-history \
  --config configs/basketball_pilot_2023_24.toml \
  --cache-directory data/nba_moneyline \
  --confirm-quota-cost EXACT_ADDITIONAL_CREDITS
```

If the exact value no longer matches, the command stops. Rerun preflight rather
than guessing.

### 4. Run the offline backtest

```bash
python -m basketball_value backtest-nba-moneyline \
  --config configs/basketball_pilot_2023_24.toml \
  --cache-directory data/nba_moneyline \
  --output reports/nba_2023_24/backtest.json
```

### 5. Export the complete NBA report

```bash
python -m basketball_value export-nba-report \
  --config configs/basketball_pilot_2023_24.toml \
  --cache-directory data/nba_moneyline \
  --output-directory reports/nba_2023_24
```

The calibrated 2023-24 pilot is always labelled `exploratory_only`. Its
within-season test is chronological but is not a new-season holdout.

## Methodology

### Point-in-time state reconstruction

For a decision timestamp `t`, the system:

1. discards records observed after `t`;
2. selects the latest record at or before `t` for each outcome/bookmaker;
3. checks whether that selected state is active and complete; and
4. rejects the bookmaker if its latest state is inactive.

An older active price cannot be resurrected after a newer inactive record.

Tennis has no maximum quote-age rejection. NBA profiles retain their separate
30-minute quote-age rule.

### Margin removal

Decimal odds are converted to implied probabilities:

```text
implied_probability = 1 / decimal_odds
```

The power method finds one exponent `k` such that the two powered implied
probabilities sum to one.

### Tennis reference and expected value

Pinnacle's two prices are power-de-vigged. The selected side's reference
probability is `p_raw`:

```text
EV_raw = offered_odds * p_raw - 1
```

Pinnacle is not evaluated as an offered candidate against itself.

### Logistic calibration

The shared binary calibrator uses:

```text
logit(p_calibrated) = intercept + slope * logit(p_raw)
```

The L2 penalty is centred on the identity mapping:

```text
intercept = 0
slope = 1
```

ATP and WTA are fitted independently.

### Conservative probability and candidate selection

Seven-day chronological blocks are resampled deterministically 200 times. For
each side, the fifth percentile of bootstrap predictions is the conservative
probability `p_safe`.

```text
EV_calibrated = offered_odds * p_calibrated - 1
EV_safe       = offered_odds * p_safe - 1
```

Tennis candidates require:

```text
EV_safe >= 0.04
```

Raw, point-calibrated, and conservative probabilities and EVs remain separate
in the exports.

### Kelly staking

For decimal odds `O` and staking probability `p_safe`:

```text
full_kelly_fraction = (O * p_safe - 1) / (O - 1)
research_stake       = available_equity * 0.25 * full_kelly_fraction
```

The fraction is floored at zero. Only one candidate is selected per match.
Capital is reserved when the signal occurs and is not reused by overlapping
open bets.

### Metrics

The reports expose, where applicable:

- raw and calibrated Brier score;
- raw and calibrated log loss;
- calibration bins;
- bookmaker, surface, tour, validation, and test breakdowns;
- result and entry coverage;
- hit rate, turnover, profit, ROI, and yield;
- drawdown and equity curve;
- closing-line value diagnostics;
- quote-age diagnostics; and
- exclusions and quarantines.

## Generated files and Git

The following root directories are intentionally ignored:

```text
/data/
/artifacts/
/reports/
```

They may contain large provider payloads, local result files, fitted models, and
analysis outputs. They must not be committed.

The source package `src/tennis_value/data/` is not ignored. It contains Python
code and must remain trackable.

Also ignored:

- `.env`;
- local SQLite databases;
- virtual environments;
- Python caches; and
- test, type-check, and lint caches.

## Testing and development

Run the complete automated suite:

```bash
pytest -q
```

Run formatting/lint checks:

```bash
ruff check .
```

Run strict type checking:

```bash
mypy src tests
```

Run all acceptance checks together:

```bash
pytest -q && ruff check . && mypy src tests
```

The test suite includes a complete synthetic ATP/WTA workflow, deterministic
bootstrap regression, ATP/WTA isolation, quota confirmation, pacing, retries,
compressed-cache integrity, resume behaviour, conservative result matching,
safe-EV/Kelly selection, and point-in-time quote-state regressions.

## Command reference

### Tennis

```text
python -m tennis_value sports
python -m tennis_value collect
python -m tennis_value players pending
python -m tennis_value players approve
python -m tennis_value normalize
python -m tennis_value evaluate
python -m tennis_value preflight-tennis-calibration
python -m tennis_value fetch-tennis-calibration-history
python -m tennis_value export-tennis-calibration-report
python -m tennis_value backtest-atp-wimbledon
python -m tennis_value export-atp-wimbledon-csv
```

Show command-specific options:

```bash
python -m tennis_value COMMAND --help
```

Global tennis options such as `--env-file` and `--timeout` go before the
subcommand:

```bash
python -m tennis_value --env-file .env --timeout 60 \
  preflight-tennis-calibration
```

### NBA

```text
python -m basketball_value preflight-nba-moneyline
python -m basketball_value fetch-nba-history
python -m basketball_value backtest-nba-moneyline
python -m basketball_value export-nba-report
```

Show command-specific options:

```bash
python -m basketball_value COMMAND --help
```
