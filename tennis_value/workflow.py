"""Application workflows connecting stored provider data to domain models."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from tennis_value.config import AppSettings
from tennis_value.domain import Bookmaker, Match, Player, Tournament
from tennis_value.entity_resolution import (
    THE_ODDS_API_PROVIDER,
    EntityResolutionError,
    normalized_name,
)
from tennis_value.ingestion import (
    IngestedOddsApiResponse,
    JsonValue,
    ingest_odds_api_json,
)
from tennis_value.normalization import OddsApiNormalizer
from tennis_value.signals import (
    InsufficientBookmakersError,
    MarketEvaluationResult,
    evaluate_market,
)
from tennis_value.storage import (
    SqliteOddsRepository,
    SqlitePlayerRegistry,
    StoredRawOddsResponse,
    UnresolvedPlayerName,
)


class WorkflowError(ValueError):
    """Raised when an application workflow cannot proceed safely."""


class PendingPlayersError(WorkflowError):
    """Raised when normalization needs explicit player approval."""

    def __init__(self, pending: tuple[UnresolvedPlayerName, ...]) -> None:
        self.pending = pending
        names = ", ".join(item.raw_name for item in pending)
        super().__init__(f"player approval required for: {names}")


@dataclass(frozen=True, slots=True)
class NormalizationSummary:
    """Counts and provenance from one stored-response normalization run."""

    response_id: str
    match_count: int
    snapshot_count: int


@dataclass(frozen=True, slots=True)
class EvaluatedStoredMatch:
    """One stored match and its complete market evaluation."""

    match: Match
    result: MarketEvaluationResult


@dataclass(frozen=True, slots=True)
class SkippedStoredMatch:
    """One stored match that was not eligible for evaluation."""

    match: Match
    reason: str


@dataclass(frozen=True, slots=True)
class StoredEvaluationBatch:
    """Point-in-time evaluations for every match in a raw response."""

    response_id: str
    decision_at: datetime
    evaluated: tuple[EvaluatedStoredMatch, ...]
    skipped: tuple[SkippedStoredMatch, ...]


def select_raw_response(
    repository: SqliteOddsRepository,
    response_id: str,
) -> StoredRawOddsResponse:
    """Resolve an explicit response ID or the special ``latest`` selector."""

    if response_id == "latest":
        return repository.get_latest_raw_response()
    return repository.get_raw_response(response_id)


def restore_ingested_response(
    stored: StoredRawOddsResponse,
) -> IngestedOddsApiResponse:
    """Rebuild the minimally validated ingestion record from exact raw bytes."""

    return ingest_odds_api_json(
        stored.raw_bytes,
        collected_at=stored.collected_at,
        source=stored.source,
    )


def scan_pending_players(
    registry: SqlitePlayerRegistry,
    response: IngestedOddsApiResponse,
) -> tuple[UnresolvedPlayerName, ...]:
    """Resolve all provider names and return those requiring human approval."""

    discovered_names = _discover_player_names(response)
    for raw_name in discovered_names:
        try:
            registry.resolve(
                provider=THE_ODDS_API_PROVIDER,
                raw_name=raw_name,
            )
        except EntityResolutionError:
            continue

    discovered_keys = {normalized_name(name) for name in discovered_names}
    return tuple(
        item
        for item in registry.list_unresolved()
        if item.provider == THE_ODDS_API_PROVIDER
        and item.normalized_name in discovered_keys
    )


def approve_pending_players(
    registry: SqlitePlayerRegistry,
    pending: tuple[UnresolvedPlayerName, ...],
    *,
    names: tuple[str, ...],
) -> tuple[Player, ...]:
    """Create explicitly selected unknown players and approve provider aliases."""

    pending_by_name = {item.normalized_name: item for item in pending}
    approved: list[Player] = []
    approved_name_keys: set[str] = set()
    for requested_name in names:
        name_key = normalized_name(requested_name)
        if name_key in approved_name_keys:
            continue
        item = pending_by_name.get(name_key)
        if item is None:
            raise WorkflowError(
                f"player name {requested_name!r} is not pending approval"
            )
        if item.reason != "unknown":
            raise WorkflowError(
                f"player name {requested_name!r} is {item.reason}; "
                "approve an alias to an existing player instead"
            )
        player = registry.add_player(item.raw_name)
        registry.add_alias(
            provider=THE_ODDS_API_PROVIDER,
            raw_name=item.raw_name,
            player_id=player.player_id,
        )
        registry.resolve(
            provider=THE_ODDS_API_PROVIDER,
            raw_name=item.raw_name,
        )
        approved.append(player)
        approved_name_keys.add(name_key)
    return tuple(approved)


def normalize_stored_response(
    connection: sqlite3.Connection,
    *,
    response_id: str = "latest",
) -> NormalizationSummary:
    """Normalize and persist every eligible event in one raw response."""

    repository = SqliteOddsRepository(connection)
    registry = SqlitePlayerRegistry(connection)
    stored = select_raw_response(repository, response_id)
    response = restore_ingested_response(stored)
    pending = scan_pending_players(registry, response)
    if pending:
        raise PendingPlayersError(pending)

    normalizer = _normalizer_for_response(response, registry)
    events = normalizer.normalize(response)
    snapshot_count = 0
    for event in events:
        repository.save_match(event.match)
        repository.link_match_to_raw_response(
            event.match.match_id,
            stored.response_id,
        )
        repository.save_snapshots(
            event.snapshots,
            raw_response_id=stored.response_id,
        )
        snapshot_count += len(event.snapshots)
    return NormalizationSummary(
        response_id=stored.response_id,
        match_count=len(events),
        snapshot_count=snapshot_count,
    )


def evaluate_stored_response(
    connection: sqlite3.Connection,
    *,
    settings: AppSettings,
    response_id: str = "latest",
    decision_at: datetime | None = None,
    calculated_at: datetime | None = None,
) -> StoredEvaluationBatch:
    """Evaluate all normalized matches linked to one stored response."""

    repository = SqliteOddsRepository(connection)
    stored = select_raw_response(repository, response_id)
    effective_decision_at = decision_at or stored.collected_at
    effective_calculated_at = calculated_at or datetime.now(UTC)
    evaluated: list[EvaluatedStoredMatch] = []
    skipped: list[SkippedStoredMatch] = []

    for match_id in repository.match_ids_for_raw_response(stored.response_id):
        match = repository.get_match(match_id)
        if effective_decision_at >= match.scheduled_start:
            skipped.append(
                SkippedStoredMatch(
                    match=match,
                    reason="decision time is not before the scheduled start",
                )
            )
            continue
        snapshots = repository.latest_snapshots_as_of(
            match.match_id,
            decision_at=effective_decision_at,
            maximum_age=timedelta(
                seconds=settings.collection.maximum_quote_age_seconds
            ),
        )
        try:
            result = evaluate_market(
                match,
                snapshots,
                decision_at=effective_decision_at,
                calculated_at=effective_calculated_at,
                settings=settings,
            )
        except InsufficientBookmakersError as error:
            skipped.append(
                SkippedStoredMatch(match=match, reason=str(error))
            )
            continue
        evaluated.append(EvaluatedStoredMatch(match=match, result=result))

    return StoredEvaluationBatch(
        response_id=stored.response_id,
        decision_at=effective_decision_at,
        evaluated=tuple(evaluated),
        skipped=tuple(skipped),
    )


def _discover_player_names(
    response: IngestedOddsApiResponse,
) -> tuple[str, ...]:
    names: set[str] = set()
    for event_index, event in enumerate(response.events):
        context = f"event[{event_index}]"
        names.add(_required_json_string(event.get("home_team"), f"{context}.home_team"))
        names.add(_required_json_string(event.get("away_team"), f"{context}.away_team"))
        bookmakers = event.get("bookmakers")
        if not isinstance(bookmakers, list):
            raise WorkflowError(f"{context}.bookmakers must be an array")
        for bookmaker_index, bookmaker in enumerate(bookmakers):
            if not isinstance(bookmaker, dict):
                raise WorkflowError(
                    f"{context}.bookmakers[{bookmaker_index}] must be an object"
                )
            markets = bookmaker.get("markets")
            if not isinstance(markets, list):
                raise WorkflowError(
                    f"{context}.bookmakers[{bookmaker_index}].markets "
                    "must be an array"
                )
            for market in markets:
                if not isinstance(market, dict) or market.get("key") != "h2h":
                    continue
                outcomes = market.get("outcomes")
                if not isinstance(outcomes, list):
                    raise WorkflowError(f"{context} h2h outcomes must be an array")
                for outcome in outcomes:
                    if not isinstance(outcome, dict):
                        raise WorkflowError(
                            f"{context} h2h outcome must be an object"
                        )
                    names.add(
                        _required_json_string(
                            outcome.get("name"),
                            f"{context}.h2h.outcome.name",
                        )
                    )
    return tuple(sorted(names, key=lambda name: (normalized_name(name), name)))


def _normalizer_for_response(
    response: IngestedOddsApiResponse,
    registry: SqlitePlayerRegistry,
) -> OddsApiNormalizer:
    tournament_titles: dict[str, str] = {}
    bookmaker_titles: dict[str, str] = {}
    for event_index, event in enumerate(response.events):
        context = f"event[{event_index}]"
        sport_key = _required_json_string(
            event.get("sport_key"),
            f"{context}.sport_key",
        )
        sport_title = _required_json_string(
            event.get("sport_title"),
            f"{context}.sport_title",
        )
        _add_consistent_title(
            tournament_titles,
            sport_key,
            sport_title,
            "tournament",
        )
        bookmakers = event.get("bookmakers")
        if not isinstance(bookmakers, list):
            raise WorkflowError(f"{context}.bookmakers must be an array")
        for bookmaker_index, bookmaker in enumerate(bookmakers):
            bookmaker_context = f"{context}.bookmakers[{bookmaker_index}]"
            if not isinstance(bookmaker, dict):
                raise WorkflowError(f"{bookmaker_context} must be an object")
            bookmaker_key = _required_json_string(
                bookmaker.get("key"),
                f"{bookmaker_context}.key",
            )
            bookmaker_title = _required_json_string(
                bookmaker.get("title"),
                f"{bookmaker_context}.title",
            )
            _add_consistent_title(
                bookmaker_titles,
                bookmaker_key,
                bookmaker_title,
                "bookmaker",
            )

    return OddsApiNormalizer(
        player_resolver=registry,
        tournaments=tuple(
            Tournament(tournament_id=key, display_name=title)
            for key, title in sorted(tournament_titles.items())
        ),
        bookmakers=tuple(
            Bookmaker(bookmaker_id=key, display_name=title)
            for key, title in sorted(bookmaker_titles.items())
        ),
        tournament_aliases={key: key for key in tournament_titles},
        bookmaker_aliases={key: key for key in bookmaker_titles},
    )


def _add_consistent_title(
    titles: dict[str, str],
    key: str,
    title: str,
    entity_type: str,
) -> None:
    existing = titles.get(key)
    if existing is not None and existing != title:
        raise WorkflowError(
            f"{entity_type} key {key!r} has conflicting titles "
            f"{existing!r} and {title!r}"
        )
    titles[key] = title


def _required_json_string(value: JsonValue | None, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{field_name} must be a non-empty string")
    return value
