# Architecture

The system is divided into ingestion, normalization, pricing, consensus,
signals, storage, and backtesting components. Bookmaker-specific details remain
at the ingestion boundary; downstream components operate on normalized,
provenance-carrying domain models.

The intended data flow is:

```text
raw provider payloads
    -> normalized odds snapshots
    -> de-vigged probabilities
    -> leave-one-bookmaker-out consensus
    -> value candidates
    -> point-in-time backtests and evaluation
```

Package boundaries are described in `AGENTS.md`. Concrete interfaces will be
documented here as they are implemented.
