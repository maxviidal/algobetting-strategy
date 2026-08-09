import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tennis_value.data.odds_papi import (
    OddsPapiRateLimitError,
    OddsPapiResponse,
)
from tennis_value.oddspapi_research import (
    EndpointRateLimiter,
    TennisResearchCollectionError,
    _paced_fetch,
    fetch_tennis_history,
    parse_account_quota,
    preflight_tennis_history,
)
from tennis_value.research_config import (
    TennisResearchSettings,
    load_tennis_research_settings,
)


def _response(payload: object, endpoint: str) -> OddsPapiResponse:
    return OddsPapiResponse(
        json.dumps(payload).encode(),
        datetime(2026, 8, 9, tzinfo=UTC),
        endpoint,
    )


def _account(*, used: int = 10, limit: int = 100) -> dict[str, object]:
    return {
        "api_key": "must-not-be-preserved",
        "current_subscription_id": 7,
        "subscriptions": [
            {
                "subscription_id": 7,
                "is_active": True,
                "request_limit": limit,
                "request_count": used,
            }
        ],
    }


class AccountOnlyClient:
    def __init__(self, *, used: int = 10, limit: int = 100) -> None:
        self.used = used
        self.limit = limit
        self.account_calls = 0

    def fetch_account(self) -> OddsPapiResponse:
        self.account_calls += 1
        return _response(_account(used=self.used, limit=self.limit), "/account")

    def list_tournaments(self, *, sport_id: int) -> OddsPapiResponse:
        raise AssertionError("cached resume must not fetch tournaments")

    def fetch_fixtures(self, **_: object) -> OddsPapiResponse:
        raise AssertionError("cached resume must not fetch fixtures")

    def fetch_historical_odds_group(self, *_: object, **__: object) -> OddsPapiResponse:
        raise AssertionError("cached resume must not fetch history")


def _settings() -> TennisResearchSettings:
    return load_tennis_research_settings(Path("configs/tennis_calibration_2026.toml"))


def _gzip_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(payload).encode(), mtime=0))


def test_account_quota_parser_sanitizes_to_only_numeric_quota() -> None:
    quota = parse_account_quota(json.dumps(_account()).encode())

    assert quota.request_limit == 100
    assert quota.request_count == 10
    assert quota.remaining == 90
    assert "api_key" not in repr(quota)


def test_preflight_reports_exact_missing_billable_calls_and_requires_confirmation(
    tmp_path: Path,
) -> None:
    client = AccountOnlyClient()
    settings = _settings()

    preflight = preflight_tennis_history(
        client,  # type: ignore[arg-type]
        settings=settings,
        cache_directory=tmp_path,
    )

    assert preflight.missing_billable_requests == 12
    assert preflight.quota_reserve == 5
    assert preflight.status == "READY_TO_DOWNLOAD"
    with pytest.raises(TennisResearchCollectionError, match="exact cost 12"):
        fetch_tennis_history(
            client,  # type: ignore[arg-type]
            settings=settings,
            cache_directory=tmp_path,
            confirm_billable_requests=11,
        )


def test_preflight_blocks_when_five_request_buffer_cannot_be_preserved(
    tmp_path: Path,
) -> None:
    preflight = preflight_tennis_history(
        AccountOnlyClient(used=84),  # type: ignore[arg-type]
        settings=_settings(),
        cache_directory=tmp_path,
    )

    assert preflight.status == "BLOCKED_BY_QUOTA"


def test_endpoint_limiter_and_retry_after_are_monotonic_and_bounded() -> None:
    current = [10.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        current[0] += seconds

    limiter = EndpointRateLimiter(sleep=sleep, clock=lambda: current[0])
    limiter.wait("history", 5.1)
    current[0] += 1
    limiter.wait("history", 5.1)
    assert sleeps == pytest.approx([4.1])

    calls = [0]

    def fetch() -> OddsPapiResponse:
        calls[0] += 1
        if calls[0] < 3:
            raise OddsPapiRateLimitError("limited", retry_after_seconds=7)
        return _response({}, "/historical-odds")

    result = _paced_fetch(
        endpoint="history-retry",
        cooldown_seconds=5.1,
        limiter=limiter,
        fetch=fetch,
        sleep=sleep,
    )
    assert result.endpoint == "/historical-odds"
    assert calls[0] == 3
    assert sum(value >= 7 for value in sleeps) == 2


def test_compressed_cache_integrity_is_checked_during_preflight(
    tmp_path: Path,
) -> None:
    (tmp_path / "tournaments.json.gz").write_bytes(b"not-gzip")

    with pytest.raises(TennisResearchCollectionError, match="invalid gzip cache"):
        preflight_tennis_history(
            AccountOnlyClient(),  # type: ignore[arg-type]
            settings=_settings(),
            cache_directory=tmp_path,
        )


def test_complete_cached_collection_resumes_without_provider_requests(
    tmp_path: Path,
) -> None:
    settings = _settings()
    catalog = [
        {
            "categorySlug": value.category_slug,
            "tournamentName": " ".join(
                (
                    *value.name_tokens,
                    "Men Singles" if value.tour == "ATP" else "Women Singles",
                )
            ),
            "tournamentId": index,
        }
        for index, value in enumerate(settings.tournaments, start=1)
    ]
    _gzip_json(tmp_path / "tournaments.json.gz", catalog)
    for value in settings.tournaments:
        _gzip_json(tmp_path / "fixtures" / f"{value.key}.json.gz", [])
    client = AccountOnlyClient()

    collection = fetch_tennis_history(
        client,  # type: ignore[arg-type]
        settings=settings,
        cache_directory=tmp_path,
        confirm_billable_requests=0,
        sleep=lambda _: None,
    )

    assert collection.fixtures == ()
    assert collection.billable_requests_made == 0
    assert collection.historical_requests_made == 0
    manifest = json.loads(collection.manifest_path.read_text())
    assert len(manifest["cache_inventory"]) == 12
    assert client.account_calls == 1
