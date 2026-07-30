# Tennis Value

Research software for identifying and backtesting potential value in
professional tennis match-winner markets.

The project will collect point-in-time bookmaker odds, normalize the underlying
events, remove bookmaker margins, construct a robust market consensus, and flag
offers whose expected value exceeds configurable thresholds.

The repository is intentionally small while the first working features are
built. It does not place bets and does not claim guaranteed profitability. See
[AGENTS.md](AGENTS.md) for the planned methodology and engineering standards.

## Structure

```text
configs/       Research settings
tennis_value/  Application code
tests/         Automated tests
```

New modules and directories should be added only when working code needs them.

## Normalization

The first normalization layer is implemented in `tennis_value/normalization.py`.
It converts retained Odds API events into immutable records from
`tennis_value/domain.py`.

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
probabilities and proportionally normalized so its fair probabilities sum to
one. Every offered outcome is then compared with the median fair probability
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
boundaries. Pricing v1 supports proportional margin removal, median consensus,
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

`tennis_value.odds_papi.OddsPapiClient` validates the OddsPapi key and retrieves
fixture-scoped historical price timelines. Its historical endpoint accepts a
maximum of three bookmaker slugs at a time, so a nine-book whitelist is fetched
as three provenance-preserving requests. The selected book whitelist and its
historical Wimbledon coverage must be checked before a real backtest is run.

Completed matches settle as wins or losses. Retirements, cancellations, and
void results are recorded as void with no turnover or profit. A real historical
backtest still needs historical odds snapshots and a separately sourced,
provider-normalized match-result feed.

## Development

The project targets Python 3.12.

```bash
python -m pip install -e ".[dev]"
pytest
ruff check .
mypy tennis_value
```
