import json
from pathlib import Path

import pytest

from basketball_value.config import load_basketball_settings
from basketball_value.providers import BasketballRateLimitError, ProviderResponse
from basketball_value.workflow import fetch_nba_history


class FakeResultsClient:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_games_page(
        self, season_start_year: int, *, cursor: int | None = None
    ) -> ProviderResponse:
        self.calls += 1
        payload = {
            "data": [
                {
                    "id": season_start_year,
                    "datetime": f"{season_start_year + 1}-01-02T03:00:00Z",
                    "season": season_start_year,
                    "status": "Final",
                    "postseason": False,
                    "postponed": False,
                    "home_team_score": 101,
                    "visitor_team_score": 99,
                    "home_team": {"full_name": "Los Angeles Lakers"},
                    "visitor_team": {"full_name": "Boston Celtics"},
                }
            ],
            "meta": {"next_cursor": None},
        }
        raw = json.dumps(payload).encode()
        return ProviderResponse(raw, payload)


class FakeOddsClient:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_snapshot(self, **_: object) -> ProviderResponse:
        self.calls += 1
        payload = {"timestamp": "2026-01-02T02:00:00Z", "data": []}
        raw = json.dumps(payload).encode()
        return ProviderResponse(raw, payload)


class RateLimitedResultsClient(FakeResultsClient):
    def __init__(self) -> None:
        super().__init__()
        self.rate_limited = False

    def fetch_games_page(
        self, season_start_year: int, *, cursor: int | None = None
    ) -> ProviderResponse:
        if not self.rate_limited:
            self.rate_limited = True
            raise BasketballRateLimitError(7)
        return super().fetch_games_page(season_start_year, cursor=cursor)


def test_manifest_requires_exact_approval_and_cached_rerun_spends_nothing(
    tmp_path: Path,
) -> None:
    settings = load_basketball_settings(
        Path("configs/basketball_research.toml")
    )
    results = FakeResultsClient()

    manifest = fetch_nba_history(
        settings=settings,
        cache_directory=tmp_path,
        results_client=results,  # type: ignore[arg-type]
        odds_client=None,
        confirmed_credits=None,
        sleep=lambda _: None,
        results_request_interval_seconds=0,
    )

    assert manifest.distinct_timestamps == 10
    assert manifest.expected_credits == 100
    assert results.calls == 5

    odds = FakeOddsClient()
    with pytest.raises(ValueError, match="approval must equal"):
        fetch_nba_history(
            settings=settings,
            cache_directory=tmp_path,
            results_client=results,  # type: ignore[arg-type]
            odds_client=odds,  # type: ignore[arg-type]
            confirmed_credits=99,
            sleep=lambda _: None,
            results_request_interval_seconds=0,
        )
    assert odds.calls == 0

    completed = fetch_nba_history(
        settings=settings,
        cache_directory=tmp_path,
        results_client=results,  # type: ignore[arg-type]
        odds_client=odds,  # type: ignore[arg-type]
        confirmed_credits=100,
        sleep=lambda _: None,
        results_request_interval_seconds=0,
    )

    assert completed.missing_requests == 0
    assert odds.calls == 10
    assert results.calls == 5

    resumed_odds = FakeOddsClient()
    resumed = fetch_nba_history(
        settings=settings,
        cache_directory=tmp_path,
        results_client=results,  # type: ignore[arg-type]
        odds_client=resumed_odds,  # type: ignore[arg-type]
        confirmed_credits=100,
        sleep=lambda _: None,
        results_request_interval_seconds=0,
    )

    assert resumed.missing_requests == 0
    assert resumed_odds.calls == 0


def test_backtest_schedule_collection_waits_and_retries_rate_limit(
    tmp_path: Path,
) -> None:
    settings = load_basketball_settings(
        Path("configs/basketball_research.toml")
    )
    results = RateLimitedResultsClient()
    waits: list[float] = []

    manifest = fetch_nba_history(
        settings=settings,
        cache_directory=tmp_path,
        results_client=results,  # type: ignore[arg-type]
        odds_client=None,
        confirmed_credits=None,
        sleep=waits.append,
        results_request_interval_seconds=0,
    )

    assert manifest.distinct_timestamps == 10
    assert waits == [7]
    assert results.calls == 5
