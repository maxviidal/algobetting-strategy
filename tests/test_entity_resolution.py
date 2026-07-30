import pytest
from tennis_value.domain import Player
from tennis_value.entity_resolution import (
    AmbiguousEntityError,
    InMemoryPlayerResolver,
    PlayerAlias,
    PlayerExternalId,
    UnknownEntityError,
)


def test_aliases_are_scoped_to_provider() -> None:
    resolver = InMemoryPlayerResolver(
        players=(
            Player(1, "First Alex Smith"),
            Player(2, "Second Alex Smith"),
        ),
        aliases=(
            PlayerAlias("provider-a", "Alex Smith", 1),
            PlayerAlias("provider-b", "Alex Smith", 2),
        ),
    )

    assert resolver.resolve(provider="provider-a", raw_name="Alex Smith") == 1
    assert resolver.resolve(provider="provider-b", raw_name="Alex Smith") == 2


def test_external_id_is_preferred_over_an_ambiguous_name() -> None:
    resolver = InMemoryPlayerResolver(
        players=(
            Player(1, "Alex Smith"),
            Player(2, "Alex Smith"),
        ),
        external_ids=(PlayerExternalId("provider-a", "external-2", 2),),
    )

    assert (
        resolver.resolve(
            provider="provider-a",
            raw_name="Alex Smith",
            external_id="external-2",
        )
        == 2
    )


def test_duplicate_canonical_name_is_ambiguous_without_external_identity() -> None:
    resolver = InMemoryPlayerResolver(
        players=(
            Player(1, "Alex Smith"),
            Player(2, "Alex Smith"),
        )
    )

    with pytest.raises(AmbiguousEntityError, match="multiple IDs"):
        resolver.resolve(provider="provider-a", raw_name="Alex Smith")


def test_unknown_name_is_not_automatically_created() -> None:
    resolver = InMemoryPlayerResolver(players=(Player(1, "Known Player"),))

    with pytest.raises(UnknownEntityError, match="Unknown Player"):
        resolver.resolve(provider="provider-a", raw_name="Unknown Player")


def test_get_player_returns_display_name_separately_from_id() -> None:
    resolver = InMemoryPlayerResolver(
        players=(Player(42, "Tallon Griekspoor"),)
    )

    assert resolver.get_player(42) == Player(42, "Tallon Griekspoor")
