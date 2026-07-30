import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from tennis_value.domain import Bookmaker, Player, Tournament
from tennis_value.entity_resolution import (
    THE_ODDS_API_PROVIDER,
    AmbiguousEntityError,
    InMemoryPlayerResolver,
    PlayerAlias,
    UnknownEntityError,
    normalized_name,
)
from tennis_value.ingestion import (
    IngestedOddsApiResponse,
    ingest_odds_api_json,
    load_odds_api_json,
)
from tennis_value.normalization import InvalidMarketError, OddsApiNormalizer
from tennis_value.storage import SqlitePlayerRegistry


def make_normalizer() -> OddsApiNormalizer:
    return OddsApiNormalizer(
        player_resolver=InMemoryPlayerResolver(
            players=(
                Player(1001, "Jannik Sinner"),
                Player(1002, "Carlos Alcaraz"),
            ),
            aliases=(
                PlayerAlias(THE_ODDS_API_PROVIDER, "J. Sinner", 1001),
            ),
        ),
        tournaments=(Tournament("tournament-wimbledon", "ATP Wimbledon"),),
        bookmakers=(Bookmaker("bookmaker-pinnacle", "Pinnacle"),),
        tournament_aliases={
            "tennis_atp_wimbledon": "tournament-wimbledon",
        },
        bookmaker_aliases={"pinnacle": "bookmaker-pinnacle"},
    )


def make_response(
    *,
    home_team: str = "J. Sinner",
    away_team: str = "Carlos Alcaraz",
    outcomes: list[dict[str, object]] | None = None,
    commence_time: str = "2026-07-12T15:00:00+02:00",
) -> IngestedOddsApiResponse:
    event = {
        "id": "event-123",
        "sport_key": "tennis_atp_wimbledon",
        "sport_title": "ATP Wimbledon",
        "commence_time": commence_time,
        "home_team": home_team,
        "away_team": away_team,
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": "2026-07-12T12:29:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-07-12T12:30:00Z",
                        "outcomes": outcomes
                        or [
                            {"name": "Carlos Alcaraz", "price": 2.1},
                            {"name": "Jannik Sinner", "price": 1.8},
                        ],
                    }
                ],
            }
        ],
    }
    return ingest_odds_api_json(
        json.dumps([event]).encode(),
        collected_at=datetime(2026, 7, 12, 12, 31, tzinfo=UTC),
        source="test-fixture",
    )


def test_normalized_name_preserves_accents_and_normalizes_whitespace() -> None:
    assert normalized_name("  FÉLIX   Auger-Aliassime ") == "félix auger-aliassime"


def test_normalize_response_resolves_entities_and_preserves_provenance() -> None:
    response = make_response()

    normalized = make_normalizer().normalize(response)

    assert len(normalized) == 1
    event = normalized[0]
    assert event.match.player_ids == (1001, 1002)
    assert event.match.scheduled_start == datetime(2026, 7, 12, 13, tzinfo=UTC)
    assert len(event.snapshots) == 1
    snapshot = event.snapshots[0]
    assert snapshot.bookmaker_id == "bookmaker-pinnacle"
    assert snapshot.observed_at == datetime(2026, 7, 12, 12, 30, tzinfo=UTC)
    assert snapshot.source == "test-fixture"
    assert snapshot.source_event_id == "event-123"
    assert {price.player_id for price in snapshot.prices} == {
        1001,
        1002,
    }


def test_normalizer_accepts_persistent_sqlite_player_registry() -> None:
    registry = SqlitePlayerRegistry(sqlite3.connect(":memory:"))
    sinner = registry.add_player("Jannik Sinner")
    alcaraz = registry.add_player("Carlos Alcaraz")
    registry.add_alias(
        provider=THE_ODDS_API_PROVIDER,
        raw_name="J. Sinner",
        player_id=sinner.player_id,
    )
    normalizer = OddsApiNormalizer(
        player_resolver=registry,
        tournaments=(Tournament("tournament-wimbledon", "ATP Wimbledon"),),
        bookmakers=(Bookmaker("bookmaker-pinnacle", "Pinnacle"),),
        tournament_aliases={
            "tennis_atp_wimbledon": "tournament-wimbledon",
        },
        bookmaker_aliases={"pinnacle": "bookmaker-pinnacle"},
    )

    event = normalizer.normalize(make_response())[0]

    assert event.match.player_ids == (sinner.player_id, alcaraz.player_id)


def test_normalization_generates_stable_match_and_snapshot_ids() -> None:
    normalizer = make_normalizer()

    first = normalizer.normalize(make_response())
    second = normalizer.normalize(make_response())

    assert first[0].match.match_id == second[0].match.match_id
    assert first[0].snapshots[0].snapshot_id == second[0].snapshots[0].snapshot_id


