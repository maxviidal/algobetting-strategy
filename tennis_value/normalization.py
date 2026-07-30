"""Resolve raw Odds API names into validated, normalized domain records."""

from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Protocol
from unicodedata import normalize as unicode_normalize
from uuid import UUID, uuid5

from tennis_value.domain import (
    Bookmaker,
    Match,
    MatchWinnerPrice,
    OddsSnapshot,
    Player,
    PlayerId,
    Tournament,
)
from tennis_value.ingestion import IngestedOddsApiResponse, JsonValue, OddsApiEvent

_ID_NAMESPACE = UUID("f98f59f8-35e7-44f7-bdb0-913bb0ce0465")


class NormalizationError(ValueError):
    """Base error for input that cannot be normalized safely."""


class UnknownEntityError(NormalizationError):
    """Raised when a raw name has no registered entity or alias."""


class AmbiguousEntityError(NormalizationError):
    """Raised when a lookup could refer to multiple registered entities."""


class InvalidMarketError(NormalizationError):
    """Raised when an event or market is incomplete or inconsistent."""


class IdentifiedEntity(Protocol):
    """The fields required by the internal entity resolver."""

    @property
    def display_name(self) -> str: ...


def normalized_name(value: str) -> str:
    """Build a conservative key without discarding meaningful accents."""

    return " ".join(unicode_normalize("NFKC", value).casefold().split())


class _EntityResolver[EntityT: IdentifiedEntity, EntityIdT: Hashable]:
    def __init__(
        self,
        entity_type: str,
        entities: Iterable[EntityT],
        aliases: Mapping[str, EntityIdT],
        id_of: Callable[[EntityT], EntityIdT],
    ) -> None:
        self._entity_type = entity_type
        entities_by_id: dict[EntityIdT, EntityT] = {}
        names: dict[str, set[EntityIdT]] = {}

        for entity in entities:
            entity_id = id_of(entity)
            if entity_id in entities_by_id:
                raise ValueError(f"duplicate {entity_type} ID {entity_id!r}")
            entities_by_id[entity_id] = entity
            names.setdefault(normalized_name(entity.display_name), set()).add(entity_id)

        for alias, entity_id in aliases.items():
            if entity_id not in entities_by_id:
                raise ValueError(
                    f"{entity_type} alias {alias!r} references unknown ID {entity_id!r}"
                )
            names.setdefault(normalized_name(alias), set()).add(entity_id)

        ambiguous = {name: ids for name, ids in names.items() if len(ids) > 1}
        if ambiguous:
            name, entity_ids = next(iter(ambiguous.items()))
            raise AmbiguousEntityError(
                f"ambiguous {entity_type} name {name!r} maps to "
                f"{_display_ids(entity_ids)}"
            )

        self._entities_by_id = MappingProxyType(entities_by_id)
        self._id_by_name = MappingProxyType(
            {name: next(iter(entity_ids)) for name, entity_ids in names.items()}
        )

    def resolve_any(self, *raw_names: str) -> EntityT:
        entity_ids = {
            entity_id
            for raw_name in raw_names
            if (
                entity_id := self._id_by_name.get(normalized_name(raw_name))
            ) is not None
        }
        if not entity_ids:
            raise UnknownEntityError(
                f"unknown {self._entity_type} name(s) {raw_names!r}; "
                "register the entity or an explicit alias"
            )
        if len(entity_ids) > 1:
            raise AmbiguousEntityError(
                f"{self._entity_type} names {raw_names!r} resolve to different IDs "
                f"{_display_ids(entity_ids)}"
            )
        return self._entities_by_id[next(iter(entity_ids))]


@dataclass(frozen=True, slots=True)
class NormalizedMatchWinnerEvent:
    """A normalized match and the bookmaker snapshots supplied for it."""

    match: Match
    snapshots: tuple[OddsSnapshot, ...]


