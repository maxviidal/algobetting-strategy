"""SQLite persistence for identities and immutable point-in-time odds."""

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from tennis_value.data.domain import (
    Match,
    MatchWinnerPrice,
    OddsSnapshot,
    Player,
    PlayerId,
)
from tennis_value.data.entity_resolution import (
    AmbiguousEntityError,
    UnknownEntityError,
    normalized_name,
)
from tennis_value.data.ingestion import IngestedOddsApiResponse


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


@dataclass(frozen=True, slots=True)
class StoredRawOddsResponse:
    """An unchanged provider response retained for audit and replay."""

    response_id: str
    raw_bytes: bytes
    collected_at: datetime
    source: str
    provider_snapshot_at: str | None


class OddsStorageError(ValueError):
    """Base exception for odds-history persistence failures."""


class StorageConflictError(OddsStorageError):
    """Raised when a stable ID is reused for different immutable data."""


class StorageIntegrityError(OddsStorageError):
    """Raised when persisted rows cannot form valid domain records."""


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


class SqliteOddsRepository:
    """Persist and retrieve immutable point-in-time match-winner markets."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def save_raw_response(self, response: IngestedOddsApiResponse) -> str:
        """Store exact provider bytes and return a deterministic response ID."""

        response_id = _raw_response_id(response)
        stored_values = (
            response.raw_bytes,
            _datetime_to_storage(response.collected_at),
            response.source,
            response.provider_snapshot_at,
        )
        existing = self._connection.execute(
            """
            SELECT raw_bytes, collected_at, source, provider_snapshot_at
            FROM raw_odds_responses
            WHERE response_id = ?
            """,
            (response_id,),
        ).fetchone()
        if existing is not None:
            existing_values = (
                bytes(existing[0]),
                str(existing[1]),
                str(existing[2]),
                None if existing[3] is None else str(existing[3]),
            )
            if existing_values != stored_values:
                raise StorageConflictError(
                    f"raw response ID {response_id!r} has conflicting content"
                )
            return response_id

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO raw_odds_responses (
                    response_id,
                    raw_bytes,
                    collected_at,
                    source,
                    provider_snapshot_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (response_id, *stored_values),
            )
        return response_id

    def get_raw_response(self, response_id: str) -> StoredRawOddsResponse:
        """Return one exact raw response by its deterministic ID."""

        row = self._connection.execute(
            """
            SELECT raw_bytes, collected_at, source, provider_snapshot_at
            FROM raw_odds_responses
            WHERE response_id = ?
            """,
            (response_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown raw response ID {response_id!r}")
        return StoredRawOddsResponse(
            response_id=response_id,
            raw_bytes=bytes(row[0]),
            collected_at=_datetime_from_storage(row[1], "collected_at"),
            source=str(row[2]),
            provider_snapshot_at=(
                None if row[3] is None else str(row[3])
            ),
        )

    def get_latest_raw_response(self) -> StoredRawOddsResponse:
        """Return the most recently collected raw response."""

        row = self._connection.execute(
            """
            SELECT response_id
            FROM raw_odds_responses
            ORDER BY collected_at DESC, response_id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            raise KeyError("no raw odds responses are stored")
        return self.get_raw_response(str(row[0]))

    def save_match(self, match: Match) -> None:
        """Insert one match or verify an identical existing record."""

        values = _match_storage_values(match)
        existing = self._connection.execute(
            """
            SELECT tournament_id, player_one_id, player_two_id, scheduled_start
            FROM matches
            WHERE match_id = ?
            """,
            (match.match_id,),
        ).fetchone()
        if existing is not None:
            existing_values = (
                str(existing[0]),
                int(existing[1]),
                int(existing[2]),
                str(existing[3]),
            )
            if existing_values != values:
                raise StorageConflictError(
                    f"match ID {match.match_id!r} has conflicting immutable data"
                )
            return

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO matches (
                    match_id,
                    tournament_id,
                    player_one_id,
                    player_two_id,
                    scheduled_start
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (match.match_id, *values),
            )

    def get_match(self, match_id: str) -> Match:
        """Return one persisted normalized match."""

        row = self._connection.execute(
            """
            SELECT tournament_id, player_one_id, player_two_id, scheduled_start
            FROM matches
            WHERE match_id = ?
            """,
            (match_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown match ID {match_id!r}")
        try:
            return Match(
                match_id=match_id,
                tournament_id=str(row[0]),
                player_ids=(int(row[1]), int(row[2])),
                scheduled_start=_datetime_from_storage(
                    row[3],
                    "scheduled_start",
                ),
            )
        except ValueError as error:
            raise StorageIntegrityError(
                f"stored match {match_id!r} is invalid: {error}"
            ) from error

    def save_snapshot(
        self,
        snapshot: OddsSnapshot,
        *,
        raw_response_id: str | None = None,
    ) -> None:
        """Insert one immutable snapshot or verify an identical existing one."""

        match = self.get_match(snapshot.match_id)
        snapshot_players = {price.player_id for price in snapshot.prices}
        if snapshot_players != set(match.player_ids):
            raise OddsStorageError(
                f"snapshot {snapshot.snapshot_id!r} participants do not match "
                f"match {snapshot.match_id!r}"
            )
        if raw_response_id is not None:
            self._require_raw_response(raw_response_id)

        header_values = _snapshot_storage_values(snapshot, raw_response_id)
        price_values = _snapshot_price_storage_values(snapshot)
        existing = self._connection.execute(
            """
            SELECT
                match_id,
                bookmaker_id,
                observed_at,
                source,
                source_event_id,
                raw_response_id
            FROM odds_snapshots
            WHERE snapshot_id = ?
            """,
            (snapshot.snapshot_id,),
        ).fetchone()
        if existing is not None:
            existing_header = (
                str(existing[0]),
                str(existing[1]),
                str(existing[2]),
                str(existing[3]),
                str(existing[4]),
                None if existing[5] is None else str(existing[5]),
            )
            existing_prices = tuple(
                (int(row[0]), str(row[1]))
                for row in self._connection.execute(
                    """
                    SELECT player_id, decimal_odds
                    FROM odds_snapshot_prices
                    WHERE snapshot_id = ?
                    ORDER BY player_id
                    """,
                    (snapshot.snapshot_id,),
                ).fetchall()
            )
            if (
                existing_header[:5] != header_values[:5]
                or existing_prices != price_values
            ):
                raise StorageConflictError(
                    f"snapshot ID {snapshot.snapshot_id!r} has conflicting "
                    "immutable data"
                )
            if raw_response_id is not None:
                self._link_snapshot_to_raw_response(
                    snapshot.snapshot_id,
                    raw_response_id,
                )
            return

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO odds_snapshots (
                    snapshot_id,
                    match_id,
                    bookmaker_id,
                    observed_at,
                    source,
                    source_event_id,
                    raw_response_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (snapshot.snapshot_id, *header_values),
            )
            self._connection.executemany(
                """
                INSERT INTO odds_snapshot_prices (
                    snapshot_id,
                    player_id,
                    decimal_odds
                )
                VALUES (?, ?, ?)
                """,
                (
                    (snapshot.snapshot_id, player_id, decimal_odds)
                    for player_id, decimal_odds in price_values
                ),
            )
            if raw_response_id is not None:
                self._link_snapshot_to_raw_response(
                    snapshot.snapshot_id,
                    raw_response_id,
                )

    def save_snapshots(
        self,
        snapshots: tuple[OddsSnapshot, ...],
        *,
        raw_response_id: str | None = None,
    ) -> None:
        """Persist a deterministic batch of snapshots."""

        for snapshot in sorted(snapshots, key=lambda item: item.snapshot_id):
            self.save_snapshot(snapshot, raw_response_id=raw_response_id)

    def latest_snapshots_as_of(
        self,
        match_id: str,
        *,
        decision_at: datetime,
    ) -> tuple[OddsSnapshot, ...]:
        """Return each bookmaker's latest snapshot known at decision time."""

        _require_utc_storage_time(decision_at, "decision_at")
        self._require_match(match_id)
        rows = self._connection.execute(
            """
            WITH latest_times AS (
                SELECT
                    bookmaker_id,
                    MAX(observed_at) AS observed_at
                FROM odds_snapshots
                WHERE
                    match_id = ?
                    AND observed_at <= ?
                GROUP BY bookmaker_id
            )
            SELECT
                snapshots.snapshot_id,
                snapshots.match_id,
                snapshots.bookmaker_id,
                snapshots.observed_at,
                snapshots.source,
                snapshots.source_event_id,
                prices.player_id,
                prices.decimal_odds
            FROM odds_snapshots AS snapshots
            JOIN latest_times
                ON latest_times.bookmaker_id = snapshots.bookmaker_id
                AND latest_times.observed_at = snapshots.observed_at
            JOIN odds_snapshot_prices AS prices
                ON prices.snapshot_id = snapshots.snapshot_id
            WHERE snapshots.match_id = ?
            ORDER BY
                snapshots.bookmaker_id,
                snapshots.snapshot_id,
                prices.player_id
            """,
            (
                match_id,
                _datetime_to_storage(decision_at),
                match_id,
            ),
        ).fetchall()
        return _collapse_equivalent_latest_snapshots(
            _snapshots_from_query_rows(rows)
        )

    def link_match_to_raw_response(
        self,
        match_id: str,
        response_id: str,
    ) -> None:
        """Record that a normalized match appeared in one raw response."""

        self._require_match(match_id)
        self._require_raw_response(response_id)
        with self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO raw_response_matches (
                    response_id,
                    match_id
                )
                VALUES (?, ?)
                """,
                (response_id, match_id),
            )

    def match_ids_for_raw_response(self, response_id: str) -> tuple[str, ...]:
        """Return normalized matches linked to one collected response."""

        self._require_raw_response(response_id)
        rows = self._connection.execute(
            """
            SELECT match_id
            FROM raw_response_matches
            WHERE response_id = ?
            ORDER BY match_id
            """,
            (response_id,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _require_match(self, match_id: str) -> None:
        row = self._connection.execute(
            "SELECT 1 FROM matches WHERE match_id = ?",
            (match_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown match ID {match_id!r}")

    def _require_raw_response(self, response_id: str) -> None:
        row = self._connection.execute(
            "SELECT 1 FROM raw_odds_responses WHERE response_id = ?",
            (response_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown raw response ID {response_id!r}")

    def _link_snapshot_to_raw_response(
        self,
        snapshot_id: str,
        response_id: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO raw_response_snapshots (
                response_id,
                snapshot_id
            )
            VALUES (?, ?)
            """,
            (response_id, snapshot_id),
        )

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS raw_odds_responses (
                    response_id TEXT PRIMARY KEY,
                    raw_bytes BLOB NOT NULL,
                    collected_at TEXT NOT NULL,
                    source TEXT NOT NULL CHECK (trim(source) <> ''),
                    provider_snapshot_at TEXT
                );

                CREATE TABLE IF NOT EXISTS matches (
                    match_id TEXT PRIMARY KEY,
                    tournament_id TEXT NOT NULL
                        CHECK (trim(tournament_id) <> ''),
                    player_one_id INTEGER NOT NULL CHECK (player_one_id > 0),
                    player_two_id INTEGER NOT NULL CHECK (player_two_id > 0),
                    scheduled_start TEXT NOT NULL,
                    CHECK (player_one_id <> player_two_id)
                );

                CREATE TABLE IF NOT EXISTS odds_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    match_id TEXT NOT NULL
                        REFERENCES matches (match_id) ON DELETE RESTRICT,
                    bookmaker_id TEXT NOT NULL
                        CHECK (trim(bookmaker_id) <> ''),
                    observed_at TEXT NOT NULL,
                    source TEXT NOT NULL CHECK (trim(source) <> ''),
                    source_event_id TEXT NOT NULL
                        CHECK (trim(source_event_id) <> ''),
                    raw_response_id TEXT
                        REFERENCES raw_odds_responses (response_id)
                        ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS odds_snapshot_as_of_idx
                    ON odds_snapshots (
                        match_id,
                        bookmaker_id,
                        observed_at DESC
                    );

                CREATE TABLE IF NOT EXISTS odds_snapshot_prices (
                    snapshot_id TEXT NOT NULL
                        REFERENCES odds_snapshots (snapshot_id)
                        ON DELETE CASCADE,
                    player_id INTEGER NOT NULL CHECK (player_id > 0),
                    decimal_odds TEXT NOT NULL,
                    PRIMARY KEY (snapshot_id, player_id)
                );

                CREATE TABLE IF NOT EXISTS raw_response_snapshots (
                    response_id TEXT NOT NULL
                        REFERENCES raw_odds_responses (response_id)
                        ON DELETE RESTRICT,
                    snapshot_id TEXT NOT NULL
                        REFERENCES odds_snapshots (snapshot_id)
                        ON DELETE RESTRICT,
                    PRIMARY KEY (response_id, snapshot_id)
                );

                CREATE TABLE IF NOT EXISTS raw_response_matches (
                    response_id TEXT NOT NULL
                        REFERENCES raw_odds_responses (response_id)
                        ON DELETE RESTRICT,
                    match_id TEXT NOT NULL
                        REFERENCES matches (match_id)
                        ON DELETE RESTRICT,
                    PRIMARY KEY (response_id, match_id)
                );

                INSERT OR IGNORE INTO raw_response_snapshots (
                    response_id,
                    snapshot_id
                )
                SELECT raw_response_id, snapshot_id
                FROM odds_snapshots
                WHERE raw_response_id IS NOT NULL;
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


def _raw_response_id(response: IngestedOddsApiResponse) -> str:
    digest = hashlib.sha256()
    for value in (
        response.source.encode(),
        _datetime_to_storage(response.collected_at).encode(),
        response.raw_bytes,
    ):
        digest.update(len(value).to_bytes(8, byteorder="big"))
        digest.update(value)
    return digest.hexdigest()


def _match_storage_values(match: Match) -> tuple[str, int, int, str]:
    return (
        match.tournament_id,
        match.player_ids[0],
        match.player_ids[1],
        _datetime_to_storage(match.scheduled_start),
    )


def _snapshot_storage_values(
    snapshot: OddsSnapshot,
    raw_response_id: str | None,
) -> tuple[str, str, str, str, str, str | None]:
    return (
        snapshot.match_id,
        snapshot.bookmaker_id,
        _datetime_to_storage(snapshot.observed_at),
        snapshot.source,
        snapshot.source_event_id,
        raw_response_id,
    )


def _snapshot_price_storage_values(
    snapshot: OddsSnapshot,
) -> tuple[tuple[PlayerId, str], ...]:
    return tuple(
        sorted(
            (
                price.player_id,
                str(price.decimal_odds),
            )
            for price in snapshot.prices
        )
    )


def _snapshots_from_query_rows(
    rows: list[sqlite3.Row | tuple[object, ...]],
) -> tuple[OddsSnapshot, ...]:
    grouped: dict[str, list[sqlite3.Row | tuple[object, ...]]] = {}
    for row in rows:
        snapshot_id = str(row[0])
        grouped.setdefault(snapshot_id, []).append(row)

    snapshots: list[OddsSnapshot] = []
    for snapshot_id, snapshot_rows in grouped.items():
        if len(snapshot_rows) != 2:
            raise StorageIntegrityError(
                f"stored snapshot {snapshot_id!r} must contain exactly two prices"
            )
        first = snapshot_rows[0]
        try:
            prices = tuple(
                MatchWinnerPrice(
                    player_id=int(str(row[6])),
                    decimal_odds=Decimal(str(row[7])),
                )
                for row in snapshot_rows
            )
            snapshots.append(
                OddsSnapshot(
                    snapshot_id=snapshot_id,
                    match_id=str(first[1]),
                    bookmaker_id=str(first[2]),
                    observed_at=_datetime_from_storage(
                        first[3],
                        "observed_at",
                    ),
                    source=str(first[4]),
                    source_event_id=str(first[5]),
                    prices=(prices[0], prices[1]),
                )
            )
        except (ValueError, ArithmeticError) as error:
            raise StorageIntegrityError(
                f"stored snapshot {snapshot_id!r} is invalid: {error}"
            ) from error
    return tuple(
        sorted(
            snapshots,
            key=lambda snapshot: (
                snapshot.bookmaker_id,
                snapshot.snapshot_id,
            ),
        )
    )


def _collapse_equivalent_latest_snapshots(
    snapshots: tuple[OddsSnapshot, ...],
) -> tuple[OddsSnapshot, ...]:
    by_bookmaker: dict[str, list[OddsSnapshot]] = {}
    for snapshot in snapshots:
        by_bookmaker.setdefault(snapshot.bookmaker_id, []).append(snapshot)

    selected: list[OddsSnapshot] = []
    for bookmaker_id, bookmaker_snapshots in sorted(by_bookmaker.items()):
        price_sets = {
            _snapshot_price_storage_values(snapshot)
            for snapshot in bookmaker_snapshots
        }
        if len(price_sets) > 1:
            snapshot_ids = sorted(
                snapshot.snapshot_id for snapshot in bookmaker_snapshots
            )
            observed_at = bookmaker_snapshots[0].observed_at.isoformat()
            raise StorageConflictError(
                f"bookmaker {bookmaker_id!r} has conflicting snapshots at "
                f"{observed_at}: {snapshot_ids!r}"
            )
        selected.append(
            min(
                bookmaker_snapshots,
                key=lambda snapshot: snapshot.snapshot_id,
            )
        )
    return tuple(selected)


def _datetime_to_storage(value: datetime) -> str:
    _require_utc_storage_time(value, "datetime")
    return value.isoformat(timespec="microseconds")


def _datetime_from_storage(value: object, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise StorageIntegrityError(
            f"stored {field_name} is not an ISO datetime"
        ) from error
    try:
        _require_utc_storage_time(parsed, field_name)
    except ValueError as error:
        raise StorageIntegrityError(str(error)) from error
    return parsed


def _require_utc_storage_time(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must be in UTC")
