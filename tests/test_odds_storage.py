import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tennis_value.config import AppSettings, CollectionSettings
from tennis_value.data.domain import Match, MatchWinnerPrice, OddsSnapshot
from tennis_value.data.ingestion import ingest_odds_api_json
from tennis_value.data.storage import (
    OddsStorageError,
    SqliteOddsRepository,
    StorageConflictError,
)
from tennis_value.signals import evaluate_market

DECISION_AT = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
CALCULATED_AT = datetime(2026, 7, 30, 12, 0, 1, tzinfo=UTC)
MATCH = Match(
    match_id="match-1",
    tournament_id="tournament-1",
    player_ids=(1, 2),
    scheduled_start=datetime(2026, 7, 30, 13, 0, tzinfo=UTC),
)


@pytest.fixture
def odds_repository() -> tuple[SqliteOddsRepository, sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    return SqliteOddsRepository(connection), connection


def make_snapshot(
    bookmaker_id: str,
    *,
    observed_at: datetime,
    first_odds: str = "2.00",
    second_odds: str = "2.00",
    suffix: str = "",
    snapshot_id: str | None = None,
) -> OddsSnapshot:
    return OddsSnapshot(
        snapshot_id=snapshot_id or f"snapshot-{bookmaker_id}{suffix}",
        match_id=MATCH.match_id,
        bookmaker_id=bookmaker_id,
        observed_at=observed_at,
        prices=(
            MatchWinnerPrice(1, Decimal(first_odds)),
            MatchWinnerPrice(2, Decimal(second_odds)),
        ),
        source="test-provider",
        source_event_id="event-1",
    )


def test_raw_response_is_preserved_exactly_and_insert_is_idempotent(
    odds_repository: tuple[SqliteOddsRepository, sqlite3.Connection],
) -> None:
    repository, connection = odds_repository
    raw_bytes = (
        b'{"timestamp":"2026-07-30T12:00:00Z",'
        b'"previous_timestamp":null,"next_timestamp":null,"data":[]}'
    )
    response = ingest_odds_api_json(
        raw_bytes,
        collected_at=CALCULATED_AT,
        source="historical-api",
    )

    first_id = repository.save_raw_response(response)
    second_id = repository.save_raw_response(response)
    stored = repository.get_raw_response(first_id)

    assert first_id == second_id
    assert stored.raw_bytes == raw_bytes
    assert stored.collected_at == CALCULATED_AT
    assert stored.source == "historical-api"
    assert stored.provider_snapshot_at == "2026-07-30T12:00:00Z"
    count = connection.execute(
        "SELECT COUNT(*) FROM raw_odds_responses"
    ).fetchone()
    assert count == (1,)


def test_match_and_snapshot_inserts_are_idempotent(
    odds_repository: tuple[SqliteOddsRepository, sqlite3.Connection],
) -> None:
    repository, connection = odds_repository
    snapshot = make_snapshot("a", observed_at=DECISION_AT)

    repository.save_match(MATCH)
    repository.save_match(MATCH)
    repository.save_snapshot(snapshot)
    repository.save_snapshot(snapshot)

    assert repository.get_match(MATCH.match_id) == MATCH
    match_count = connection.execute("SELECT COUNT(*) FROM matches").fetchone()
    snapshot_count = connection.execute(
        "SELECT COUNT(*) FROM odds_snapshots"
    ).fetchone()
    price_count = connection.execute(
        "SELECT COUNT(*) FROM odds_snapshot_prices"
    ).fetchone()
    assert match_count == (1,)
    assert snapshot_count == (1,)
    assert price_count == (2,)


def test_stable_ids_reject_conflicting_immutable_data(
    odds_repository: tuple[SqliteOddsRepository, sqlite3.Connection],
) -> None:
    repository, _ = odds_repository
    repository.save_match(MATCH)
    repository.save_snapshot(
        make_snapshot(
            "a",
            observed_at=DECISION_AT,
            snapshot_id="stable-snapshot",
        )
    )
    conflicting = make_snapshot(
        "a",
        observed_at=DECISION_AT,
        first_odds="2.10",
        second_odds="1.91",
        snapshot_id="stable-snapshot",
    )

    with pytest.raises(StorageConflictError, match="stable-snapshot"):
        repository.save_snapshot(conflicting)


def test_snapshot_participants_must_match_persisted_match(
    odds_repository: tuple[SqliteOddsRepository, sqlite3.Connection],
) -> None:
    repository, _ = odds_repository
    repository.save_match(MATCH)
    invalid = OddsSnapshot(
        snapshot_id="snapshot-wrong-players",
        match_id=MATCH.match_id,
        bookmaker_id="a",
        observed_at=DECISION_AT,
        prices=(
            MatchWinnerPrice(3, Decimal("2.00")),
            MatchWinnerPrice(4, Decimal("2.00")),
        ),
        source="test-provider",
        source_event_id="event-1",
    )

    with pytest.raises(OddsStorageError, match="participants do not match"):
        repository.save_snapshot(invalid)


def test_as_of_rejects_conflicting_quotes_at_latest_timestamp(
    odds_repository: tuple[SqliteOddsRepository, sqlite3.Connection],
) -> None:
    repository, _ = odds_repository
    repository.save_match(MATCH)
    repository.save_snapshots(
        (
            make_snapshot(
                "a",
                observed_at=DECISION_AT,
                first_odds="2.00",
                second_odds="2.00",
                suffix="-first-source",
            ),
            make_snapshot(
                "a",
                observed_at=DECISION_AT,
                first_odds="2.10",
                second_odds="1.91",
                suffix="-second-source",
            ),
        )
    )

    with pytest.raises(StorageConflictError, match="conflicting snapshots"):
        repository.latest_snapshots_as_of(
            MATCH.match_id,
            decision_at=DECISION_AT,
            maximum_age=timedelta(minutes=5),
        )


def test_as_of_deduplicates_equivalent_latest_quotes_deterministically(
    odds_repository: tuple[SqliteOddsRepository, sqlite3.Connection],
) -> None:
    repository, _ = odds_repository
    repository.save_match(MATCH)
    repository.save_snapshots(
        (
            make_snapshot(
                "a",
                observed_at=DECISION_AT,
                suffix="-z",
            ),
            make_snapshot(
                "a",
                observed_at=DECISION_AT,
                suffix="-a",
            ),
        )
    )

    selected = repository.latest_snapshots_as_of(
        MATCH.match_id,
        decision_at=DECISION_AT,
        maximum_age=timedelta(minutes=5),
    )

    assert [snapshot.snapshot_id for snapshot in selected] == ["snapshot-a-a"]


def test_as_of_returns_latest_fresh_snapshot_per_bookmaker(
    odds_repository: tuple[SqliteOddsRepository, sqlite3.Connection],
) -> None:
    repository, _ = odds_repository
    repository.save_match(MATCH)
    snapshots = (
        make_snapshot(
            "a",
            observed_at=DECISION_AT - timedelta(minutes=20),
            suffix="-old",
        ),
        make_snapshot(
            "a",
            observed_at=DECISION_AT - timedelta(minutes=5),
            first_odds="2.10",
            second_odds="1.91",
            suffix="-latest",
        ),
        make_snapshot(
            "a",
            observed_at=DECISION_AT + timedelta(minutes=5),
            first_odds="2.20",
            second_odds="1.83",
            suffix="-future",
        ),
        *(
            make_snapshot(
                bookmaker,
                observed_at=DECISION_AT - timedelta(minutes=10),
            )
            for bookmaker in ("b", "c", "d", "e")
        ),
    )
    repository.save_snapshots(tuple(reversed(snapshots)))

    selected = repository.latest_snapshots_as_of(
        MATCH.match_id,
        decision_at=DECISION_AT,
        maximum_age=timedelta(minutes=15),
    )

    assert [snapshot.bookmaker_id for snapshot in selected] == [
        "a",
        "b",
        "c",
        "d",
        "e",
    ]
    assert selected[0].snapshot_id == "snapshot-a-latest"
    assert selected[0].prices[0].decimal_odds == Decimal("2.10")
    assert all(snapshot.observed_at <= DECISION_AT for snapshot in selected)


def test_as_of_includes_freshness_boundary_and_excludes_older_quotes(
    odds_repository: tuple[SqliteOddsRepository, sqlite3.Connection],
) -> None:
    repository, _ = odds_repository
    repository.save_match(MATCH)
    repository.save_snapshots(
        (
            make_snapshot(
                "boundary",
                observed_at=DECISION_AT - timedelta(minutes=5),
            ),
            make_snapshot(
                "stale",
                observed_at=DECISION_AT - timedelta(minutes=5, seconds=1),
            ),
        )
    )

    selected = repository.latest_snapshots_as_of(
        MATCH.match_id,
        decision_at=DECISION_AT,
        maximum_age=timedelta(minutes=5),
    )

    assert [snapshot.bookmaker_id for snapshot in selected] == ["boundary"]


def test_persisted_as_of_market_feeds_point_in_time_signal_model(
    odds_repository: tuple[SqliteOddsRepository, sqlite3.Connection],
) -> None:
    repository, _ = odds_repository
    repository.save_match(MATCH)
    repository.save_snapshots(
        tuple(
            make_snapshot(
                bookmaker,
                observed_at=DECISION_AT - timedelta(minutes=10),
            )
            for bookmaker in ("a", "b", "c", "d", "e")
        )
        + (
            make_snapshot(
                "a",
                observed_at=DECISION_AT + timedelta(minutes=1),
                first_odds="3.00",
                second_odds="1.50",
                suffix="-future",
            ),
        )
    )
    stored_snapshots = repository.latest_snapshots_as_of(
        MATCH.match_id,
        decision_at=DECISION_AT,
        maximum_age=timedelta(minutes=15),
    )
    settings = AppSettings(
        collection=CollectionSettings(
            minimum_bookmakers=5,
            maximum_quote_age_seconds=900,
        )
    )

    result = evaluate_market(
        repository.get_match(MATCH.match_id),
        stored_snapshots,
        decision_at=DECISION_AT,
        calculated_at=CALCULATED_AT,
        settings=settings,
    )

    assert result.eligible_bookmaker_count == 5
    assert len(result.evaluations) == 10
    assert all(
        evaluation.snapshot_id != "snapshot-a-future"
        for evaluation in result.evaluations
    )


def test_snapshot_can_reference_preserved_raw_response(
    odds_repository: tuple[SqliteOddsRepository, sqlite3.Connection],
) -> None:
    repository, connection = odds_repository
    response = ingest_odds_api_json(
        b"[]",
        collected_at=CALCULATED_AT,
        source="current-api",
    )
    response_id = repository.save_raw_response(response)
    repository.save_match(MATCH)
    repository.save_snapshot(
        make_snapshot("a", observed_at=DECISION_AT),
        raw_response_id=response_id,
    )

    row = connection.execute(
        "SELECT raw_response_id FROM odds_snapshots WHERE snapshot_id = ?",
        ("snapshot-a",),
    ).fetchone()

    assert row == (response_id,)


def test_same_immutable_snapshot_can_link_to_multiple_raw_responses(
    odds_repository: tuple[SqliteOddsRepository, sqlite3.Connection],
) -> None:
    repository, connection = odds_repository
    first_response = ingest_odds_api_json(
        b"[]",
        collected_at=CALCULATED_AT,
        source="current-api",
    )
    second_response = ingest_odds_api_json(
        b"[]",
        collected_at=CALCULATED_AT + timedelta(seconds=30),
        source="current-api",
    )
    first_id = repository.save_raw_response(first_response)
    second_id = repository.save_raw_response(second_response)
    repository.save_match(MATCH)
    snapshot = make_snapshot("a", observed_at=DECISION_AT)

    repository.save_snapshot(snapshot, raw_response_id=first_id)
    repository.save_snapshot(snapshot, raw_response_id=second_id)

    links = connection.execute(
        """
        SELECT response_id
        FROM raw_response_snapshots
        WHERE snapshot_id = ?
        ORDER BY response_id
        """,
        (snapshot.snapshot_id,),
    ).fetchall()
    assert links == sorted([(first_id,), (second_id,)])


def test_as_of_requires_utc_decision_time(
    odds_repository: tuple[SqliteOddsRepository, sqlite3.Connection],
) -> None:
    repository, _ = odds_repository
    repository.save_match(MATCH)

    with pytest.raises(ValueError, match="decision_at must be in UTC"):
        repository.latest_snapshots_as_of(
            MATCH.match_id,
            decision_at=datetime.fromisoformat("2026-07-30T14:00:00+02:00"),
            maximum_age=timedelta(minutes=15),
        )