class OddsApiNormalizer:
    """Normalize ingested Odds API events using an explicit entity catalog."""

    def __init__(
        self,
        *,
        players: Iterable[Player],
        tournaments: Iterable[Tournament],
        bookmakers: Iterable[Bookmaker],
        player_aliases: Mapping[str, PlayerId] | None = None,
        tournament_aliases: Mapping[str, str] | None = None,
        bookmaker_aliases: Mapping[str, str] | None = None,
    ) -> None:
        self._players = _EntityResolver(
            "player", players, player_aliases or {}, lambda player: player.player_id
        )
        self._tournaments = _EntityResolver(
            "tournament",
            tournaments,
            tournament_aliases or {},
            lambda tournament: tournament.tournament_id,
        )
        self._bookmakers = _EntityResolver(
            "bookmaker",
            bookmakers,
            bookmaker_aliases or {},
            lambda bookmaker: bookmaker.bookmaker_id,
        )

    def normalize(
        self,
        response: IngestedOddsApiResponse,
    ) -> tuple[NormalizedMatchWinnerEvent, ...]:
        """Normalize every event in an ingested response."""

        return tuple(
            self._normalize_event(event, response, index)
            for index, event in enumerate(response.events)
        )

    def _normalize_event(
        self,
        event: OddsApiEvent,
        response: IngestedOddsApiResponse,
        event_index: int,
    ) -> NormalizedMatchWinnerEvent:
        context = f"event[{event_index}]"
        source_event_id = _required_string(event, "id", context)
        sport_key = _required_string(event, "sport_key", context)
        sport_title = _required_string(event, "sport_title", context)
        home_name = _required_string(event, "home_team", context)
        away_name = _required_string(event, "away_team", context)
        scheduled_start = _parse_timestamp(
            _required_string(event, "commence_time", context),
            f"{context}.commence_time",
        )

        tournament = self._tournaments.resolve_any(sport_key, sport_title)
        home_player = self._players.resolve_any(home_name)
        away_player = self._players.resolve_any(away_name)
        player_ids = (home_player.player_id, away_player.player_id)
        if len(set(player_ids)) != 2:
            raise InvalidMarketError(
                f"{context} participants resolve to the same player ID"
            )

        match_id = _stable_id(
            "match",
            tournament.tournament_id,
            *(str(player_id) for player_id in sorted(player_ids)),
            scheduled_start.isoformat(),
        )
        match = Match(
            match_id=match_id,
            tournament_id=tournament.tournament_id,
            player_ids=player_ids,
            scheduled_start=scheduled_start,
        )

        raw_bookmakers = _required_object_list(event, "bookmakers", context)
        snapshots: list[OddsSnapshot] = []
        seen_bookmakers: set[str] = set()
        for bookmaker_index, raw_bookmaker in enumerate(raw_bookmakers):
            bookmaker_context = f"{context}.bookmakers[{bookmaker_index}]"
            snapshot = self._normalize_bookmaker(
                raw_bookmaker,
                bookmaker_context,
                match,
                response,
                source_event_id,
            )
            if snapshot.bookmaker_id in seen_bookmakers:
                raise InvalidMarketError(
                    f"{context} contains duplicate bookmaker "
                    f"{snapshot.bookmaker_id!r}"
                )
            seen_bookmakers.add(snapshot.bookmaker_id)
            snapshots.append(snapshot)

        return NormalizedMatchWinnerEvent(match=match, snapshots=tuple(snapshots))

    def _normalize_bookmaker(
        self,
        raw_bookmaker: Mapping[str, JsonValue],
        context: str,
        match: Match,
        response: IngestedOddsApiResponse,
        source_event_id: str,
    ) -> OddsSnapshot:
        bookmaker_key = _required_string(raw_bookmaker, "key", context)
        bookmaker_title = _required_string(raw_bookmaker, "title", context)
        bookmaker = self._bookmakers.resolve_any(bookmaker_key, bookmaker_title)
        markets = _required_object_list(raw_bookmaker, "markets", context)
        match_winner_markets = [
            market for market in markets if market.get("key") == "h2h"
        ]
        if len(match_winner_markets) != 1:
            raise InvalidMarketError(
                f"{context} must contain exactly one 'h2h' market; "
                f"found {len(match_winner_markets)}"
            )

        market = match_winner_markets[0]
        observed_value = market.get("last_update", raw_bookmaker.get("last_update"))
        if not isinstance(observed_value, str) or not observed_value.strip():
            raise InvalidMarketError(
                f"{context} h2h market must have a string observation timestamp"
            )
        observed_at = _parse_timestamp(observed_value, f"{context}.last_update")
        outcomes = _required_object_list(market, "outcomes", f"{context}.h2h")
        if len(outcomes) != 2:
            raise InvalidMarketError(
                f"{context} h2h market must have exactly two outcomes"
            )

        prices = tuple(
            self._normalize_outcome(outcome, f"{context}.h2h.outcomes[{index}]")
            for index, outcome in enumerate(outcomes)
        )
        if {price.player_id for price in prices} != set(match.player_ids):
            raise InvalidMarketError(
                f"{context} outcome players do not match the event participants"
            )

        snapshot_id = _stable_id(
            "snapshot",
            match.match_id,
            bookmaker.bookmaker_id,
            observed_at.isoformat(),
            response.source,
            source_event_id,
        )
        return OddsSnapshot(
            snapshot_id=snapshot_id,
            match_id=match.match_id,
            bookmaker_id=bookmaker.bookmaker_id,
            observed_at=observed_at,
            prices=(prices[0], prices[1]),
            source=response.source,
            source_event_id=source_event_id,
        )

    def _normalize_outcome(
        self,
        raw_outcome: Mapping[str, JsonValue],
        context: str,
    ) -> MatchWinnerPrice:
        participant_name = _required_string(raw_outcome, "name", context)
        raw_price = raw_outcome.get("price")
        if isinstance(raw_price, bool) or not isinstance(raw_price, int | float):
            raise InvalidMarketError(f"{context}.price must be a JSON number")
        try:
            price = MatchWinnerPrice(
                player_id=self._players.resolve_any(participant_name).player_id,
                decimal_odds=Decimal(str(raw_price)),
            )
        except (InvalidOperation, ValueError) as error:
            raise InvalidMarketError(f"{context} has invalid decimal odds") from error
        return price


def _required_string(
    value: Mapping[str, JsonValue],
    key: str,
    context: str,
) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise InvalidMarketError(f"{context}.{key} must be a non-empty string")
    return item


def _required_object_list(
    value: Mapping[str, JsonValue],
    key: str,
    context: str,
) -> list[dict[str, JsonValue]]:
    item = value.get(key)
    if not isinstance(item, list):
        raise InvalidMarketError(f"{context}.{key} must be an array")
    objects: list[dict[str, JsonValue]] = []
    for element in item:
        if not isinstance(element, dict):
            raise InvalidMarketError(f"{context}.{key} must contain only objects")
        objects.append(element)
    return objects


def _parse_timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidMarketError(
            f"{field_name} must be a valid ISO 8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidMarketError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _stable_id(record_type: str, *parts: str) -> str:
    return str(uuid5(_ID_NAMESPACE, "|".join((record_type, *parts))))


def _display_ids(entity_ids: Iterable[Hashable]) -> str:
    return repr(sorted(repr(entity_id) for entity_id in entity_ids))
