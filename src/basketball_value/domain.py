"""Normalized NBA domain records."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5


def stable_game_id(source: str, source_event_id: str) -> str:
    """Create an identity that is unaffected by rescheduling."""

    _non_empty(source, "source")
    _non_empty(source_event_id, "source_event_id")
    return str(uuid5(NAMESPACE_URL, f"{source}:{source_event_id}"))


@dataclass(frozen=True, slots=True)
class Team:
    team_id: str
    abbreviation: str
    display_name: str
    provider_ids: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _non_empty(self.team_id, "team_id")
        _non_empty(self.abbreviation, "abbreviation")
        _non_empty(self.display_name, "display_name")
        if len({source for source, _ in self.provider_ids}) != len(
            self.provider_ids
        ):
            raise ValueError("provider_ids must contain one ID per source")
        for source, provider_id in self.provider_ids:
            _non_empty(source, "provider source")
            _non_empty(provider_id, "provider ID")


@dataclass(frozen=True, slots=True)
class Game:
    game_id: str
    source: str
    source_event_id: str
    season: str
    home_team_id: str
    away_team_id: str
    scheduled_start: datetime
    postseason: bool = False

    def __post_init__(self) -> None:
        for value, name in (
            (self.game_id, "game_id"),
            (self.source, "source"),
            (self.source_event_id, "source_event_id"),
            (self.season, "season"),
        ):
            _non_empty(value, name)
        if self.game_id != stable_game_id(self.source, self.source_event_id):
            raise ValueError("game_id must be derived from source and source_event_id")
        if self.home_team_id == self.away_team_id:
            raise ValueError("home and away teams must be distinct")
        _require_utc(self.scheduled_start, "scheduled_start")


@dataclass(frozen=True, slots=True)
class MoneylinePrice:
    team_id: str
    decimal_odds: Decimal

    def __post_init__(self) -> None:
        _non_empty(self.team_id, "team_id")
        if not self.decimal_odds.is_finite() or self.decimal_odds <= 1:
            raise ValueError("decimal_odds must be finite and greater than 1")


@dataclass(frozen=True, slots=True)
class MoneylineSnapshot:
    snapshot_id: str
    game_id: str
    bookmaker_id: str
    observed_at: datetime
    prices: tuple[MoneylinePrice, MoneylinePrice]
    source: str
    source_event_id: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.snapshot_id, "snapshot_id"),
            (self.game_id, "game_id"),
            (self.bookmaker_id, "bookmaker_id"),
            (self.source, "source"),
            (self.source_event_id, "source_event_id"),
        ):
            _non_empty(value, name)
        _require_utc(self.observed_at, "observed_at")
        if len({price.team_id for price in self.prices}) != 2:
            raise ValueError("a moneyline snapshot must contain two distinct teams")

    def price_for(self, team_id: str) -> Decimal:
        for price in self.prices:
            if price.team_id == team_id:
                return price.decimal_odds
        raise KeyError(team_id)


@dataclass(frozen=True, slots=True)
class GameResult:
    game_id: str
    home_score: int | None
    away_score: int | None
    final: bool
    postponed: bool

    @property
    def winner_team_side(self) -> str | None:
        if (
            not self.final
            or self.postponed
            or self.home_score is None
            or self.away_score is None
            or self.home_score == self.away_score
        ):
            return None
        return "home" if self.home_score > self.away_score else "away"


def _non_empty(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must be in UTC")
