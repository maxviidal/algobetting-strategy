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

## Development

The project targets Python 3.12.

```bash
python -m pip install -e ".[dev]"
pytest
ruff check .
mypy tennis_value
```
