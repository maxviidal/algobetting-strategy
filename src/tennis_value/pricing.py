"""Pure probability and power margin-removal calculations."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from betting_core import (
    expected_value as core_expected_value,
)
from betting_core import (
    fair_odds as core_fair_odds,
)
from betting_core import (
    power_probabilities,
)
from tennis_value.data.domain import OddsSnapshot, PlayerId

POWER_MARGIN_METHOD = "power"


@dataclass(frozen=True, slots=True)
class DeViggedPrice:
    """One outcome's quoted and de-vigged probabilities."""

    player_id: PlayerId
    decimal_odds: Decimal
    implied_probability: Decimal
    fair_probability: Decimal


@dataclass(frozen=True, slots=True)
class DeViggedMarket:
    """A complete de-vigged market with calculation provenance."""

    snapshot_id: str
    match_id: str
    bookmaker_id: str
    observed_at: datetime
    prices: tuple[DeViggedPrice, DeViggedPrice]
    overround: Decimal
    method: str
    calculated_at: datetime

    def probability_for(self, player_id: PlayerId) -> Decimal:
        """Return the de-vigged probability for one participant."""

        for price in self.prices:
            if price.player_id == player_id:
                return price.fair_probability
        raise KeyError(f"player_id {player_id!r} is not present in this market")


def power_devig(
    snapshot: OddsSnapshot,
    *,
    calculated_at: datetime,
) -> DeViggedMarket:
    """Remove a two-way market's margin with the power method."""

    _require_utc(calculated_at, "calculated_at")
    ordered_prices = tuple(sorted(snapshot.prices, key=lambda price: price.player_id))
    implied = tuple(Decimal(1) / price.decimal_odds for price in ordered_prices)
    overround = sum(implied, start=Decimal(0))
    if not overround.is_finite() or overround <= 0:
        raise ValueError("snapshot overround must be finite and greater than zero")

    fair = dict(
        power_probabilities(
            (
                (str(ordered_prices[0].player_id), ordered_prices[0].decimal_odds),
                (str(ordered_prices[1].player_id), ordered_prices[1].decimal_odds),
            )
        )
    )
    devigged = tuple(
        DeViggedPrice(
            player_id=price.player_id,
            decimal_odds=price.decimal_odds,
            implied_probability=implied_probability,
            fair_probability=fair[str(price.player_id)],
        )
        for price, implied_probability in zip(ordered_prices, implied, strict=True)
    )
    return DeViggedMarket(
        snapshot_id=snapshot.snapshot_id,
        match_id=snapshot.match_id,
        bookmaker_id=snapshot.bookmaker_id,
        observed_at=snapshot.observed_at,
        prices=(devigged[0], devigged[1]),
        overround=overround,
        method=POWER_MARGIN_METHOD,
        calculated_at=calculated_at,
    )


def fair_odds(probability: Decimal) -> Decimal:
    """Convert a strictly positive probability to decimal fair odds."""

    if probability == 0:
        raise ValueError("probability must be greater than zero for fair odds")
    return core_fair_odds(probability)


def expected_value(
    offered_odds: Decimal,
    probability: Decimal,
) -> Decimal:
    """Return expected profit per unit staked."""

    return core_expected_value(offered_odds, probability)


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must be in UTC")
