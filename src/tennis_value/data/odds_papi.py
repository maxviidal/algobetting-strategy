"""Small client for validating and retrieving OddsPapi historical fixtures."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class OddsPapiError(RuntimeError):
    """Raised when OddsPapi cannot return a valid response."""


class OddsPapiRateLimitError(OddsPapiError):
    """Raised for HTTP 429 with the provider's requested retry delay."""

    def __init__(self, message: str, *, retry_after_seconds: float | None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class OddsPapiNotFoundError(OddsPapiError):
    """A JSON HTTP 404 response that can be preserved as an exact no-data record."""

    def __init__(self, message: str, *, raw_bytes: bytes) -> None:
        super().__init__(message)
        self.raw_bytes = raw_bytes


@dataclass(frozen=True, slots=True)
class OddsPapiResponse:
    """Unmodified provider payload plus collection provenance."""

    raw_bytes: bytes
    collected_at: datetime
    endpoint: str


class OddsPapiClient:
    """HTTP client for the documented OddsPapi v4 REST endpoints.

    Historical calls are fixture-scoped. OddsPapi permits at most three
    bookmaker slugs per call, so ``fetch_historical_odds`` groups a requested
    nine-book whitelist into three deterministic requests.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.oddspapi.io/v4",
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key must not be empty")
        if not base_url.startswith("https://"):
            raise ValueError("base_url must use https://")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def list_sports(self) -> OddsPapiResponse:
        """Fetch the provider sport catalog to validate credentials."""

        return self._get("/sports", {})

    def list_bookmakers(self) -> OddsPapiResponse:
        """Fetch bookmaker metadata for an explicit whitelist review."""

        return self._get("/bookmakers", {})

    def fetch_account(self) -> OddsPapiResponse:
        """Fetch account quota state; callers must never persist the API key field."""

        return self._get("/account", {})

    def list_tournaments(self, *, sport_id: int) -> OddsPapiResponse:
        """Fetch the provider tournament catalog for one sport."""

        if sport_id <= 0:
            raise ValueError("sport_id must be positive")
        return self._get("/tournaments", {"sportId": str(sport_id)})

    def fetch_fixtures(
        self,
        *,
        tournament_id: int,
        status_id: int,
        from_time: datetime,
        to_time: datetime,
    ) -> OddsPapiResponse:
        """Fetch fixtures for one tournament and UTC time window."""

        if tournament_id <= 0:
            raise ValueError("tournament_id must be positive")
        if status_id not in {0, 1, 2, 3}:
            raise ValueError("status_id must be between zero and three")
        _require_utc(from_time, "from_time")
        _require_utc(to_time, "to_time")
        if from_time >= to_time:
            raise ValueError("from_time must be earlier than to_time")
        return self._get(
            "/fixtures",
            {
                "tournamentId": str(tournament_id),
                "statusId": str(status_id),
                "from": _iso_z(from_time),
                "to": _iso_z(to_time),
            },
        )

    def fetch_settlement(self, fixture_id: str) -> OddsPapiResponse:
        """Fetch the final market settlement for one fixture."""

        fixture = _required_parameter(fixture_id, "fixture_id")
        return self._get("/settlements", {"fixtureId": fixture})

    def fetch_historical_odds_group(
        self,
        fixture_id: str,
        *,
        bookmakers: tuple[str, ...],
    ) -> OddsPapiResponse:
        """Fetch historical odds for one provider-supported group of up to three."""

        fixture = _required_parameter(fixture_id, "fixture_id")
        requested = _validated_bookmakers(bookmakers)
        if len(requested) > 3:
            raise ValueError("one historical request supports at most three bookmakers")
        return self._get(
            "/historical-odds",
            {"fixtureId": fixture, "bookmakers": ",".join(requested)},
        )

    def fetch_historical_odds(
        self,
        fixture_id: str,
        *,
        bookmakers: tuple[str, ...],
    ) -> tuple[OddsPapiResponse, ...]:
        """Fetch complete price histories in provider-supported groups of three."""

        fixture = _required_parameter(fixture_id, "fixture_id")
        requested = _validated_bookmakers(bookmakers)
        return tuple(
            self.fetch_historical_odds_group(fixture, bookmakers=group)
            for group in _chunks(requested, 3)
        )

    def _get(self, path: str, parameters: dict[str, str]) -> OddsPapiResponse:
        query = {"apiKey": self._api_key, **parameters}
        raw_bytes = _http_get(
            f"{self._base_url}{path}?{urlencode(query)}",
            timeout_seconds=self._timeout_seconds,
        )
        _require_json(raw_bytes, path)
        return OddsPapiResponse(
            raw_bytes=raw_bytes,
            collected_at=datetime.now(UTC),
            endpoint=path,
        )


def _http_get(url: str, *, timeout_seconds: float) -> bytes:
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "tennis-value/0.1"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return bytes(response.read())
    except HTTPError as error:
        message = f"OddsPapi returned HTTP {error.code}"
        raw_error = bytes(error.read())
        try:
            payload = json.loads(raw_error)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            pass
        else:
            if isinstance(payload, dict):
                detail = payload.get("message") or payload.get("error")
                if isinstance(detail, str) and detail:
                    message = f"{message}: {detail}"
        if error.code == 429:
            retry_after = error.headers.get("Retry-After")
            try:
                retry_after_seconds = (
                    float(retry_after) if retry_after is not None else None
                )
            except ValueError:
                retry_after_seconds = None
            raise OddsPapiRateLimitError(
                message,
                retry_after_seconds=retry_after_seconds,
            ) from error
        if error.code == 404:
            try:
                json.loads(raw_error)
            except (json.JSONDecodeError, UnicodeDecodeError) as parse_error:
                raise OddsPapiError(
                    "OddsPapi returned HTTP 404 with a non-JSON response"
                ) from parse_error
            raise OddsPapiNotFoundError(message, raw_bytes=raw_error) from error
        raise OddsPapiError(message) from error
    except URLError as error:
        raise OddsPapiError(f"Could not connect to OddsPapi: {error.reason}") from error


def _require_json(raw_bytes: bytes, endpoint: str) -> None:
    try:
        json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise OddsPapiError(
            f"OddsPapi {endpoint} response was not valid JSON"
        ) from error


def _required_parameter(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned or any(character in cleaned for character in "/?&#,"):
        raise ValueError(f"{field_name} contains invalid characters")
    return cleaned


def _validated_bookmakers(bookmakers: tuple[str, ...]) -> tuple[str, ...]:
    if not bookmakers:
        raise ValueError("at least one bookmaker is required")
    values = tuple(
        _required_parameter(bookmaker, "bookmaker") for bookmaker in bookmakers
    )
    if len(set(values)) != len(values):
        raise ValueError("bookmakers must not contain duplicates")
    return values


def _chunks(values: tuple[str, ...], size: int) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(values[index : index + size]) for index in range(0, len(values), size)
    )


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must be in UTC")


def _iso_z(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
