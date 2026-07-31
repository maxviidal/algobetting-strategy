from urllib.parse import parse_qs, urlparse

import pytest

import tennis_value.data.odds_api as odds_api_module
from tennis_value.data.odds_api import OddsApiClient, OddsApiError


def test_fetch_current_odds_builds_decimal_h2h_request_and_hides_key_in_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_url = ""

    def fake_http_get(
        url: str,
        *,
        timeout_seconds: float,
    ) -> tuple[bytes, dict[str, str]]:
        nonlocal captured_url
        captured_url = url
        assert timeout_seconds == 12.0
        return (
            b"[]",
            {
                "x-requests-remaining": "499",
                "x-requests-used": "1",
                "x-requests-last": "1",
            },
        )

    monkeypatch.setattr(odds_api_module, "_http_get", fake_http_get)
    client = OddsApiClient("secret-key", timeout_seconds=12)

    result = client.fetch_current_odds(
        "tennis_atp_wimbledon",
        regions=("uk",),
    )

    parsed = urlparse(captured_url)
    parameters = parse_qs(parsed.query)
    assert parsed.path == "/v4/sports/tennis_atp_wimbledon/odds"
    assert parameters == {
        "apiKey": ["secret-key"],
        "regions": ["uk"],
        "markets": ["h2h"],
        "oddsFormat": ["decimal"],
        "dateFormat": ["iso"],
    }
    assert result.response.raw_bytes == b"[]"
    assert result.response.source == (
        "the-odds-api:v4:current:tennis_atp_wimbledon"
    )
    assert "secret-key" not in result.response.source
    assert result.quota.remaining == 499
    assert result.quota.used == 1
    assert result.quota.last_request_cost == 1


def test_list_sports_returns_typed_deterministic_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_http_get(
        url: str,
        *,
        timeout_seconds: float,
    ) -> tuple[bytes, dict[str, str]]:
        assert "apiKey=secret-key" in url
        assert timeout_seconds == 30.0
        return (
            b"""
[
  {
    "key": "tennis_wta_wimbledon",
    "group": "Tennis",
    "title": "WTA Wimbledon",
    "active": true
  },
  {
    "key": "tennis_atp_wimbledon",
    "group": "Tennis",
    "title": "ATP Wimbledon",
    "active": true
  }
]
""",
            {},
        )

    monkeypatch.setattr(odds_api_module, "_http_get", fake_http_get)

    sports = OddsApiClient("secret-key").list_sports()

    assert [sport.key for sport in sports] == [
        "tennis_atp_wimbledon",
        "tennis_wta_wimbledon",
    ]
    assert all(sport.active for sport in sports)


def test_fetch_rejects_invalid_quota_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_http_get(
        url: str,
        *,
        timeout_seconds: float,
    ) -> tuple[bytes, dict[str, str]]:
        return b"[]", {"x-requests-remaining": "not-an-integer"}

    monkeypatch.setattr(odds_api_module, "_http_get", fake_http_get)

    with pytest.raises(OddsApiError, match="x-requests-remaining"):
        OddsApiClient("secret-key").fetch_current_odds(
            "tennis_atp_wimbledon"
        )


@pytest.mark.parametrize(
    "sport",
    ["", "tennis/atp", "tennis?key", "tennis,atp"],
)
def test_fetch_rejects_invalid_sport_key(sport: str) -> None:
    with pytest.raises(ValueError, match="sport"):
        OddsApiClient("secret-key").fetch_current_odds(sport)
