"""Small client for validating and retrieving OddsPapi historical fixtures."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class OddsPapiError(RuntimeError):
    """Raised when OddsPapi cannot return a valid response."""


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
            self._get(
                "/historical-odds",
                {"fixtureId": fixture, "bookmakers": ",".join(group)},
            )
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
        try:
            payload = json.loads(error.read())
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            pass
        else:
            if isinstance(payload, dict):
                detail = payload.get("message") or payload.get("error")
                if isinstance(detail, str) and detail:
                    message = f"{message}: {detail}"
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
        tuple(values[index : index + size])
        for index in range(0, len(values), size)
    )
