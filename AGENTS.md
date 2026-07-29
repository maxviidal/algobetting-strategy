# AGENTS.md

## Project Objective

Build a reproducible Python system that identifies potential value bets in
professional tennis match-winner markets.

The system will:

1. Collect pre-match win/lose decimal odds from multiple bookmakers.
2. Normalize players, tournaments, matches, bookmakers, and observation times.
3. Remove each bookmaker's overround to estimate fair outcome probabilities.
4. Combine de-vigged prices into a robust market-consensus probability.
5. Compare offered odds with consensus fair odds to detect unusually mispriced
   selections.
6. Backtest signals using only information available at the decision timestamp.
7. Evaluate profitability, calibration, robustness, and execution constraints
   before any live use.

This project produces research signals, not guaranteed profits. Avoid claims of
certainty and make assumptions, data limitations, and sources of bias explicit.

## Planned Methodology

### 1. Data Collection

- Ingest bookmaker odds through adapters with a shared interface.
- Preserve raw responses unchanged for traceability and replay.
- Store decimal odds, bookmaker, event identifiers, players, tournament, market,
  collection timestamp, and scheduled start time.
- Record odds snapshots rather than overwriting previous observations.
- Use timezone-aware UTC timestamps internally.
- Respect provider terms, rate limits, and applicable laws.

### 2. Data Normalization and Validation

- Create stable internal identifiers for players, tournaments, and matches.
- Resolve naming differences without silently merging ambiguous entities.
- Validate that match-winner markets contain the expected mutually exclusive
  outcomes and positive decimal odds greater than `1.0`.
- Flag stale, suspended, incomplete, duplicated, or internally inconsistent
  quotes.
- Keep validation failures observable; do not silently discard data.

### 3. Margin Removal

- Convert decimal odds to implied probabilities with `p = 1 / odds`.
- Calculate bookmaker overround from the sum of implied outcome probabilities.
- Implement margin-removal methods behind a common interface.
- Start with proportional normalization as the baseline:
  `fair_p_i = implied_p_i / sum(implied_p)`.
- Add alternative methods only when they can be tested and compared, such as
  additive, power, or Shin-style adjustments.

### 4. Market Consensus

- Construct consensus from de-vigged bookmaker probabilities, never directly
  from raw implied probabilities.
- Begin with a transparent robust aggregate such as the median or trimmed mean.
- Consider bookmaker reliability, quote freshness, liquidity, and correlated
  price sources before introducing weights.
- Require a configurable minimum number of valid independent bookmakers.
- Prevent the bookmaker being evaluated from mechanically defining its own
  benchmark; support leave-one-bookmaker-out consensus.

### 5. Value Signal

- Convert consensus probability to fair odds with
  `fair_odds = 1 / consensus_probability`.
- Calculate expected value per unit staked as
  `expected_value = offered_odds * consensus_probability - 1`.
- Emit a candidate only when it passes configurable thresholds for expected
  value, bookmaker coverage, quote freshness, and data quality.
- Keep signal generation separate from staking and bet execution.
- Treat anomalous prices as possible data errors until validated.

### 6. Backtesting and Evaluation

- Use point-in-time snapshots and prohibit future information leakage.
- Model realistic availability, rejected bets, stake limits, timing, and odds
  movement where data permits.
- Track return on investment, yield, drawdown, turnover, hit rate, closing-line
  value, probability calibration, and sample size.
- Report results by tournament level, surface, bookmaker, odds band, season, and
  signal strength.
- Prefer walk-forward or time-based validation over random train/test splits.
- Record all experiment parameters and random seeds.

## Coding Conventions

### Python and Tooling

- Target Python `3.12` unless project configuration states otherwise.
- Use a `src/` package layout and manage project metadata in `pyproject.toml`.
- Add type hints to public functions, methods, and data structures.
- Prefer small, composable pure functions for probability calculations.
- Use `pathlib.Path` instead of string-based path manipulation.
- Use timezone-aware `datetime` values and UTC for persistence.
- Represent money and odds carefully; avoid hidden binary floating-point
  assumptions in settlement or accounting code.
- Use structured logging instead of `print` in library and application code.
- Keep secrets in environment variables or an ignored local secrets file.
  Never commit API keys, credentials, account identifiers, or betting history.

### Design

- Separate raw ingestion, normalization, domain logic, storage, modeling,
  backtesting, and execution concerns.
- Keep bookmaker-specific behavior inside provider adapters.
- Make margin-removal and consensus strategies replaceable through explicit
  interfaces.
- Put configuration in typed settings rather than scattered constants.
- Prefer explicit domain models over loosely structured dictionaries after the
  ingestion boundary.
- Preserve provenance: derived records should identify their input snapshots,
  method, parameters, and calculation timestamp.
- Avoid adding live bet placement until research and risk controls are proven.

### Quality

- Format and lint with Ruff; type-check with mypy.
- Test with pytest and mirror package structure under `tests/`.
- Add unit tests for probability math, edge cases, and data validation.
- Add integration tests for provider adapters using recorded or synthetic
  fixtures; tests must not depend on live bookmaker APIs.
- Use deterministic fixtures and seed randomized tests.
- Compare floating-point values with tolerances, not direct equality.
- Include regression tests whenever fixing a bug.
- Raise specific exceptions with actionable messages.
- Do not catch broad exceptions unless adding context and re-raising or handling
  them at an application boundary.

### Git and Documentation

- Keep commits focused and use imperative commit messages.
- Do not commit generated datasets, credentials, local databases, caches,
  notebook outputs, or virtual environments.
- Document assumptions and formulas close to the relevant module or in `docs/`.
- Update tests and documentation with behavioral changes.
- Avoid unrelated refactors in feature or bug-fix changes.

## Proposed Folder Structure

```text
.
├── AGENTS.md
├── README.md
├── pyproject.toml
├── .env.example
├── .gitignore
├── configs/
│   ├── development.toml
│   └── research.toml
├── data/
│   ├── raw/                 # Immutable source payloads; Git-ignored
│   ├── interim/             # Normalized intermediate datasets; Git-ignored
│   └── processed/           # Model-ready datasets; Git-ignored
├── docs/
│   ├── architecture.md
│   └── methodology.md
├── notebooks/               # Exploration only; production logic belongs in src
├── scripts/                 # Thin operational and research entry points
├── src/
│   └── tennis_value/
│       ├── __init__.py
│       ├── config.py
│       ├── domain/          # Typed entities and domain rules
│       ├── ingestion/       # Bookmaker adapters and raw collection
│       ├── normalization/   # Entity matching and quote validation
│       ├── pricing/         # Implied probabilities and margin removal
│       ├── consensus/       # Market aggregation strategies
│       ├── signals/         # Value detection and filtering
│       ├── storage/         # Repositories and persistence implementations
│       ├── backtesting/     # Point-in-time simulation and evaluation
│       └── cli/             # Command-line entry points
└── tests/
    ├── unit/
    ├── integration/
    └── fixtures/
```

Create directories only when they are needed. Every package should have a clear
responsibility; do not add placeholder abstractions or duplicate production
logic in notebooks and scripts.

## Definition of Done

A change is complete when:

- Its behavior is covered by appropriate automated tests.
- Formatting, linting, and relevant tests pass.
- Types and public interfaces are clear.
- Data provenance and point-in-time correctness are preserved.
- Configuration, assumptions, and user-facing behavior are documented.
- No secrets or large generated artifacts are included.
