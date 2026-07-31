"""Pricing and point-in-time selection for complete two-outcome markets."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import median


@dataclass(frozen=True, slots=True)
class QuoteRecord:
    """One recorded state for one outcome."""

    outcome_id: str
    decimal_odds: Decimal | None
    observed_at: datetime
    active: bool

    def __post_init__(self) -> None:
        _require_utc(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class SelectedMarket:
    """The complete market that was actually knowable at a decision time."""

    observed_at: datetime
    prices: tuple[tuple[str, Decimal], tuple[str, Decimal]]

    def price_for(self, outcome_id: str) -> Decimal:
        """Return one selected outcome price."""

        for selected_id, price in self.prices:
            if selected_id == outcome_id:
                return price
        raise KeyError(outcome_id)


def select_market_at(
    records: tuple[QuoteRecord, ...],
    *,
    outcome_ids: tuple[str, str],
    decision_at: datetime,
    maximum_age: timedelta | None = None,
) -> SelectedMarket | None:
    """Select the latest state for both outcomes without resurrecting prices.

    The latest record at or before ``decision_at`` is selected independently
    for each outcome. If either latest state is inactive, invalid, or stale,
    the whole two-sided market is unavailable.
    """

    _require_utc(decision_at, "decision_at")
    if len(set(outcome_ids)) != 2:
        raise ValueError("outcome_ids must contain two distinct outcomes")
    if maximum_age is not None and maximum_age < timedelta(0):
        raise ValueError("maximum_age must not be negative")

    chosen: list[QuoteRecord] = []
    for outcome_id in outcome_ids:
        latest = select_latest_record(
            tuple(record for record in records if record.outcome_id == outcome_id),
            decision_at=decision_at,
        )
        if latest is None or not latest.active or not _valid_odds(latest.decimal_odds):
            return None
        chosen.append(latest)

    observed_at = max(record.observed_at for record in chosen)
    if maximum_age is not None and decision_at - observed_at > maximum_age:
        return None
    return SelectedMarket(
        observed_at=observed_at,
        prices=(
            (chosen[0].outcome_id, _odds(chosen[0])),
            (chosen[1].outcome_id, _odds(chosen[1])),
        ),
    )


def select_latest_record(
    records: tuple[QuoteRecord, ...],
    *,
    decision_at: datetime,
) -> QuoteRecord | None:
    """Return the latest recorded state, whether active or inactive."""

    _require_utc(decision_at, "decision_at")
    eligible = tuple(record for record in records if record.observed_at <= decision_at)
    return max(eligible, key=lambda record: record.observed_at) if eligible else None


def proportional_probabilities(
    prices: tuple[tuple[str, Decimal], tuple[str, Decimal]],
) -> tuple[tuple[str, Decimal], tuple[str, Decimal]]:
    """Remove margin by normalizing the two implied probabilities."""

    if len({outcome_id for outcome_id, _ in prices}) != 2:
        raise ValueError("prices must contain two distinct outcomes")
    for _, price in prices:
        if not _valid_odds(price):
            raise ValueError("decimal odds must be finite and greater than 1.0")
    implied = tuple(Decimal(1) / price for _, price in prices)
    overround = sum(implied, start=Decimal(0))
    return (
        (prices[0][0], implied[0] / overround),
        (prices[1][0], implied[1] / overround),
    )


def median_decimal(values: tuple[Decimal, ...]) -> Decimal:
    """Return a deterministic median for a non-empty Decimal collection."""

    if not values:
        raise ValueError("at least one value is required")
    return median(values)


def expected_value(offered_odds: Decimal, probability: Decimal) -> Decimal:
    """Return expected profit per unit staked."""

    if not _valid_odds(offered_odds):
        raise ValueError("offered_odds must be finite and greater than 1.0")
    if not probability.is_finite() or probability < 0 or probability > 1:
        raise ValueError("probability must be finite and between 0 and 1")
    return offered_odds * probability - Decimal(1)


def fair_odds(probability: Decimal) -> Decimal:
    """Convert a strictly positive probability to decimal fair odds."""

    if not probability.is_finite() or probability <= 0 or probability > 1:
        raise ValueError("probability must be finite, greater than zero, and at most 1")
    return Decimal(1) / probability


def _odds(record: QuoteRecord) -> Decimal:
    assert record.decimal_odds is not None
    return record.decimal_odds


def _valid_odds(value: Decimal | None) -> bool:
    return value is not None and value.is_finite() and value > 1


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must be in UTC")
