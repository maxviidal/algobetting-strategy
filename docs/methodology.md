# Methodology

The baseline methodology converts decimal odds to implied probabilities,
removes each bookmaker's overround with proportional normalization, and
aggregates the resulting probabilities with a robust statistic such as the
median.

Expected value per unit staked is:

```text
expected_value = offered_odds * consensus_probability - 1
```

Backtests must use only snapshots available at the simulated decision time.
They must report assumptions, exclusions, execution constraints, calibration,
sample size, profitability, and robustness. Detailed requirements are recorded
in `AGENTS.md`.
