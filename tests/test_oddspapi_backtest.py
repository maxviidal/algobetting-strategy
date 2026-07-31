import json
from csv import DictReader
from datetime import UTC, datetime
from pathlib import Path

from tennis_value.config import AppSettings, CollectionSettings
from tennis_value.data.odds_papi import OddsPapiResponse
from tennis_value.oddspapi_backtest import (
    WIMBLEDON_BOOKMAKERS,
    export_atp_wimbledon_csv,
    run_atp_wimbledon_backtest,
)


class FakeOddsPapiClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def fetch_fixtures(
        self,
        *,
        tournament_id: int,
        status_id: int,
        from_time: datetime,
        to_time: datetime,
    ) -> OddsPapiResponse:
        self.calls.append(("fixtures", tournament_id))
        return _response(
            [
                {
                    "fixtureId": "fixture-1",
                    "tournamentId": 2555,
                    "participant1Id": 1,
                    "participant2Id": 2,
                    "participant1Name": "Player One",
                    "participant2Name": "Player Two",
                    "startTime": "2026-07-01T12:00:00Z",
                    "trueEndTime": "2026-07-01T14:00:00Z",
                }
            ],
            "/fixtures",
        )

    def fetch_settlement(self, fixture_id: str) -> OddsPapiResponse:
        self.calls.append(("settlement", fixture_id))
        return _response(
            {
                "fixtureId": fixture_id,
                "markets": {
                    "121": {
                        "outcomes": {
                            "121": {"players": {"0": {"result": "WIN"}}},
                            "122": {"players": {"0": {"result": "LOSE"}}},
                        }
                    }
                },
            },
            "/settlements",
        )

    def fetch_historical_odds_group(
        self,
        fixture_id: str,
        *,
        bookmakers: tuple[str, ...],
    ) -> OddsPapiResponse:
        self.calls.append(("historical", bookmakers))
        books = {}
        for bookmaker in bookmakers:
            first_odds, second_odds = (
                ("3.0", "1.5") if bookmaker == "pinnacle" else ("2.0", "2.0")
            )
            books[bookmaker] = {
                "markets": {
                    "121": {
                        "outcomes": {
                            "121": {
                                "players": {
                                    "0": [
                                        {
                                            "createdAt": "2026-07-01T10:59:00Z",
                                            "price": first_odds,
                                            "active": True,
                                        }
                                    ]
                                }
                            },
                            "122": {
                                "players": {
                                    "0": [
                                        {
                                            "createdAt": "2026-07-01T10:59:00Z",
                                            "price": second_odds,
                                            "active": True,
                                        }
                                    ]
                                }
                            },
                        }
                    }
                }
            }
        return _response(
            {"fixtureId": fixture_id, "bookmakers": books},
            "/historical-odds",
        )


def test_complete_atp_wimbledon_run_and_cache_resume(tmp_path: Path) -> None:
    client = FakeOddsPapiClient()
    settings = AppSettings(
        collection=CollectionSettings(
            minimum_bookmakers=5,
            maximum_quote_age_seconds=180,
        )
    )

    run = run_atp_wimbledon_backtest(
        client,  # type: ignore[arg-type]
        model_settings=settings,
        cache_directory=tmp_path,
        sleep=lambda _: None,
    )

    assert run.fixture_count == 1
    assert run.evaluated_matches == 1
    assert run.skipped_matches == 0
    assert run.offer_evaluations == 18
    assert run.report.selected_candidates == 1
    assert run.report.wins == 1
    assert run.report.final_equity == 11250
    assert [kind for kind, _ in client.calls] == [
        "fixtures",
        "settlement",
        "historical",
        "historical",
        "historical",
    ]
    assert len(WIMBLEDON_BOOKMAKERS) == 9

    cached_client = FakeOddsPapiClient()
    cached_run = run_atp_wimbledon_backtest(
        cached_client,  # type: ignore[arg-type]
        model_settings=settings,
        cache_directory=tmp_path,
        sleep=lambda _: None,
    )

    assert cached_run.report.final_equity == 11250
    assert cached_client.calls == []


def test_export_writes_one_match_row_and_every_offer(tmp_path: Path) -> None:
    client = FakeOddsPapiClient()
    settings = AppSettings(
        collection=CollectionSettings(maximum_quote_age_seconds=180)
    )
    run_atp_wimbledon_backtest(
        client,  # type: ignore[arg-type]
        model_settings=settings,
        cache_directory=tmp_path / "cache",
        sleep=lambda _: None,
    )

    export = export_atp_wimbledon_csv(
        model_settings=settings,
        cache_directory=tmp_path / "cache",
        output_directory=tmp_path / "reports",
    )

    assert export.match_rows == 1
    assert export.offer_rows == 18
    with export.matches_path.open(newline="", encoding="utf-8") as file:
        matches = list(DictReader(file))
    with export.offers_path.open(newline="", encoding="utf-8") as file:
        offers = list(DictReader(file))
    assert len(matches) == 1
    assert matches[0]["selected_player_name"] == "Player One"
    assert matches[0]["settlement"] == "win"
    assert len(offers) == 18
    assert sum(row["selected_for_kelly"] == "True" for row in offers) == 1
    assert {row["player_name"] for row in offers} == {"Player One", "Player Two"}


def test_void_settlement_does_not_change_equity(tmp_path: Path) -> None:
    client = FakeOddsPapiClient()
    original = client.fetch_settlement

    def void_settlement(fixture_id: str) -> OddsPapiResponse:
        response = original(fixture_id)
        payload = json.loads(response.raw_bytes)
        payload["markets"]["121"]["outcomes"]["121"]["players"]["0"]["result"] = (
            "UNDECIDED"
        )
        payload["markets"]["121"]["outcomes"]["122"]["players"]["0"]["result"] = (
            "UNDECIDED"
        )
        return _response(payload, "/settlements")

    client.fetch_settlement = void_settlement  # type: ignore[method-assign]
    run = run_atp_wimbledon_backtest(
        client,  # type: ignore[arg-type]
        model_settings=AppSettings(
            collection=CollectionSettings(maximum_quote_age_seconds=180)
        ),
        cache_directory=tmp_path,
        sleep=lambda _: None,
    )

    assert run.report.void_bets == 1
    assert run.report.final_equity == 10000


def _response(payload: object, endpoint: str) -> OddsPapiResponse:
    return OddsPapiResponse(
        raw_bytes=json.dumps(payload).encode(),
        collected_at=datetime(2026, 7, 30, tzinfo=UTC),
        endpoint=endpoint,
    )
