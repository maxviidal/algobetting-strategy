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

## Development

The project targets Python 3.12.

```bash
python -m pip install -e ".[dev]"
pytest
ruff check .
mypy tennis_value
```
