"""SQLite persistence for the player identity registry."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from tennis_value.domain import Player, PlayerId
from tennis_value.entity_resolution import (
    AmbiguousEntityError,
    UnknownEntityError,
    normalized_name,
)


@dataclass(frozen=True, slots=True)
class UnresolvedPlayerName:
    """A provider name that requires alias approval or a new player record."""

    provider: str
    raw_name: str
    normalized_name: str
    reason: str
    occurrences: int
    first_seen_at: datetime
    last_seen_at: datetime


class SqlitePlayerRegistry:
    """Assign player IDs and resolve provider identities using SQLite."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def add_player(self, display_name: str) -> Player:
        """Create a player and let SQLite assign its positive integer ID."""

        name_key = normalized_name(display_name)
        if not name_key:
            raise ValueError("display_name must not be empty")
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO players (display_name, normalized_display_name)
                VALUES (?, ?)
                """,
                (display_name.strip(), name_key),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return a player ID")
        return Player(player_id=cursor.lastrowid, display_name=display_name.strip())

    def get_player(self, player_id: PlayerId) -> Player:
        """Return the display record for one internal player ID."""

        row = self._connection.execute(
            "SELECT id, display_name FROM players WHERE id = ?",
            (player_id,),
        ).fetchone()
        if row is None:
            raise UnknownEntityError(f"unknown player ID {player_id!r}")
        return Player(player_id=int(row[0]), display_name=str(row[1]))

    def add_alias(
        self,
        *,
        provider: str,
        raw_name: str,
        player_id: PlayerId,
    ) -> None:
        """Register one approved provider name for an existing player."""

        provider_key = _required_provider(provider)
        name_key = normalized_name(raw_name)
        if not name_key:
            raise ValueError("raw_name must not be empty")
        self.get_player(player_id)
        with self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO player_aliases (
                    provider,
                    raw_name,
                    normalized_name,
                    player_id
                )
                VALUES (?, ?, ?, ?)
                """,
                (provider_key, raw_name.strip(), name_key, player_id),
            )

    def add_external_id(
        self,
        *,
        provider: str,
        external_id: str,
        player_id: PlayerId,
    ) -> None:
        """Link a provider's stable player ID to an internal player."""

        provider_key = _required_provider(provider)
        external_key = _required_external_id(external_id)
        self.get_player(player_id)
        existing = self._connection.execute(
            """
            SELECT player_id
            FROM player_external_ids
            WHERE provider = ? AND external_id = ?
            """,
            (provider_key, external_key),
        ).fetchone()
        if existing is not None:
            if int(existing[0]) == player_id:
                return
            message = (
                f"external player ID {(provider_key, external_key)!r} is already "
                "registered to another player"
            )
            raise ValueError(message)
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO player_external_ids (
                        provider,
                        external_id,
                        player_id
                    )
                    VALUES (?, ?, ?)
                    """,
                    (provider_key, external_key, player_id),
                )
        except sqlite3.IntegrityError as error:
            message = (
                f"external player ID {(provider_key, external_key)!r} is already "
                "registered"
            )
            raise ValueError(message) from error

    def resolve(
        self,
        *,
        provider: str,
        raw_name: str,
        external_id: str | None = None,
    ) -> PlayerId:
        """Resolve external ID, approved alias, then exact canonical name."""

        provider_key = _required_provider(provider)
        name_key = normalized_name(raw_name)
        if not name_key:
            raise UnknownEntityError("player name must not be empty")

        if external_id is not None:
            external_key = _required_external_id(external_id)
            row = self._connection.execute(
                """
                SELECT player_id
                FROM player_external_ids
                WHERE provider = ? AND external_id = ?
                """,
                (provider_key, external_key),
            ).fetchone()
            if row is not None:
                player_id = int(row[0])
                self._clear_unresolved(provider_key, name_key)
                return player_id

        alias_ids = self._select_player_ids(
            """
            SELECT player_id
            FROM player_aliases
            WHERE provider = ? AND normalized_name = ?
            """,
            (provider_key, name_key),
        )
        if alias_ids:
            return self._finish_resolution(
                provider_key, raw_name, name_key, alias_ids
            )

        canonical_ids = self._select_player_ids(
            """
            SELECT id
            FROM players
            WHERE normalized_display_name = ?
            """,
            (name_key,),
        )
        if canonical_ids:
            return self._finish_resolution(
                provider_key, raw_name, name_key, canonical_ids
            )

        self._record_unresolved(
            provider=provider_key,
            raw_name=raw_name,
            name_key=name_key,
            reason="unknown",
        )
        raise UnknownEntityError(
            f"unknown player name {raw_name!r} for provider {provider_key!r}"
        )

    def list_unresolved(self) -> tuple[UnresolvedPlayerName, ...]:
        """Return unresolved names in deterministic provider/name order."""

        rows = self._connection.execute(
            """
            SELECT
                provider,
                raw_name,
                normalized_name,
                reason,
                occurrences,
                first_seen_at,
                last_seen_at
            FROM unresolved_player_names
            ORDER BY provider, normalized_name
            """
        ).fetchall()
        return tuple(
            UnresolvedPlayerName(
                provider=str(row[0]),
                raw_name=str(row[1]),
                normalized_name=str(row[2]),
                reason=str(row[3]),
                occurrences=int(row[4]),
                first_seen_at=datetime.fromisoformat(str(row[5])),
                last_seen_at=datetime.fromisoformat(str(row[6])),
            )
            for row in rows
        )

    def _finish_resolution(
        self,
        provider: str,
        raw_name: str,
        name_key: str,
        player_ids: set[PlayerId],
    ) -> PlayerId:
        if len(player_ids) > 1:
            self._record_unresolved(
                provider=provider,
                raw_name=raw_name,
                name_key=name_key,
                reason="ambiguous",
            )
            raise AmbiguousEntityError(
                f"player name {raw_name!r} for provider {provider!r} maps to "
                f"multiple IDs {sorted(player_ids)!r}"
            )
        self._clear_unresolved(provider, name_key)
        return next(iter(player_ids))

    def _select_player_ids(
        self,
        statement: str,
        parameters: tuple[str, ...],
    ) -> set[PlayerId]:
        return {
            int(row[0])
            for row in self._connection.execute(statement, parameters).fetchall()
        }

    def _record_unresolved(
        self,
        *,
        provider: str,
        raw_name: str,
        name_key: str,
        reason: str,
    ) -> None:
        observed_at = datetime.now(UTC).isoformat()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO unresolved_player_names (
                    provider,
                    raw_name,
                    normalized_name,
                    reason,
                    occurrences,
                    first_seen_at,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT (provider, normalized_name) DO UPDATE SET
                    raw_name = excluded.raw_name,
                    reason = excluded.reason,
                    occurrences = unresolved_player_names.occurrences + 1,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    provider,
                    raw_name.strip(),
                    name_key,
                    reason,
                    observed_at,
                    observed_at,
                ),
            )

    def _clear_unresolved(self, provider: str, name_key: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                DELETE FROM unresolved_player_names
                WHERE provider = ? AND normalized_name = ?
                """,
                (provider, name_key),
            )

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS players (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    display_name TEXT NOT NULL CHECK (trim(display_name) <> ''),
                    normalized_display_name TEXT NOT NULL
                        CHECK (trim(normalized_display_name) <> '')
                );

                CREATE INDEX IF NOT EXISTS players_normalized_name_idx
                    ON players (normalized_display_name);

                CREATE TABLE IF NOT EXISTS player_external_ids (
                    provider TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    player_id INTEGER NOT NULL
                        REFERENCES players (id) ON DELETE CASCADE,
                    PRIMARY KEY (provider, external_id)
                );

                CREATE TABLE IF NOT EXISTS player_aliases (
                    provider TEXT NOT NULL,
                    raw_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    player_id INTEGER NOT NULL
                        REFERENCES players (id) ON DELETE CASCADE,
                    PRIMARY KEY (provider, normalized_name, player_id)
                );

                CREATE INDEX IF NOT EXISTS player_alias_lookup_idx
                    ON player_aliases (provider, normalized_name);

                CREATE TABLE IF NOT EXISTS unresolved_player_names (
                    provider TEXT NOT NULL,
                    raw_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    reason TEXT NOT NULL
                        CHECK (reason IN ('unknown', 'ambiguous')),
                    occurrences INTEGER NOT NULL CHECK (occurrences > 0),
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (provider, normalized_name)
                );
                """
            )


def _required_provider(value: str) -> str:
    cleaned = value.strip().casefold()
    if not cleaned:
        raise ValueError("provider must not be empty")
    return cleaned


def _required_external_id(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("external_id must not be empty")
    return cleaned
