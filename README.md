# Algobetting Strategy

Research software for identifying and backtesting potential value in
professional sports betting markets. Tennis and basketball are separate
applications backed by a small shared two-outcome pricing core.

The project will collect point-in-time bookmaker odds, normalize the underlying
events, remove bookmaker margins, construct a robust market consensus, and flag
offers whose expected value exceeds configurable thresholds.

The repository is intentionally small while the first working features are
built. It does not place bets and does not claim guaranteed profitability. See
[AGENTS.md](AGENTS.md) for the planned methodology and engineering standards.

## Structure

```text
configs/       Research settings
src/
  betting_core/ Sport-neutral two-outcome pricing and point-in-time selection
  basketball_value/ NBA data, strategy, backtest, and reporting
  tennis_value/ Tennis-specific application code
    data/       Ingestion, normalization, domain records, and persistence
tests/         Automated tests
```

New modules and directories should be added only when working code needs them.

## NBA moneyline consensus pilot

`basketball_value` implements a research-only NBA regular-season moneyline
backtest for 2021–22 through 2025–26. It does not predict games from team
statistics and does not place bets. The locked model uses EU decimal `h2h`
prices, power-method de-vigging, a leave-one-bookmaker-out median consensus,
and a 5% expected-value threshold. Entry is measured 60 minutes before
scheduled tip and closing value five minutes before tip. At least five
complete fixed-odds bookmakers are required, and quotes older than 30 minutes
are excluded.

Copy `.env.example` to the ignored `.env` file and supply:

```text
BALLDONTLIE_API_KEY=your-key
ODDS_API_KEY=your-key
```

The first collection pass retrieves the free BALLDONTLIE schedules/results,
deduplicates all 60-minute and five-minute timestamps, and writes an exact
quota manifest. It makes no paid historical-odds requests:

```bash
basketball-value fetch-nba-history
```

For the lower-cost development pilot, use the separate 2023–24 profile. It
leaves the five-season research design unchanged and has a maximum initial cost
of 14,840 credits:

```bash
python -m basketball_value preflight-nba-moneyline \
  --config configs/basketball_pilot_2023_24.toml \
  --output-directory reports/nba_2023_24
```

Preflight uses the quota-free sports endpoint to read account quota headers.
It validates the season, cached schedule, exact remaining request count, API
key presence, and writable cache/report locations. It makes no historical-odds
requests. `READY_TO_PURCHASE` means the configuration is sound but the current
plan lacks enough credits; after subscribing, run preflight again and require
`READY_TO_DOWNLOAD` before continuing.

Review `data/nba_moneyline/quota_manifest.json`. To authorize the historical
requests, rerun with the exact `expected_credits` value printed by the first
pass:

```bash
python -m basketball_value fetch-nba-history \
  --config configs/basketball_pilot_2023_24.toml \
  --confirm-quota-cost EXPECTED_ADDITIONAL_CREDITS
```

The value must match exactly or the command stops. Exact provider responses
are written atomically with query time, provider snapshot time, and checksum.
Successful and explicit no-data requests are reused. Rate limits, temporary
provider failures, and network interruptions use bounded retries. Progress and
the provider's reported request cost, used credits, and remaining credits are
retained in the manifest. The run stops before another request if the provider
reports fewer than ten remaining credits.

Once the cache is complete, both remaining commands are offline:

```bash
python -m basketball_value backtest-nba-moneyline \
  --config configs/basketball_pilot_2023_24.toml
python -m basketball_value export-nba-report \
  --config configs/basketball_pilot_2023_24.toml \
  --output-directory reports/nba_2023_24
```

The 2023–24 pilot applies calibration without requesting or depending on any
other season. Whole UTC game dates are divided chronologically into an initial
60% calibration-training phase, a 20% validation phase, and a final untouched
20% test phase. Games in the training phase cannot become betting candidates.
The validation calibrator is fitted only on training outcomes; the final-test
calibrator is then fitted on the earlier training and validation outcomes.

Calibration uses one home-win row per eligible game, drawn from the 60-minute
entry consensus across all valid bookmakers. It fits the locked logistic form
`logit(p_calibrated) = intercept + slope * logit(p_market)` with an L2 penalty
toward the identity mapping (`intercept = 0`, `slope = 1`). It never fits only
on bets that passed the EV threshold, never calibrates both sides independently,
and never uses the five-minute closing snapshot as an entry feature.

For uncertainty, the configured 200 deterministic bootstrap refits resample
seven-day chronological blocks. The fifth percentile for the selected side is
reported as `p_safe`. Candidate selection and quarter-Kelly use
`EV_safe = offered_odds * p_safe - 1`; raw consensus EV and point-calibrated
probability remain separate columns for inspection. Threshold exploration is
disabled for this profile so the final 20% remains untouched.

The export creates game-, offer-, candidate-, equity-curve-, exclusion-, and
summary-level CSV files plus a JSON summary
containing coverage, raw-versus-calibrated Brier score and log loss, closing value,
flat-stake ROI, turnover, hit rate, drawdown, 95% confidence intervals,
fractional-Kelly sensitivity, and the configured breakdowns. Development-only
threshold tables are reported separately for the uncalibrated five-season
profile. The validation and 2025–26 holdout threshold remain fixed at 5%.

