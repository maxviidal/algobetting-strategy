"""Minimal HTTP clients for the NBA schedule/results and historical odds."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class BasketballProviderError(RuntimeError):
    """Provider request or response failure."""


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    raw_bytes: bytes
    payload: object


class BallDontLieClient:
    """BALLDONTLIE games client with cursor pagination."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.balldontlie.io/v1",
        timeout: float = 30,
    ) -> None:
        if not api_key:
            raise ValueError("BALLDONTLIE_API_KEY is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def fetch_games_page(
        self, season_start_year: int, *, cursor: int | None = None
    ) -> ProviderResponse:
        parameters: list[tuple[str, str]] = [
            ("seasons[]", str(season_start_year)),
            ("postseason", "false"),
            ("per_page", "100"),
        ]
        if cursor is not None:
            parameters.append(("cursor", str(cursor)))
        request = Request(
            f"{self._base_url}/games?{urlencode(parameters)}",
            headers={"Authorization": self._api_key, "Accept": "application/json"},
        )
        return _open(request, self._timeout)


class TheOddsApiHistoricalClient:
    """The Odds API historical featured-market client."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.the-odds-api.com/v4",
        timeout: float = 30,
    ) -> None:
        if not api_key:
            raise ValueError("ODDS_API_KEY is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def fetch_snapshot(
        self,
        *,
        sport_key: str,
        query_at: datetime,
        region: str,
        market_key: str,
    ) -> ProviderResponse:
        parameters = urlencode(
            {
                "apiKey": self._api_key,
                "regions": region,
                "markets": market_key,
                "oddsFormat": "decimal",
                "dateFormat": "iso",
                "date": query_at.isoformat().replace("+00:00", "Z"),
            }
        )
        request = Request(
            f"{self._base_url}/historical/sports/{sport_key}/odds?{parameters}",
            headers={"Accept": "application/json"},
        )
        return _open(request, self._timeout)


def _open(request: Request, timeout: float) -> ProviderResponse:
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
    except HTTPError as error:
        raise BasketballProviderError(
            f"provider returned HTTP {error.code}"
        ) from error
    except (URLError, OSError) as error:
        raise BasketballProviderError("provider network request failed") from error
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as error:
        raise BasketballProviderError("provider returned invalid JSON") from error
    if isinstance(payload, dict) and payload.get("message"):
        raise BasketballProviderError(str(payload["message"]))
    return ProviderResponse(raw_bytes=raw, payload=payload)
