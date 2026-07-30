from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from tennis_value.ingestion import (
    InvalidCollectionTimeError,
    InvalidJsonError,
    InvalidOddsApiPayloadError,
    RawResponseReadError,
    ingest_odds_api_json,
    load_odds_api_json,
)


def test_ingest_current_odds_response_preserves_raw_bytes() -> None:
    raw_response = b'[{"id":"event-1","home_team":"Player A","away_team":"Player B"}]'
    collected_at = datetime(
        2026,
        7,
        30,
        12,
        0,
        tzinfo=timezone(timedelta(hours=2)),
    )

    response = ingest_odds_api_json(
        raw_response,
        collected_at=collected_at,
        source="test-fixture",
    )

    assert response.raw_bytes is raw_response
    assert response.payload == [
        {
            "id": "event-1",
            "home_team": "Player A",
            "away_team": "Player B",
        }
    ]
    assert response.events[0]["id"] == "event-1"
    assert response.collected_at == datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    assert response.source == "test-fixture"
    assert response.provider_snapshot_at is None


def test_ingest_historical_odds_response_records_snapshot_time() -> None:
    raw_response = (
        b'{"timestamp":"2026-07-30T10:00:00Z",'
        b'"previous_timestamp":null,"next_timestamp":null,'
        b'"data":[{"id":"event-1"}]}'
    )

    response = ingest_odds_api_json(
        raw_response,
        collected_at=datetime(2026, 7, 30, 10, 1, tzinfo=UTC),
        source="historical-fixture",
    )

    assert response.provider_snapshot_at == "2026-07-30T10:00:00Z"
    assert response.events == ({"id": "event-1"},)


def test_load_odds_api_json_reads_an_explicit_path(tmp_path: Path) -> None:
    response_path = tmp_path / "odds.json"
    response_path.write_bytes(b"[]")

    response = load_odds_api_json(
        response_path,
        collected_at=datetime(2026, 7, 30, 10, 0, tzinfo=UTC),
    )

    assert response.raw_bytes == b"[]"
    assert response.events == ()
    assert response.source == str(response_path.resolve())


def test_load_odds_api_json_reports_read_failure(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.json"

    with pytest.raises(RawResponseReadError, match="missing.json"):
        load_odds_api_json(
            missing_path,
            collected_at=datetime(2026, 7, 30, 10, 0, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "raw_response",
    [
        b"{",
        b'{"value": NaN}',
        b"\xff",
    ],
)
def test_ingest_rejects_invalid_json(raw_response: bytes) -> None:
    with pytest.raises(InvalidJsonError, match="not valid JSON"):
        ingest_odds_api_json(
            raw_response,
            collected_at=datetime(2026, 7, 30, 10, 0, tzinfo=UTC),
            source="invalid-fixture",
        )


def test_ingest_requires_timezone_aware_collection_time() -> None:
    with pytest.raises(
        InvalidCollectionTimeError,
        match="timezone-aware",
    ):
        ingest_odds_api_json(
            b"[]",
            collected_at=datetime(2026, 7, 30, 10, 0),
            source="test-fixture",
        )


@pytest.mark.parametrize(
    ("raw_response", "expected_message"),
    [
        (b'"not an event array"', "must be an event array"),
        (b"[null]", "event at index 0"),
        (b'{"timestamp":"2026-07-30T10:00:00Z"}', "'data' event array"),
        (b'{"data":[]}', "string 'timestamp'"),
    ],
)
def test_ingest_rejects_invalid_odds_api_shapes(
    raw_response: bytes,
    expected_message: str,
) -> None:
    with pytest.raises(InvalidOddsApiPayloadError, match=expected_message):
        ingest_odds_api_json(
            raw_response,
            collected_at=datetime(2026, 7, 30, 10, 0, tzinfo=UTC),
            source="invalid-fixture",
        )
