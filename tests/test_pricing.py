from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tennis_value.data.domain import MatchWinnerPrice, OddsSnapshot
from tennis_value.pricing import expected_value, fair_odds, power_devig

CALCULATED_AT = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def make_snapshot(first_odds: str, second_odds: str) -> OddsSnapshot:
    return OddsSnapshot(
        snapshot_id="snapshot-1",
        match_id="match-1",
        bookmaker_id="bookmaker-1",
        observed_at=datetime(2026, 7, 30, 11, 59, tzinfo=UTC),
        prices=(
            MatchWinnerPrice(1, Decimal(first_odds)),
            MatchWinnerPrice(2, Decimal(second_odds)),
        ),
        source="test",
        source_event_id="event-1",
    )


@pytest.mark.parametrize(
    ("first_odds", "second_odds"),
    [
        ("2.00", "2.00"),
        ("1.50", "2.80"),
        ("1.10", "7.00"),
        ("2.10", "2.10"),
        ("2.05", "2.05"),
    ],
)
def test_power_devig_probabilities_sum_to_one(
    first_odds: str,
    second_odds: str,
) -> None:
    market = power_devig(
        make_snapshot(first_odds, second_odds),
        calculated_at=CALCULATED_AT,
    )

    probability_sum = sum(
        (price.fair_probability for price in market.prices),
        start=Decimal(0),
    )
    assert probability_sum == pytest.approx(Decimal(1), abs=Decimal("1e-26"))
    assert market.overround == (
        Decimal(1) / Decimal(first_odds) + Decimal(1) / Decimal(second_odds)
    )
    assert market.snapshot_id == "snapshot-1"
    assert market.method == "power"
    assert market.calculated_at == CALCULATED_AT


def test_power_devig_symmetric_market() -> None:
    market = power_devig(
        make_snapshot("1.91", "1.91"),
        calculated_at=CALCULATED_AT,
    )

    assert all(
        probability == Decimal("0.5")
        for probability in (price.fair_probability for price in market.prices)
    )


def test_power_devig_differs_from_proportional_for_asymmetric_market() -> None:
    market = power_devig(
        make_snapshot("1.50", "2.80"),
        calculated_at=CALCULATED_AT,
    )

    favourite_probability = market.probability_for(1)
    proportional_probability = (Decimal(1) / Decimal("1.50")) / (
        Decimal(1) / Decimal("1.50") + Decimal(1) / Decimal("2.80")
    )

    assert favourite_probability > proportional_probability


@pytest.mark.parametrize(
    ("odds", "probability", "expected"),
    [
        ("2.10", "0.50", "0.0500"),
        ("2.09", "0.50", "0.0450"),
        ("2.11", "0.50", "0.0550"),
    ],
)
def test_expected_value_boundary(
    odds: str,
    probability: str,
    expected: str,
) -> None:
    assert expected_value(Decimal(odds), Decimal(probability)) == Decimal(expected)


def test_fair_odds_is_probability_reciprocal() -> None:
    assert fair_odds(Decimal("0.4")) == Decimal("2.5")


def test_pricing_requires_utc_calculation_time() -> None:
    with pytest.raises(ValueError, match="calculated_at must be in UTC"):
        power_devig(
            make_snapshot("2", "2"),
            calculated_at=datetime.fromisoformat("2026-07-30T14:00:00+02:00"),
        )