Because 2023–24 supplies both the earlier fitting data and the later evaluation
data, the calibrated pilot conclusion is always `exploratory_only`. Its final
20% is a legitimate within-season chronological test, but it cannot establish
that calibration or profitability will survive a new season. The five-season
research profile remains available separately and is not required by the pilot.

The final conclusion is `supported` only when the cache and matching acceptance
checks pass and the untouched 2025–26 holdout contains at least 300 candidates,
positive mean closing value, positive flat-stake ROI, and a positive lower 95%
confidence bound. Otherwise it is reported as `negative` or `inconclusive`;
the command never lowers the threshold after observing results.

## Normalization

The first normalization layer is implemented in
`src/tennis_value/data/normalization.py`.
It converts retained Odds API events into immutable records from
`src/tennis_value/data/domain.py`.

Entity matching is intentionally conservative. Players, tournaments, and
bookmakers must be present in an explicit catalog, and provider-specific names
must either match a canonical display name or be registered as aliases. Unknown
or ambiguous names raise observable errors rather than being guessed. Complete
match-winner markets are normalized to UTC, validated as two distinct player
outcomes with decimal odds greater than `1.0`, and assigned deterministic match
and snapshot identifiers.

Players use positive integer IDs internally, while `display_name` remains the
human-readable name shown to users. Two players may therefore share a surname
without sharing an identity. A surname-only provider value is still rejected
when it cannot be resolved safely.

Player identity resolution is separated from provider normalization.
`InMemoryPlayerResolver` supplies deterministic catalogs for tests, while
`SqlitePlayerRegistry` creates persistent numeric player IDs and stores:

- provider-scoped aliases;
- opaque provider player IDs when a source supplies them; and
- unknown or ambiguous names for later review.

Resolution checks a provider player ID first, then an approved provider alias,
then an exact canonical display name. It never creates a player merely because
an unknown name appeared in an odds response. The resolver is injected into
`OddsApiNormalizer`, keeping database access out of the provider parsing logic.

The Odds API request may include `h2h`, `spreads`, and `totals` together. This
project currently normalizes only the `h2h` match-winner market; the other
market objects remain preserved in the raw response but are not converted into
domain prices. Each bookmaker's `h2h.last_update` becomes the observation time
for its snapshot.

API keys must be read from the `ODDS_API_KEY` environment variable at request
time. Never place a real key in source code, fixtures, logs, or a committed URL.

For the Wimbledon historical backtest, `ODDS_PAPI_KEY` is the separate
OddsPapi credential and `ODDS_PAPI_BASE_URL=https://api.oddspapi.io/v4` is the
provider endpoint. The local `.env` is ignored by Git; neither value belongs in
source code or a committed configuration file.

## Market pricing and value signals

The first pricing model is an explainable market-consensus baseline. For a
specified UTC decision time, `tennis_value.signals.evaluate_market` selects the
latest non-stale snapshot from every bookmaker and requires at least five
eligible bookmakers in total. A quote observed after the decision time is never
used.

Each complete two-player market is converted from decimal odds to implied
probabilities. The power method finds a common exponent `k` such that the
powered implied probabilities sum to one. Every offered outcome is then compared with the median fair probability
from all other eligible bookmakers. The evaluated bookmaker is excluded from
its own benchmark.

Expected value is reported per unit staked:

```text
expected_value = offered_odds * consensus_probability - 1
```

All eligible offers are returned for research, including those below the signal
threshold. Quality flags identify unusually large edges, suspicious market
overround, and wide peer disagreement without deleting the observation. These
are research signals, not guarantees or instructions to place a bet.

The settings in `configs/development.toml` and `configs/research.toml` control
quote age, minimum bookmaker coverage, the candidate threshold, and diagnostic
boundaries. Pricing supports power margin removal, median consensus,
and leave-one-bookmaker-out evaluation only.

## Odds history

`SqliteOddsRepository` retains the exact raw provider bytes, normalized matches,
and every timestamped match-winner snapshot. Stable response, match, and
snapshot identifiers make repeated ingestion idempotent; reusing an identifier
for different immutable data raises an explicit conflict instead of overwriting
history.

The `latest_snapshots_as_of` query returns each bookmaker's most recent quote
that was already known at a UTC decision time and is still within the supplied
freshness window. Future and stale quotes are excluded in SQL. Equivalent
duplicates are collapsed deterministically, while different prices from the
same bookmaker at the same latest timestamp are rejected as conflicting data.
The returned snapshots can be passed directly to `evaluate_market`.

## Command-line collection

The local `.env` file is loaded safely as data rather than executed as a shell
script. To list active tennis tournament keys using The Odds API's quota-free
sports endpoint:

```bash
python -m tennis_value sports
```

To request current UK match-winner odds and preserve the exact response:

```bash
python -m tennis_value collect --sport tennis_atp_example
```

