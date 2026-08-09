"""Small, secret-safe HTTP client for The Odds API v4."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tennis_value.data.ingestion import IngestedOddsApiResponse, ingest_odds_api_json

_DEFAULT_BASE_URL = "https://api.the-odds-api.com/v4"


class OddsApiError(RuntimeError):
    """Raised when The Odds API cannot return a usable response."""


@dataclass(frozen=True, slots=True)
class OddsApiSport:
    """One sport or tournament key advertised by The Odds API."""

    key: str
    group: str
    title: str
    active: bool


@dataclass(frozen=True, slots=True)
class OddsApiQuota:
    """Quota headers returned by The Odds API."""

    remaining: int | None
    used: int | None
    last_request_cost: int | None


@dataclass(frozen=True, slots=True)
class CurrentOddsResult:
    """An ingested current-odds response and its quota information."""

    response: IngestedOddsApiResponse
    quota: OddsApiQuota


class OddsApiClient:
    """Fetch current odds without exposing the API key in stored provenance."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def list_sports(self) -> tuple[OddsApiSport, ...]:
        """Return active and inactive sport keys; this endpoint is quota-free."""

        url = self._url("/sports", {"apiKey": self._api_key})
        body, _ = _http_get(url, timeout_seconds=self._timeout_seconds)
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            message = "The Odds API sports response was not valid JSON"
            raise OddsApiError(message) from error
        if not isinstance(payload, list):
            raise OddsApiError("The Odds API sports response must be a JSON array")

        sports: list[OddsApiSport] = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                raise OddsApiError(f"sports[{index}] must be a JSON object")
            sports.append(
                OddsApiSport(
                    key=_required_string(item, "key", f"sports[{index}]"),
                    group=_required_string(item, "group", f"sports[{index}]"),
                    title=_required_string(item, "title", f"sports[{index}]"),
                    active=_required_bool(item, "active", f"sports[{index}]"),
                )
            )
        return tuple(sorted(sports, key=lambda sport: sport.key))

    def fetch_current_odds(
        self,
        sport: str,
        *,
        regions: tuple[str, ...] = ("uk",),
        market: str = "h2h",
    ) -> CurrentOddsResult:
        """Fetch and minimally validate one current featured-market response."""

        sport_key = _required_parameter(sport, "sport")
        region_values = tuple(
            _required_parameter(region, "region") for region in regions
        )
        if not region_values:
            raise ValueError("at least one region is required")
        market_key = _required_parameter(market, "market")
        url = self._url(
            f"/sports/{sport_key}/odds",
            {
                "apiKey": self._api_key,
                "regions": ",".join(region_values),
                "markets": market_key,
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
        )
        raw_bytes, headers = _http_get(
            url,
            timeout_seconds=self._timeout_seconds,
        )
        collected_at = datetime.now(UTC)
        response = ingest_odds_api_json(
            raw_bytes,
            collected_at=collected_at,
            source=f"the-odds-api:v4:current:{sport_key}",
        )
        return CurrentOddsResult(
            response=response,
            quota=OddsApiQuota(
                remaining=_optional_header_int(headers, "x-requests-remaining"),
                used=_optional_header_int(headers, "x-requests-used"),
                last_request_cost=_optional_header_int(
                    headers,
                    "x-requests-last",
                ),
            ),
        )

    def _url(self, path: str, parameters: dict[str, str]) -> str:
        return f"{self._base_url}{path}?{urlencode(parameters)}"


def _http_get(
    url: str,
    *,
    timeout_seconds: float,
) -> tuple[bytes, dict[str, str]]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "tennis-value/0.1",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
            headers = {
                name.casefold(): value for name, value in response.headers.items()
            }
    except HTTPError as error:
        message = _http_error_message(error)
        raise OddsApiError(message) from error
    except URLError as error:
        raise OddsApiError(
            f"Could not connect to The Odds API: {error.reason}"
        ) from error
    return body, headers


def _http_error_message(error: HTTPError) -> str:
    message = f"The Odds API returned HTTP {error.code}"
    try:
        payload = json.loads(error.read())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return message
    if isinstance(payload, dict):
        provider_message = payload.get("message")
        error_code = payload.get("error_code")
        details = [
            value
            for value in (error_code, provider_message)
            if isinstance(value, str) and value
        ]
        if details:
            return f"{message}: {' - '.join(details)}"
    return message


def _required_parameter(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned or any(character in cleaned for character in "/?&#,"):
        raise ValueError(f"{field_name} contains invalid characters")
    return cleaned


def _required_string(
    value: dict[object, object],
    key: str,
    context: str,
) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise OddsApiError(f"{context}.{key} must be a non-empty string")
    return item


def _required_bool(
    value: dict[object, object],
    key: str,
    context: str,
) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise OddsApiError(f"{context}.{key} must be a boolean")
    return item


def _optional_header_int(headers: dict[str, str], name: str) -> int | None:
    raw_value = headers.get(name)
    if raw_value is None:
        return None
    try:
        return int(raw_value)
    except ValueError as error:
        raise OddsApiError(f"The Odds API returned an invalid {name} header") from error