def test_realistic_french_open_response_extracts_only_h2h_snapshots() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "odds_api_french_open.json"
    response = load_odds_api_json(
        fixture_path,
        collected_at=datetime(2025, 5, 31, 3, 23, tzinfo=UTC),
    )
    normalizer = OddsApiNormalizer(
        player_resolver=InMemoryPlayerResolver(
            players=(
                Player(3001, "Tallon Griekspoor"),
                Player(3002, "Ethan Quinn"),
            )
        ),
        tournaments=(Tournament("french-open-atp", "ATP French Open"),),
        bookmakers=(
            Bookmaker("grosvenor-uk", "Grosvenor"),
            Bookmaker("betway-uk", "Betway"),
        ),
        tournament_aliases={
            "tennis_atp_french_open": "french-open-atp",
        },
        bookmaker_aliases={
            "grosvenor": "grosvenor-uk",
            "betway": "betway-uk",
        },
    )

    event = normalizer.normalize(response)[0]

    assert event.match.player_ids == (3001, 3002)
    assert event.match.scheduled_start == datetime(2025, 5, 31, 9, tzinfo=UTC)
    assert len(event.snapshots) == 2
    assert [snapshot.bookmaker_id for snapshot in event.snapshots] == [
        "grosvenor-uk",
        "betway-uk",
    ]
    assert {
        price.player_id: price.decimal_odds
        for price in event.snapshots[0].prices
    } == {
        3001: Decimal("1.21"),
        3002: Decimal("4.6"),
    }
    assert {
        price.player_id: price.decimal_odds
        for price in event.snapshots[1].prices
    } == {
        3001: Decimal("1.15"),
        3002: Decimal("5.25"),
    }


def test_unknown_player_is_not_silently_merged() -> None:
    with pytest.raises(UnknownEntityError, match="Unknown Player"):
        make_normalizer().normalize(make_response(home_team="Unknown Player"))


def test_unknown_outcome_player_keeps_entity_resolution_error() -> None:
    outcomes = [
        {"name": "Unknown Player", "price": 1.8},
        {"name": "Carlos Alcaraz", "price": 2.1},
    ]

    with pytest.raises(UnknownEntityError, match="Unknown Player"):
        make_normalizer().normalize(make_response(outcomes=outcomes))


def test_alias_collision_is_rejected_as_ambiguous() -> None:
    resolver = InMemoryPlayerResolver(
        players=(
            Player(1, "First Player"),
            Player(2, "Second Player"),
        ),
        aliases=(
            PlayerAlias(THE_ODDS_API_PROVIDER, "Same Name", 1),
            PlayerAlias(THE_ODDS_API_PROVIDER, "Same Name", 2),
        ),
    )

    with pytest.raises(AmbiguousEntityError, match="multiple IDs"):
        resolver.resolve(
            provider=THE_ODDS_API_PROVIDER,
            raw_name="Same Name",
        )


def test_same_surname_players_keep_distinct_ids_and_display_names() -> None:
    normalizer = OddsApiNormalizer(
        player_resolver=InMemoryPlayerResolver(
            players=(
                Player(2001, "Serena Williams"),
                Player(2002, "Venus Williams"),
            )
        ),
        tournaments=(Tournament("tournament-wimbledon", "ATP Wimbledon"),),
        bookmakers=(Bookmaker("bookmaker-pinnacle", "Pinnacle"),),
        tournament_aliases={
            "tennis_atp_wimbledon": "tournament-wimbledon",
        },
        bookmaker_aliases={"pinnacle": "bookmaker-pinnacle"},
    )
    response = make_response(
        home_team="Serena Williams",
        away_team="Venus Williams",
        outcomes=[
            {"name": "Serena Williams", "price": 1.8},
            {"name": "Venus Williams", "price": 2.1},
        ],
    )

    event = normalizer.normalize(response)[0]

    assert event.match.player_ids == (2001, 2002)
    assert Player(2001, "Serena Williams").display_name == "Serena Williams"
    assert Player(2002, "Venus Williams").display_name == "Venus Williams"


@pytest.mark.parametrize("player_id", [0, -1, True])
def test_player_id_must_be_a_positive_integer(player_id: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        Player(player_id, "Player Name")


def test_incomplete_market_is_rejected() -> None:
    outcomes = [{"name": "Jannik Sinner", "price": 1.8}]

    with pytest.raises(InvalidMarketError, match="exactly two outcomes"):
        make_normalizer().normalize(make_response(outcomes=outcomes))


def test_outcomes_must_match_event_participants() -> None:
    outcomes = [
        {"name": "Jannik Sinner", "price": 1.8},
        {"name": "J. Sinner", "price": 2.1},
    ]

    with pytest.raises(InvalidMarketError, match="do not match"):
        make_normalizer().normalize(make_response(outcomes=outcomes))


def test_naive_provider_timestamp_is_rejected() -> None:
    with pytest.raises(InvalidMarketError, match="timezone-aware"):
        make_normalizer().normalize(
            make_response(commence_time="2026-07-12T15:00:00")
        )
