from datetime import UTC, datetime, timedelta

import pytest

from basketball_value.domain import Game, stable_game_id
from basketball_value.normalization import (
    balldontlie_quarantines,
    match_odds_event,
    normalize_balldontlie_games,
    normalize_odds_snapshot,
)
from basketball_value.teams import NBA_TEAMS, resolve_provider_team, resolve_team


def _game(event_id: str = "result-1") -> Game:
    return Game(
        game_id=stable_game_id("balldontlie", event_id),
        source="balldontlie",
        source_event_id=event_id,
        season="2025-26",
        home_team_id="LAL",
        away_team_id="BOS",
        scheduled_start=datetime(2026, 1, 2, 3, tzinfo=UTC),
    )


def test_catalog_has_30_teams_and_resolves_provider_aliases() -> None:
    assert len(NBA_TEAMS) == 30
    assert resolve_team("LA Lakers").team_id == "LAL"
    assert resolve_team("Portland Trailblazers").team_id == "POR"
    assert resolve_provider_team("balldontlie", "14").team_id == "LAL"


def test_game_identity_does_not_change_when_tip_changes() -> None:
    original = _game()
    rescheduled = Game(
        game_id=original.game_id,
        source=original.source,
        source_event_id=original.source_event_id,
        season=original.season,
        home_team_id=original.home_team_id,
        away_team_id=original.away_team_id,
        scheduled_start=original.scheduled_start + timedelta(days=2),
    )

    assert rescheduled.game_id == original.game_id


def test_overtime_final_settles_and_postponement_does_not() -> None:
    payload = {
        "data": [
            {
                "id": 1,
                "datetime": "2026-01-02T03:00:00Z",
                "season": 2025,
                "status": "Final",
                "postseason": False,
                "postponed": False,
                "home_team_score": 121,
                "visitor_team_score": 119,
                "home_team": {"full_name": "Los Angeles Lakers"},
                "visitor_team": {"full_name": "Boston Celtics"},
            },
            {
                "id": 2,
                "datetime": "2026-01-03T03:00:00Z",
                "season": 2025,
                "status": "Postponed",
                "postseason": False,
                "postponed": True,
                "home_team_score": 0,
                "visitor_team_score": 0,
                "home_team": {"full_name": "Los Angeles Lakers"},
                "visitor_team": {"full_name": "Boston Celtics"},
            },
        ]
    }

    games = normalize_balldontlie_games((payload,))

    assert games[0].result.winner_team_side == "home"
    assert games[1].result.winner_team_side is None


def test_result_without_exact_tip_time_is_quarantined_not_guessed() -> None:
    payload = {
        "data": [
            {
                "id": 857680,
                "date": "2022-12-02",
                "datetime": None,
                "season": 2022,
                "status": "Final",
                "postseason": False,
                "home_team": {"full_name": "Boston Celtics"},
                "visitor_team": {"full_name": "Miami Heat"},
            }
        ]
    }

    assert normalize_balldontlie_games((payload,)) == ()
    assert balldontlie_quarantines((payload,)) == (
        "857680:missing_scheduled_datetime",
    )


def test_conflicting_cross_provider_match_is_quarantined() -> None:
    game = _game("a")
    duplicate = _game("b")
    event = {
        "id": "odds-1",
        "home_team": "LA Lakers",
        "away_team": "Boston Celtics",
        "commence_time": "2026-01-02T03:00:00Z",
    }

    with pytest.raises(ValueError, match="matched 2 results"):
        match_odds_event(event, (game, duplicate))


def test_incomplete_suspended_and_exchange_markets_are_observable_exclusions() -> None:
    game = _game()
    decision = datetime(2026, 1, 2, 2, tzinfo=UTC)
    payload = {
        "timestamp": decision.isoformat(),
        "data": [
            {
                "id": "odds-1",
                "home_team": "Los Angeles Lakers",
                "away_team": "Boston Celtics",
                "commence_time": "2026-01-02T03:00:00Z",
                "bookmakers": [
                    {
                        "key": "complete",
                        "markets": [
                            {
                                "key": "h2h",
                                "last_update": "2026-01-02T01:59:00Z",
                                "outcomes": [
                                    {"name": "LA Lakers", "price": 2.1},
                                    {"name": "Boston Celtics", "price": 1.8},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "incomplete",
                        "markets": [
                            {
                                "key": "h2h",
                                "last_update": "2026-01-02T01:59:00Z",
                                "outcomes": [
                                    {"name": "LA Lakers", "price": 2.1},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "suspended",
                        "markets": [
                            {
                                "key": "h2h",
                                "last_update": "2026-01-02T01:59:00Z",
                                "outcomes": [
                                    {
                                        "name": "LA Lakers",
                                        "price": 2.1,
                                        "active": False,
                                    },
                                    {"name": "Boston Celtics", "price": 1.8},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "betfair_ex_eu",
                        "markets": [
                            {"key": "h2h", "outcomes": []},
                            {"key": "h2h_lay", "outcomes": []},
                        ],
                    },
                    {
                        "key": "stale",
                        "markets": [
                            {
                                "key": "h2h",
                                "last_update": "2026-01-02T01:20:00Z",
                                "outcomes": [
                                    {"name": "LA Lakers", "price": 2.1},
                                    {"name": "Boston Celtics", "price": 1.8},
                                ],
                            }
                        ],
                    },
                ],
            }
        ],
    }

    result = normalize_odds_snapshot(
        payload,
        (game,),
        decision_at=decision,
        maximum_age=timedelta(minutes=30),
    )

    assert [value.bookmaker_id for value in result.snapshots] == ["complete"]
    assert len(result.excluded) == 4
    assert any(value.endswith(":suspended") for value in result.excluded)
    assert any(value.endswith(":stale") for value in result.excluded)
    assert any(value.endswith(":incomplete") for value in result.excluded)
    assert any(value.endswith(":exchange") for value in result.excluded)
