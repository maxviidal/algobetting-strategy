from datetime import UTC, datetime, timedelta
from decimal import Decimal

from betting_core import QuoteRecord, select_market_at


def test_later_inactive_record_suppresses_earlier_active_price() -> None:
    decision = datetime(2026, 1, 1, 12, tzinfo=UTC)
    records = (
        QuoteRecord("home", Decimal("2.1"), decision - timedelta(minutes=10), True),
        QuoteRecord("home", None, decision - timedelta(minutes=2), False),
        QuoteRecord("away", Decimal("1.8"), decision - timedelta(minutes=3), True),
    )

    assert (
        select_market_at(
            records,
            outcome_ids=("home", "away"),
            decision_at=decision,
        )
        is None
    )


def test_market_uses_later_side_timestamp_and_never_looks_ahead() -> None:
    decision = datetime(2026, 1, 1, 12, tzinfo=UTC)
    records = (
        QuoteRecord("home", Decimal("2.1"), decision - timedelta(minutes=8), True),
        QuoteRecord("away", Decimal("1.8"), decision - timedelta(minutes=3), True),
        QuoteRecord("away", Decimal("1.7"), decision + timedelta(seconds=1), True),
    )

    selected = select_market_at(
        records,
        outcome_ids=("home", "away"),
        decision_at=decision,
        maximum_age=timedelta(minutes=30),
    )

    assert selected is not None
    assert selected.observed_at == decision - timedelta(minutes=3)
    assert selected.price_for("away") == Decimal("1.8")


def test_stale_complete_market_is_excluded() -> None:
    decision = datetime(2026, 1, 1, 12, tzinfo=UTC)
    records = (
        QuoteRecord("home", Decimal("2.1"), decision - timedelta(minutes=31), True),
        QuoteRecord("away", Decimal("1.8"), decision - timedelta(minutes=31), True),
    )

    assert (
        select_market_at(
            records,
            outcome_ids=("home", "away"),
            decision_at=decision,
            maximum_age=timedelta(minutes=30),
        )
        is None
    )
