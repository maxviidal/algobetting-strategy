"""Resolve provider identities to stable internal domain entities."""

from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol
from unicodedata import normalize as unicode_normalize

from tennis_value.data.domain import Player, PlayerId

THE_ODDS_API_PROVIDER = "the_odds_api"


class EntityResolutionError(ValueError):
    """Base error for an entity that cannot be resolved safely."""


class UnknownEntityError(EntityResolutionError):
    """Raised when no registered entity matches a provider identity."""


class AmbiguousEntityError(EntityResolutionError):
    """Raised when a provider identity matches multiple registered entities."""


class PlayerResolver(Protocol):
    """Resolve a provider's player identity to an internal numeric ID."""

    def resolve(
        self,
        *,
        provider: str,
        raw_name: str,
        external_id: str | None = None,
    ) -> PlayerId:
        """Return one existing player ID or raise an entity-resolution error."""

        ...


@dataclass(frozen=True, slots=True)
class PlayerAlias:
    """One approved provider spelling linked to an internal player."""

    provider: str
    raw_name: str
    player_id: PlayerId


@dataclass(frozen=True, slots=True)
class PlayerExternalId:
    """One provider-owned player identifier linked to an internal player."""

    provider: str
    external_id: str
    player_id: PlayerId


class IdentifiedEntity(Protocol):
    """The fields required by the generic non-player resolver."""

    @property
    def display_name(self) -> str: ...


def normalized_name(value: str) -> str:
    """Build a conservative lookup key without discarding meaningful accents."""

    return " ".join(unicode_normalize("NFKC", value).casefold().split())


class EntityResolver[EntityT: IdentifiedEntity, EntityIdT: Hashable]:
    """Resolve canonical names and explicit aliases for a small static catalog."""

    def __init__(
        self,
        entity_type: str,
        entities: Iterable[EntityT],
        aliases: Mapping[str, EntityIdT],
        id_of: Callable[[EntityT], EntityIdT],
    ) -> None:
        self._entity_type = entity_type
        entities_by_id: dict[EntityIdT, EntityT] = {}
        ids_by_name: dict[str, set[EntityIdT]] = {}

        for entity in entities:
            entity_id = id_of(entity)
            if entity_id in entities_by_id:
                raise ValueError(f"duplicate {entity_type} ID {entity_id!r}")
            entities_by_id[entity_id] = entity
            ids_by_name.setdefault(normalized_name(entity.display_name), set()).add(
                entity_id
            )

        for alias, entity_id in aliases.items():
            if entity_id not in entities_by_id:
                raise ValueError(
                    f"{entity_type} alias {alias!r} references unknown ID {entity_id!r}"
                )
            ids_by_name.setdefault(normalized_name(alias), set()).add(entity_id)

        self._entities_by_id = MappingProxyType(entities_by_id)
        self._ids_by_name = MappingProxyType(
            {name: frozenset(entity_ids) for name, entity_ids in ids_by_name.items()}
        )

    def resolve_any(self, *raw_names: str) -> EntityT:
        entity_ids: set[EntityIdT] = set()
        for raw_name in raw_names:
            entity_ids.update(
                self._ids_by_name.get(normalized_name(raw_name), frozenset())
            )
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


class InMemoryPlayerResolver:
    """Provider-scoped player resolver for tests and small in-memory catalogs."""

    def __init__(
        self,
        *,
        players: Iterable[Player],
        aliases: Iterable[PlayerAlias] = (),
        external_ids: Iterable[PlayerExternalId] = (),
    ) -> None:
        players_by_id: dict[PlayerId, Player] = {}
        ids_by_canonical_name: dict[str, set[PlayerId]] = {}
        for player in players:
            if player.player_id in players_by_id:
                raise ValueError(f"duplicate player ID {player.player_id!r}")
            players_by_id[player.player_id] = player
            ids_by_canonical_name.setdefault(
                normalized_name(player.display_name), set()
            ).add(player.player_id)

        ids_by_alias: dict[tuple[str, str], set[PlayerId]] = {}
        for alias in aliases:
            _require_known_player(
                players_by_id, alias.player_id, "alias", alias.raw_name
            )
            key = (_required_provider(alias.provider), normalized_name(alias.raw_name))
            if not key[1]:
                raise ValueError("player alias must not be empty")
            ids_by_alias.setdefault(key, set()).add(alias.player_id)

        player_by_external_id: dict[tuple[str, str], PlayerId] = {}
        for external_identity in external_ids:
            _require_known_player(
                players_by_id,
                external_identity.player_id,
                "external ID",
                external_identity.external_id,
            )
            key = (
                _required_provider(external_identity.provider),
                _required_external_id(external_identity.external_id),
            )
            existing = player_by_external_id.get(key)
            if existing is not None and existing != external_identity.player_id:
                raise AmbiguousEntityError(
                    f"external player ID {key!r} maps to multiple players"
                )
            player_by_external_id[key] = external_identity.player_id

        self._players_by_id = MappingProxyType(players_by_id)
        self._ids_by_canonical_name = MappingProxyType(
            {
                name: frozenset(player_ids)
                for name, player_ids in ids_by_canonical_name.items()
            }
        )
        self._ids_by_alias = MappingProxyType(
            {key: frozenset(player_ids) for key, player_ids in ids_by_alias.items()}
        )
        self._player_by_external_id = MappingProxyType(player_by_external_id)

    def resolve(
        self,
        *,
        provider: str,
        raw_name: str,
        external_id: str | None = None,
    ) -> PlayerId:
        provider_key = _required_provider(provider)
        name_key = normalized_name(raw_name)
        if not name_key:
            raise UnknownEntityError("player name must not be empty")

        if external_id is not None:
            external_key = _required_external_id(external_id)
            player_id = self._player_by_external_id.get(
                (provider_key, external_key)
            )
            if player_id is not None:
                return player_id

        alias_ids = self._ids_by_alias.get(
            (provider_key, name_key), frozenset()
        )
        if alias_ids:
            return _require_single_player_id(provider_key, raw_name, alias_ids)

        canonical_ids = self._ids_by_canonical_name.get(name_key, frozenset())
        if canonical_ids:
            return _require_single_player_id(provider_key, raw_name, canonical_ids)

        raise UnknownEntityError(
            f"unknown player name {raw_name!r} for provider {provider_key!r}"
        )

    def get_player(self, player_id: PlayerId) -> Player:
        """Return the display record for an existing internal player ID."""

        try:
            return self._players_by_id[player_id]
        except KeyError as error:
            raise UnknownEntityError(f"unknown player ID {player_id!r}") from error


def _require_single_player_id(
    provider: str,
    raw_name: str,
    player_ids: Iterable[PlayerId],
) -> PlayerId:
    candidates = set(player_ids)
    if len(candidates) > 1:
        raise AmbiguousEntityError(
            f"player name {raw_name!r} for provider {provider!r} maps to "
            f"multiple IDs {_display_ids(candidates)}"
        )
    return next(iter(candidates))


def _require_known_player(
    players_by_id: Mapping[PlayerId, Player],
    player_id: PlayerId,
    identifier_type: str,
    identifier: str,
) -> None:
    if player_id not in players_by_id:
        raise ValueError(
            f"player {identifier_type} {identifier!r} references unknown "
            f"player ID {player_id!r}"
        )


def _required_provider(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("provider must not be empty")
    return cleaned.casefold()


def _required_external_id(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("external_id must not be empty")
    return cleaned


def _display_ids(entity_ids: Iterable[Hashable]) -> str:
    return repr(sorted(repr(entity_id) for entity_id in entity_ids))
