"""Normalized domain records used by pricing and backtesting."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

type PlayerId = int


def _require_non_empty(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must be in UTC")


def _require_positive_player_id(value: PlayerId) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("player_id must be a positive integer")


@dataclass(frozen=True, slots=True)
class Player:
    """A player with a stable internal identifier."""

    player_id: PlayerId
    display_name: str

    def __post_init__(self) -> None:
        _require_positive_player_id(self.player_id)
        _require_non_empty(self.display_name, "display_name")


@dataclass(frozen=True, slots=True)
class Tournament:
    """A tournament with a stable internal identifier."""

    tournament_id: str
    display_name: str

    def __post_init__(self) -> None:
        _require_non_empty(self.tournament_id, "tournament_id")
        _require_non_empty(self.display_name, "display_name")


@dataclass(frozen=True, slots=True)
class Bookmaker:
    """A bookmaker or exchange with a stable internal identifier."""

    bookmaker_id: str
    display_name: str

    def __post_init__(self) -> None:
        _require_non_empty(self.bookmaker_id, "bookmaker_id")
        _require_non_empty(self.display_name, "display_name")


@dataclass(frozen=True, slots=True)
class Match:
    """A normalized tennis match."""

    match_id: str
    tournament_id: str
    player_ids: tuple[PlayerId, PlayerId]
    scheduled_start: datetime

    def __post_init__(self) -> None:
        _require_non_empty(self.match_id, "match_id")
        _require_non_empty(self.tournament_id, "tournament_id")
        if len(set(self.player_ids)) != 2:
            raise ValueError("player_ids must contain two distinct players")
        for player_id in self.player_ids:
            _require_positive_player_id(player_id)
        _require_utc(self.scheduled_start, "scheduled_start")


@dataclass(frozen=True, slots=True)
class MatchWinnerPrice:
    """The decimal price offered for one player to win a match."""

    player_id: PlayerId
    decimal_odds: Decimal

    def __post_init__(self) -> None:
        _require_positive_player_id(self.player_id)
        if not self.decimal_odds.is_finite() or self.decimal_odds <= Decimal("1"):
            raise ValueError("decimal_odds must be finite and greater than 1.0")


@dataclass(frozen=True, slots=True)
class OddsSnapshot:
    """One bookmaker's complete match-winner market at an observation time."""

    snapshot_id: str
    match_id: str
    bookmaker_id: str
    observed_at: datetime
    prices: tuple[MatchWinnerPrice, MatchWinnerPrice]
    source: str
    source_event_id: str

    def __post_init__(self) -> None:
        _require_non_empty(self.snapshot_id, "snapshot_id")
        _require_non_empty(self.match_id, "match_id")
        _require_non_empty(self.bookmaker_id, "bookmaker_id")
        _require_non_empty(self.source, "source")
        _require_non_empty(self.source_event_id, "source_event_id")
        _require_utc(self.observed_at, "observed_at")
        if len({price.player_id for price in self.prices}) != 2:
            raise ValueError("prices must contain one price for each of two players")