Replace `tennis_atp_example` with an active key from the `sports` command. The
response is stored in `data/tennis_value.sqlite3` by default, and the command
prints the provider's request cost and remaining quota when those headers are
available. API keys are never written to stored provenance or command output.

Current collection deliberately stops after raw preservation. Player names must
be reviewed and registered before normalization so a provider spelling change
cannot silently create or merge player identities.

Review the names found in the latest stored response:

```bash
python -m tennis_value players pending
```

Approve names individually, repeating `--name` when needed:

```bash
python -m tennis_value players approve \
  --name "Player One" \
  --name "Player Two"
```

When every displayed name has been reviewed, `--all` is an explicit shortcut:

```bash
python -m tennis_value players approve --all
```

Normalize the latest raw response into matches and bookmaker snapshots:

```bash
python -m tennis_value normalize
```

Finally, run point-in-time consensus and EV evaluation. The response collection
time is used as the decision time by default, so replaying the command later
does not make the stored quotes stale or introduce future information:

```bash
python -m tennis_value evaluate \
  --config configs/research.toml
```

The evaluation prints all run counts but displays only offers that meet the
configured candidate threshold. Each candidate includes its bookmaker, offered
odds, consensus probability, EV, peer count, and quality flags. All local
commands accept `--database` and `--response-id` to select a different database
or stored collection.

## Backtesting core

`tennis_value.backtesting` provides baseline unit-stake settlement plus a
finite-bankroll Kelly simulation. `KellySettings()` starts at `10000` and uses
`0.25` Kelly by default. For decimal odds `O` and consensus probability `p`,
the full-Kelly fraction is `(O * p - 1) / (O - 1)`; the simulation stakes 25%
of that fraction of available equity. It chooses a single highest-EV candidate
per match so it never backs both players, and prevents overlapping matches from
committing the same cash twice.

`tennis_value.data.odds_papi.OddsPapiClient` validates the OddsPapi key and retrieves
fixture-scoped historical price timelines. Its historical endpoint accepts a
maximum of three bookmaker slugs at a time, so a nine-book whitelist is fetched
as three provenance-preserving requests. The selected book whitelist and its
historical Wimbledon coverage must be checked before a real backtest is run.

Completed matches settle as wins or losses. Retirements, cancellations, and
void results are recorded as void with no turnover or profit. A real historical
backtest still needs historical odds snapshots and a separately sourced,
provider-normalized match-result feed.

## ATP Wimbledon 2026 OddsPapi backtest

The runnable OddsPapi workflow uses the Wimbledon Men Singles tournament
(`2555`), the nine validated sportsbooks, and a fixed decision time of 60
minutes before scheduled start. It uses the scheduled `startTime` from the
fixture—not `trueStartTime`—and selects each bookmaker's latest valid
match-winner odds known at or before that decision time. This historical
workflow deliberately has no quote-age cutoff; it still excludes all odds
observed after the decision time. It evaluates the market with
`configs/research.toml`, starts with `10000` equity, and stakes 25% of the
full-Kelly fraction.

The command stores exact fixture, settlement, and historical-odds responses
under `data/oddspapi/wimbledon_atp_2026`. Writes are atomic, valid cached JSON is
reused, and rerunning the command resumes instead of requesting completed
fixtures again. The cache is ignored by Git.

Before making provider requests, confirm `.env` contains:

```text
ODDS_PAPI_KEY=your-key
ODDS_PAPI_BASE_URL=https://api.oddspapi.io/v4
```

Inspect the command without accessing OddsPapi:

```bash
python -m tennis_value backtest-atp-wimbledon --help
```

Run or resume the complete backtest:

```bash
python -m tennis_value backtest-atp-wimbledon \
  --config configs/research.toml \
  --cache-directory data/oddspapi/wimbledon_atp_2026
```

The first complete run uses one billable fixture request and one billable
settlement request per returned match. Historical-odds calls are issued in
three groups of three bookmakers and follow the provider cooldown. Subsequent
runs use the local cache and normally make no requests. Do not delete a partial
cache merely to restart: rerun the same command and it will continue from the
last valid response.

### Export the 127-match dataset to CSV

After the raw cache is complete, create two Excel- or Numbers-ready CSV files
without contacting OddsPapi:

```bash
python -m tennis_value export-atp-wimbledon-csv \
  --config configs/research.toml \
  --cache-directory data/oddspapi/wimbledon_atp_2026 \
  --output-directory reports/wimbledon_atp_2026
```

`wimbledon_atp_2026_matches.csv` has one row per fixture, including players,
scheduled and decision times, settlement, bookmaker coverage, candidate count,
and any selected 25%-Kelly bet. `wimbledon_atp_2026_offers.csv` has one row per
evaluated player/bookmaker offer, including observed time, de-vigged overround,
leave-one-out consensus, EV, peer range, flags, and Kelly/settlement details
where that offer was selected.

## Development

The project targets Python 3.12.

```bash
python -m pip install -e ".[dev]"
pytest
ruff check .
mypy tennis_value
```
