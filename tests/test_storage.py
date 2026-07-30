import sqlite3

import pytest
from tennis_value.entity_resolution import (
    AmbiguousEntityError,
    UnknownEntityError,
)
from tennis_value.storage import SqlitePlayerRegistry


@pytest.fixture
def registry() -> SqlitePlayerRegistry:
    return SqlitePlayerRegistry(sqlite3.connect(":memory:"))


def test_database_assigns_distinct_ids_without_changing_display_names(
    registry: SqlitePlayerRegistry,
) -> None:
    first = registry.add_player("Serena Williams")
    second = registry.add_player("Venus Williams")

    assert first.player_id > 0
    assert second.player_id > first.player_id
    assert registry.get_player(first.player_id).display_name == "Serena Williams"
    assert registry.get_player(second.player_id).display_name == "Venus Williams"


def test_provider_alias_resolves_to_existing_player(
    registry: SqlitePlayerRegistry,
) -> None:
    player = registry.add_player("Jannik Sinner")
    registry.add_alias(
        provider="the_odds_api",
        raw_name="J. Sinner",
        player_id=player.player_id,
    )

    assert (
        registry.resolve(
            provider="the_odds_api",
            raw_name="J. Sinner",
        )
        == player.player_id
    )


def test_aliases_are_provider_scoped(registry: SqlitePlayerRegistry) -> None:
    first = registry.add_player("First Alex Smith")
    second = registry.add_player("Second Alex Smith")
    registry.add_alias(
        provider="provider-a",
        raw_name="Alex Smith",
        player_id=first.player_id,
    )
    registry.add_alias(
        provider="provider-b",
        raw_name="Alex Smith",
        player_id=second.player_id,
    )

    assert (
        registry.resolve(provider="provider-a", raw_name="Alex Smith")
        == first.player_id
    )
    assert (
        registry.resolve(provider="provider-b", raw_name="Alex Smith")
        == second.player_id
    )


def test_external_id_has_highest_resolution_priority(
    registry: SqlitePlayerRegistry,
) -> None:
    first = registry.add_player("Alex Smith")
    second = registry.add_player("Alex Smith")
    registry.add_external_id(
        provider="provider-a",
        external_id="player-2",
        player_id=second.player_id,
    )

    assert (
        registry.resolve(
            provider="provider-a",
            raw_name=first.display_name,
            external_id="player-2",
        )
        == second.player_id
    )


def test_external_ids_are_opaque_and_case_sensitive(
    registry: SqlitePlayerRegistry,
) -> None:
    uppercase = registry.add_player("Uppercase ID Player")
    lowercase = registry.add_player("Lowercase ID Player")
    registry.add_external_id(
        provider="provider-a",
        external_id="ABC-123",
        player_id=uppercase.player_id,
    )
    registry.add_external_id(
        provider="provider-a",
        external_id="abc-123",
        player_id=lowercase.player_id,
    )

    assert (
        registry.resolve(
            provider="provider-a",
            raw_name="Not Used",
            external_id="ABC-123",
        )
        == uppercase.player_id
    )
    assert (
        registry.resolve(
            provider="provider-a",
            raw_name="Not Used",
            external_id="abc-123",
        )
        == lowercase.player_id
    )


def test_duplicate_full_names_are_recorded_as_ambiguous(
    registry: SqlitePlayerRegistry,
) -> None:
    registry.add_player("Alex Smith")
    registry.add_player("Alex Smith")

    with pytest.raises(AmbiguousEntityError, match="multiple IDs"):
        registry.resolve(provider="provider-a", raw_name="Alex Smith")

    unresolved = registry.list_unresolved()
    assert len(unresolved) == 1
    assert unresolved[0].normalized_name == "alex smith"
    assert unresolved[0].reason == "ambiguous"
    assert unresolved[0].occurrences == 1


def test_unknown_names_are_counted_but_not_created(
    registry: SqlitePlayerRegistry,
) -> None:
    for _ in range(2):
        with pytest.raises(UnknownEntityError, match="Unknown Player"):
            registry.resolve(provider="provider-a", raw_name="Unknown Player")

    unresolved = registry.list_unresolved()
    assert len(unresolved) == 1
    assert unresolved[0].reason == "unknown"
    assert unresolved[0].occurrences == 2
    with pytest.raises(UnknownEntityError, match="player ID"):
        registry.get_player(1)


def test_successful_alias_resolution_clears_unresolved_name(
    registry: SqlitePlayerRegistry,
) -> None:
    with pytest.raises(UnknownEntityError):
        registry.resolve(provider="provider-a", raw_name="T. Griekspoor")
    player = registry.add_player("Tallon Griekspoor")
    registry.add_alias(
        provider="provider-a",
        raw_name="T. Griekspoor",
        player_id=player.player_id,
    )

    assert (
        registry.resolve(provider="provider-a", raw_name="T. Griekspoor")
        == player.player_id
    )
    assert registry.list_unresolved() == ()


def test_alias_can_represent_ambiguity_instead_of_overwriting(
    registry: SqlitePlayerRegistry,
) -> None:
    first = registry.add_player("First Player")
    second = registry.add_player("Second Player")
    registry.add_alias(
        provider="provider-a",
        raw_name="Same Name",
        player_id=first.player_id,
    )
    registry.add_alias(
        provider="provider-a",
        raw_name="Same Name",
        player_id=second.player_id,
    )

    with pytest.raises(AmbiguousEntityError, match="multiple IDs"):
        registry.resolve(provider="provider-a", raw_name="Same Name")
