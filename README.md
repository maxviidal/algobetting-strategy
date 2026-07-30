# Tennis Value

Research software for identifying and backtesting potential value in
professional tennis match-winner markets.

The project will collect point-in-time bookmaker odds, normalize the underlying
events, remove bookmaker margins, construct a robust market consensus, and flag
offers whose expected value exceeds configurable thresholds.

The repository is currently an initial scaffold. It does not place bets and
does not claim guaranteed profitability. See [AGENTS.md](AGENTS.md) for the
planned methodology, architecture, and engineering standards.

## Development

The project targets Python 3.12 and uses a `src/` package layout.

```bash
python -m pip install -e ".[dev]"
pytest
ruff check .
mypy src
```
