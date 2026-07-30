from urllib.parse import parse_qs, urlparse

import pytest
import tennis_value.odds_papi as odds_papi_module
from tennis_value.odds_papi import OddsPapiClient, OddsPapiError


def test_historical_requests_nine_books_in_three_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls: list[str] = []

    def fake_http_get(url: str, *, timeout_seconds: float) -> bytes:
        assert timeout_seconds == 12
        urls.append(url)
        return b'{"bookmakers": {}}'

    monkeypatch.setattr(odds_papi_module, "_http_get", fake_http_get)
    responses = OddsPapiClient("secret", timeout_seconds=12).fetch_historical_odds(
        "fixture-1",
        bookmakers=(
            "book-1", "book-2", "book-3", "book-4", "book-5", "book-6",
            "book-7", "book-8", "book-9",
        ),
    )

    assert len(responses) == 3
    assert [
        parse_qs(urlparse(url).query)["bookmakers"][0] for url in urls
    ] == ["book-1,book-2,book-3", "book-4,book-5,book-6", "book-7,book-8,book-9"]
    assert all("secret" not in response.raw_bytes.decode() for response in responses)


def test_client_rejects_invalid_historical_inputs() -> None:
    client = OddsPapiClient("secret")
    with pytest.raises(ValueError, match="at least one"):
        client.fetch_historical_odds("fixture-1", bookmakers=())
    with pytest.raises(ValueError, match="duplicates"):
        client.fetch_historical_odds(
            "fixture-1", bookmakers=("book-1", "book-1")
        )


def test_client_rejects_non_json_provider_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        odds_papi_module,
        "_http_get",
        lambda url, timeout_seconds: b"not-json",
    )

    with pytest.raises(OddsPapiError, match="not valid JSON"):
        OddsPapiClient("secret").list_bookmakers()
