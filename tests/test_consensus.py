from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tennis_value.consensus import (
    leave_one_out_median_consensus,
    median_probability,
)
from tennis_value.domain import MatchWinnerPrice, OddsSnapshot
from tennis_value.pricing import DeViggedMarket, proportional_devig

CALCULATED_AT = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def make_market(bookmaker_id: str, first_probability: str) -> DeViggedMarket:
    probability = Decimal(first_probability)
    snapshot = OddsSnapshot(
        snapshot_id=f"snapshot-{bookmaker_id}",
        match_id="match-1",
        bookmaker_id=bookmaker_id,
        observed_at=datetime(2026, 7, 30, 11, 59, tzinfo=UTC),
        prices=(
            MatchWinnerPrice(1, Decimal(1) / probability),
            MatchWinnerPrice(2, Decimal(1) / (Decimal(1) - probability)),
        ),
        source="test",
        source_event_id="event-1",
    )
    return proportional_devig(snapshot, calculated_at=CALCULATED_AT)


def test_median_probability_handles_odd_and_even_counts() -> None:
    assert median_probability(
        (Decimal("0.2"), Decimal("0.6"), Decimal("0.4"))
    ) == Decimal("0.4")
    assert median_probability(
        (
            Decimal("0.2"),
            Decimal("0.6"),
            Decimal("0.4"),
            Decimal("0.5"),
        )
    ) == Decimal("0.45")


def test_leave_one_out_consensus_excludes_target_bookmaker() -> None:
    markets = (
        make_market("a", "0.90"),
        make_market("b", "0.40"),
        make_market("c", "0.50"),
        make_market("d", "0.60"),
        make_market("e", "0.70"),
    )

    estimate = leave_one_out_median_consensus(
        markets,
        target_bookmaker_id="a",
        player_id=1,
        calculated_at=CALCULATED_AT,
    )

    assert estimate.probability == pytest.approx(
        Decimal("0.55"),
        abs=Decimal("1e-26"),
    )
    assert estimate.peer_count == 4
    assert "snapshot-a" not in estimate.peer_snapshot_ids
    assert estimate.peer_snapshot_ids == (
        "snapshot-b",
        "snapshot-c",
        "snapshot-d",
        "snapshot-e",
    )
    assert estimate.minimum_peer_probability == pytest.approx(
        Decimal("0.4"),
        abs=Decimal("1e-26"),
    )
    assert estimate.maximum_peer_probability == pytest.approx(
        Decimal("0.7"),
        abs=Decimal("1e-26"),
    )
    assert estimate.peer_probability_range == pytest.approx(
        Decimal("0.3"),
        abs=Decimal("1e-26"),
    )
