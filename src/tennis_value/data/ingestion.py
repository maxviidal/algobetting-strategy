"""Raw JSON ingestion for The Odds API responses."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, cast

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type OddsApiEvent = dict[str, JsonValue]


class IngestionError(ValueError):
    """Base exception for raw-response ingestion failures."""


class RawResponseReadError(IngestionError):
    """Raised when a raw response cannot be read from its source."""


class InvalidJsonError(IngestionError):
    """Raised when a raw response is not valid JSON."""


class InvalidOddsApiPayloadError(IngestionError):
    """Raised when valid JSON does not have an Odds API response shape."""


class InvalidCollectionTimeError(IngestionError):
    """Raised when the response collection time is not timezone-aware."""


@dataclass(frozen=True, slots=True)
class IngestedOddsApiResponse:
    """An unchanged raw response and its minimally validated JSON representation."""

    raw_bytes: bytes
    payload: JsonValue
    events: tuple[OddsApiEvent, ...]
    collected_at: datetime
    source: str
    provider_snapshot_at: str | None


def ingest_odds_api_json(
    raw_response: bytes,
    *,
    collected_at: datetime,
    source: str,
) -> IngestedOddsApiResponse:
    """Parse and minimally validate raw JSON returned by The Odds API.

    The exact response bytes are retained for traceability and replay. This
    function accepts both the current-odds response (a JSON event array) and the
    historical-odds response (an object containing ``timestamp`` and ``data``).
    Entity resolution and market-level cleaning belong in normalization.
    """
    collected_at_utc = _as_utc(collected_at)
    payload = _decode_json(raw_response)
    events, provider_snapshot_at = _extract_events(payload)

    return IngestedOddsApiResponse(
        raw_bytes=raw_response,
        payload=payload,
        events=events,
        collected_at=collected_at_utc,
        source=source,
        provider_snapshot_at=provider_snapshot_at,
    )


def load_odds_api_json(
    path: Path,
    *,
    collected_at: datetime,
) -> IngestedOddsApiResponse:
    """Read and ingest a raw Odds API response stored in a local JSON file."""
    try:
        raw_response = path.read_bytes()
    except OSError as error:
        message = f"Could not read raw Odds API response from {path}: {error}"
        raise RawResponseReadError(message) from error

    return ingest_odds_api_json(
        raw_response,
        collected_at=collected_at,
        source=str(path.resolve()),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        message = "collected_at must be a timezone-aware datetime"
        raise InvalidCollectionTimeError(message)
    return value.astimezone(UTC)


def _decode_json(raw_response: bytes) -> JsonValue:
    try:
        payload = json.loads(
            raw_response,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        message = f"Raw Odds API response is not valid JSON: {error}"
        raise InvalidJsonError(message) from error
    return cast(JsonValue, payload)


def _reject_nonstandard_json_constant(value: str) -> NoReturn:
    message = f"Non-standard JSON constant {value!r} is not allowed"
    raise ValueError(message)


def _extract_events(
    payload: JsonValue,
) -> tuple[tuple[OddsApiEvent, ...], str | None]:
    provider_snapshot_at: str | None = None
    raw_events: JsonValue

    if isinstance(payload, list):
        raw_events = payload
    elif isinstance(payload, dict):
        raw_events = payload.get("data")
        provider_snapshot_at_value = payload.get("timestamp")
        if not isinstance(raw_events, list):
            message = "Historical Odds API response must contain a 'data' event array"
            raise InvalidOddsApiPayloadError(message)
        if not isinstance(provider_snapshot_at_value, str):
            message = "Historical Odds API response must contain a string 'timestamp'"
            raise InvalidOddsApiPayloadError(message)
        provider_snapshot_at = provider_snapshot_at_value
    else:
        message = (
            "Odds API response must be an event array or a historical response object"
        )
        raise InvalidOddsApiPayloadError(message)

    events: list[OddsApiEvent] = []
    for index, event in enumerate(raw_events):
        if not isinstance(event, dict):
            message = f"Odds API event at index {index} must be a JSON object"
            raise InvalidOddsApiPayloadError(message)
        events.append(event)

    return tuple(events), provider_snapshot_at
