"""Quota-gated, paced and resumable OddsPapi tennis research collection."""

import gzip
import json
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from tennis_value.data.odds_papi import (
    OddsPapiClient,
    OddsPapiNotFoundError,
    OddsPapiRateLimitError,
    OddsPapiResponse,
)
from tennis_value.research_config import TennisResearchSettings, TournamentSpec

_MAX_RATE_LIMIT_RETRIES = 5


class TennisResearchCollectionError(RuntimeError):
    """Raised when the research collection cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class AccountQuota:
    request_limit: int
    request_count: int

    @property
    def remaining(self) -> int:
        return self.request_limit - self.request_count


@dataclass(frozen=True, slots=True)
class TennisHistoryPreflight:
    status: str
    quota: AccountQuota
    missing_billable_requests: int
    quota_reserve: int
    missing_catalog: bool
    missing_fixture_requests: int
    known_fixtures: int
    missing_historical_requests: int


@dataclass(frozen=True, slots=True)
class ResearchFixture:
    fixture_id: str
    tournament_key: str
    tour: str
    surface: str
    tournament_id: int
    tournament_name: str
    player_one_id: int
    player_two_id: int
    player_one_name: str
    player_two_name: str
    scheduled_start: datetime


@dataclass(frozen=True, slots=True)
class TennisHistoryCollection:
    fixtures: tuple[ResearchFixture, ...]
    billable_requests_made: int
    historical_requests_made: int
    no_data_responses: int
    cache_directory: Path
    manifest_path: Path


class EndpointRateLimiter:
    """Enforce provider cooldowns using a monotonic clock."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sleep = sleep
        self._clock = clock
        self._last_request: dict[str, float] = {}

    def wait(self, endpoint: str, cooldown_seconds: float) -> None:
        previous = self._last_request.get(endpoint)
        now = self._clock()
        if previous is not None:
            remaining = cooldown_seconds - (now - previous)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last_request[endpoint] = now


def preflight_tennis_history(
    client: OddsPapiClient,
    *,
    settings: TennisResearchSettings,
    cache_directory: Path,
) -> TennisHistoryPreflight:
    """Check quota and exact cached billable work without billable API calls."""

    quota = parse_account_quota(client.fetch_account().raw_bytes)
    catalog_path = cache_directory / "tournaments.json.gz"
    missing_catalog = not catalog_path.is_file()
    if not missing_catalog:
        _read_gzip_json(catalog_path)
    missing_fixtures = sum(
        _cached_fixture_bytes(cache_directory, spec.key) is None
        for spec in settings.tournaments
    )
    billable = int(missing_catalog) + missing_fixtures
    known_fixtures = 0
    missing_historical = 0
    for spec in settings.tournaments:
        fixture_bytes = _cached_fixture_bytes(cache_directory, spec.key)
        if fixture_bytes is None:
            continue
        fixtures = parse_research_fixtures(
            fixture_bytes,
            spec=spec,
            sport_id=settings.sport_id,
        )
        known_fixtures += len(fixtures)
        for fixture in fixtures:
            for group_index, _ in enumerate(_bookmaker_groups(settings.bookmakers)):
                history_path = _history_path(
                    cache_directory, fixture.fixture_id, group_index
                )
                if history_path.is_file():
                    _read_gzip_json(history_path)
                else:
                    missing_historical += 1
    status = (
        "READY_TO_DOWNLOAD"
        if quota.remaining - billable >= settings.quota_reserve
        else "BLOCKED_BY_QUOTA"
    )
    return TennisHistoryPreflight(
        status=status,
        quota=quota,
        missing_billable_requests=billable,
        quota_reserve=settings.quota_reserve,
        missing_catalog=missing_catalog,
        missing_fixture_requests=missing_fixtures,
        known_fixtures=known_fixtures,
        missing_historical_requests=missing_historical,
    )


def fetch_tennis_history(
    client: OddsPapiClient,
    *,
    settings: TennisResearchSettings,
    cache_directory: Path,
    confirm_billable_requests: int,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> TennisHistoryCollection:
    """Fetch all missing catalog, fixture and historical responses safely."""

    preflight = preflight_tennis_history(
        client,
        settings=settings,
        cache_directory=cache_directory,
    )
    if preflight.status != "READY_TO_DOWNLOAD":
        raise TennisResearchCollectionError(
            "OddsPapi quota is insufficient after preserving the configured reserve"
        )
    if confirm_billable_requests != preflight.missing_billable_requests:
        raise TennisResearchCollectionError(
            "--confirm-billable-requests must equal the current exact cost "
            f"{preflight.missing_billable_requests}"
        )
    limiter = EndpointRateLimiter(sleep=sleep, clock=clock)
    billable_made = 0
    historical_made = 0
    no_data_responses = 0
    response_metadata: list[dict[str, Any]] = []
    catalog_path = cache_directory / "tournaments.json.gz"
    if catalog_path.is_file():
        catalog_bytes = _read_gzip_json(catalog_path)
    else:
        response = _paced_fetch(
            endpoint="/tournaments",
            cooldown_seconds=1.1,
            limiter=limiter,
            fetch=lambda: client.list_tournaments(sport_id=settings.sport_id),
            sleep=sleep,
        )
        catalog_bytes = response.raw_bytes
        _write_gzip_json(catalog_path, catalog_bytes)
        response_metadata.append(_response_metadata(response, catalog_path))
        billable_made += 1
    tournament_ids = resolve_tournaments(catalog_bytes, settings.tournaments)
    fixtures: list[ResearchFixture] = []
    for spec in settings.tournaments:
        fixture_path = _fixture_path(cache_directory, spec.key)
        fixture_bytes = _cached_fixture_bytes(cache_directory, spec.key)
        if fixture_bytes is None:

            def fetch_fixture(specification: TournamentSpec = spec) -> OddsPapiResponse:
                return client.fetch_fixtures(
                    tournament_id=tournament_ids[specification.key],
                    status_id=2,
                    from_time=specification.from_time,
                    to_time=specification.to_time,
                )

            try:
                response = _paced_fetch(
                    endpoint="/fixtures",
                    cooldown_seconds=settings.fixture_cooldown_seconds,
                    limiter=limiter,
                    fetch=fetch_fixture,
                    sleep=sleep,
                )
                fixture_bytes = response.raw_bytes
                response_path = fixture_path
                response_status = "success"
            except OddsPapiNotFoundError as error:
                response = OddsPapiResponse(
                    raw_bytes=error.raw_bytes,
                    collected_at=datetime.now(UTC),
                    endpoint="/fixtures",
                )
                fixture_bytes = b"[]"
                response_path = _fixture_no_data_path(cache_directory, spec.key)
                response_status = "no_data_http_404"
                no_data_responses += 1
            _write_gzip_json(response_path, response.raw_bytes)
            response_metadata.append(
                _response_metadata(
                    response,
                    response_path,
                    status=response_status,
                )
            )
            billable_made += 1
        fixtures.extend(
            parse_research_fixtures(
                fixture_bytes,
                spec=spec,
                sport_id=settings.sport_id,
            )
        )
    unique = {fixture.fixture_id: fixture for fixture in fixtures}
    ordered = tuple(
        sorted(
            unique.values(), key=lambda value: (value.scheduled_start, value.fixture_id)
        )
    )
    groups = _bookmaker_groups(settings.bookmakers)
    for fixture in ordered:
        for group_index, group in enumerate(groups):
            history_path = _history_path(
                cache_directory, fixture.fixture_id, group_index
            )
            if history_path.is_file():
                _read_gzip_json(history_path)
                continue

            def fetch_history(
                current_fixture: ResearchFixture = fixture,
                bookmaker_group: tuple[str, ...] = group,
            ) -> OddsPapiResponse:
                return client.fetch_historical_odds_group(
                    current_fixture.fixture_id,
                    bookmakers=bookmaker_group,
                )

            try:
                response = _paced_fetch(
                    endpoint="/historical-odds",
                    cooldown_seconds=settings.historical_cooldown_seconds,
                    limiter=limiter,
                    fetch=fetch_history,
                    sleep=sleep,
                )
                response_status = "success"
            except OddsPapiNotFoundError as error:
                response = OddsPapiResponse(
                    raw_bytes=error.raw_bytes,
                    collected_at=datetime.now(UTC),
                    endpoint="/historical-odds",
                )
                response_status = "no_data_http_404"
                no_data_responses += 1
            _write_gzip_json(history_path, response.raw_bytes)
            response_metadata.append(
                _response_metadata(
                    response,
                    history_path,
                    status=response_status,
                )
            )
            historical_made += 1
    manifest_path = cache_directory / "collection_manifest.json"
    manifest = {
        "profile": settings.profile,
        "generated_at": datetime.now(UTC).isoformat(),
        "bookmakers": settings.bookmakers,
        "minimum_bookmakers": settings.minimum_bookmakers,
        "fixture_count": len(ordered),
        "billable_requests_made": billable_made,
        "historical_requests_made": historical_made,
        "no_data_responses": no_data_responses,
        "quota_before": {
            "request_limit": preflight.quota.request_limit,
            "request_count": preflight.quota.request_count,
            "reserve": settings.quota_reserve,
        },
        "fixtures": [
            {
                "fixture_id": fixture.fixture_id,
                "tournament_key": fixture.tournament_key,
                "tour": fixture.tour,
                "scheduled_start": fixture.scheduled_start.isoformat(),
            }
            for fixture in ordered
        ],
        "new_responses": response_metadata,
        "cache_inventory": _cache_inventory(cache_directory),
    }
    _atomic_text(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return TennisHistoryCollection(
        fixtures=ordered,
        billable_requests_made=billable_made,
        historical_requests_made=historical_made,
        no_data_responses=no_data_responses,
        cache_directory=cache_directory,
        manifest_path=manifest_path,
    )


def load_cached_research_fixtures(
    *,
    settings: TennisResearchSettings,
    cache_directory: Path,
) -> tuple[ResearchFixture, ...]:
    """Load and validate every configured cached fixture response."""

    fixtures = tuple(
        fixture
        for spec in settings.tournaments
        for fixture in parse_research_fixtures(
            _required_cached_fixture_bytes(cache_directory, spec.key),
            spec=spec,
            sport_id=settings.sport_id,
        )
    )
    unique = {fixture.fixture_id: fixture for fixture in fixtures}
    return tuple(
        sorted(
            unique.values(), key=lambda value: (value.scheduled_start, value.fixture_id)
        )
    )


def load_cached_histories(
    fixture_id: str,
    *,
    settings: TennisResearchSettings,
    cache_directory: Path,
) -> tuple[bytes, ...]:
    """Load every required history group for one fixture."""

    return tuple(
        _read_gzip_json(_history_path(cache_directory, fixture_id, index))
        for index, _ in enumerate(_bookmaker_groups(settings.bookmakers))
    )


def parse_account_quota(raw_bytes: bytes) -> AccountQuota:
    """Extract only non-secret active quota fields from an account response."""

    payload = _json(raw_bytes)
    if not isinstance(payload, dict):
        raise TennisResearchCollectionError(
            "OddsPapi account response must be an object"
        )
    subscription_id = payload.get("current_subscription_id")
    subscriptions = payload.get("subscriptions")
    if not isinstance(subscriptions, list):
        raise TennisResearchCollectionError("account subscriptions must be an array")
    active = [
        value
        for value in subscriptions
        if isinstance(value, dict)
        and value.get("is_active") is True
        and (subscription_id is None or value.get("subscription_id") == subscription_id)
    ]
    if len(active) != 1:
        raise TennisResearchCollectionError(
            "account must contain one active subscription"
        )
    limit = active[0].get("request_limit")
    count = active[0].get("request_count")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or limit < 0
        or count < 0
    ):
        raise TennisResearchCollectionError(
            "account quota fields must be non-negative integers"
        )
    return AccountQuota(limit, count)


def resolve_tournaments(
    raw_bytes: bytes,
    specs: tuple[TournamentSpec, ...],
) -> dict[str, int]:
    """Resolve each configured event without silently accepting ambiguity."""

    payload = _json(raw_bytes)
    if not isinstance(payload, list):
        raise TennisResearchCollectionError("tournament catalog must be an array")
    resolved: dict[str, int] = {}
    for spec in specs:
        matches: list[int] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            category = item.get("categorySlug")
            name = item.get("tournamentName")
            tournament_id = item.get("tournamentId")
            if (
                category != spec.category_slug
                or not isinstance(name, str)
                or isinstance(tournament_id, bool)
                or not isinstance(tournament_id, int)
                or tournament_id <= 0
            ):
                continue
            normalized = _normalize_text(name)
            singles_marker = "men singles" if spec.tour == "ATP" else "women singles"
            if singles_marker not in normalized:
                continue
            if all(_normalize_text(token) in normalized for token in spec.name_tokens):
                matches.append(tournament_id)
        if len(matches) != 1:
            raise TennisResearchCollectionError(
                f"{spec.key} resolved to {len(matches)} tournament IDs; "
                "expected exactly one"
            )
        resolved[spec.key] = matches[0]
    return resolved


def parse_research_fixtures(
    raw_bytes: bytes,
    *,
    spec: TournamentSpec,
    sport_id: int,
) -> tuple[ResearchFixture, ...]:
    payload = _json(raw_bytes)
    if not isinstance(payload, list):
        raise TennisResearchCollectionError(f"{spec.key} fixtures must be an array")
    fixtures: list[ResearchFixture] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise TennisResearchCollectionError(
                f"{spec.key}[{index}] must be an object"
            )
        if item.get("statusId") != 2 or item.get("sportId") != sport_id:
            continue
        scheduled = _provider_datetime(item.get("startTime"), "startTime")
        if not spec.from_time <= scheduled < spec.to_time:
            continue
        fixture_id = _provider_string(item.get("fixtureId"), "fixtureId")
        tournament_id = _provider_int(item.get("tournamentId"), "tournamentId")
        fixtures.append(
            ResearchFixture(
                fixture_id=fixture_id,
                tournament_key=spec.key,
                tour=spec.tour,
                surface=spec.surface,
                tournament_id=tournament_id,
                tournament_name=_provider_string(
                    item.get("tournamentName"), "tournamentName"
                ),
                player_one_id=_provider_int(
                    item.get("participant1Id"), "participant1Id"
                ),
                player_two_id=_provider_int(
                    item.get("participant2Id"), "participant2Id"
                ),
                player_one_name=_provider_string(
                    item.get("participant1Name"), "participant1Name"
                ),
                player_two_name=_provider_string(
                    item.get("participant2Name"), "participant2Name"
                ),
                scheduled_start=scheduled,
            )
        )
    return tuple(
        sorted(fixtures, key=lambda value: (value.scheduled_start, value.fixture_id))
    )


def _paced_fetch(
    *,
    endpoint: str,
    cooldown_seconds: float,
    limiter: EndpointRateLimiter,
    fetch: Callable[[], OddsPapiResponse],
    sleep: Callable[[float], None],
) -> OddsPapiResponse:
    for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
        limiter.wait(endpoint, cooldown_seconds)
        try:
            return fetch()
        except OddsPapiRateLimitError as error:
            if attempt == _MAX_RATE_LIMIT_RETRIES:
                raise
            sleep(max(cooldown_seconds, error.retry_after_seconds or cooldown_seconds))
    raise AssertionError("rate-limit retry loop did not return")


def _write_gzip_json(path: Path, raw_bytes: bytes) -> None:
    _json(raw_bytes)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(gzip.compress(raw_bytes, compresslevel=6, mtime=0))
    temporary.replace(path)


def _read_gzip_json(path: Path) -> bytes:
    if not path.is_file():
        raise FileNotFoundError(f"required cached response is missing: {path}")
    try:
        raw_bytes = gzip.decompress(path.read_bytes())
    except (OSError, EOFError) as error:
        raise TennisResearchCollectionError(f"invalid gzip cache: {path}") from error
    _json(raw_bytes)
    return raw_bytes


def _history_path(cache_directory: Path, fixture_id: str, group_index: int) -> Path:
    return cache_directory / "historical" / f"{fixture_id}_{group_index}.json.gz"


def _fixture_path(cache_directory: Path, tournament_key: str) -> Path:
    return cache_directory / "fixtures" / f"{tournament_key}.json.gz"


def _fixture_no_data_path(cache_directory: Path, tournament_key: str) -> Path:
    return cache_directory / "fixtures" / f"{tournament_key}.404.json.gz"


def _cached_fixture_bytes(
    cache_directory: Path, tournament_key: str
) -> bytes | None:
    fixture_path = _fixture_path(cache_directory, tournament_key)
    if fixture_path.is_file():
        return _read_gzip_json(fixture_path)
    no_data_path = _fixture_no_data_path(cache_directory, tournament_key)
    if no_data_path.is_file():
        _read_gzip_json(no_data_path)
        return b"[]"
    return None


def _required_cached_fixture_bytes(
    cache_directory: Path, tournament_key: str
) -> bytes:
    fixture_bytes = _cached_fixture_bytes(cache_directory, tournament_key)
    if fixture_bytes is None:
        raise FileNotFoundError(
            "required cached fixture response is missing: "
            f"{_fixture_path(cache_directory, tournament_key)}"
        )
    return fixture_bytes


def _bookmaker_groups(values: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(values[index : index + 3]) for index in range(0, len(values), 3))


def _response_metadata(
    response: OddsPapiResponse,
    path: Path,
    *,
    status: str = "success",
) -> dict[str, Any]:
    return {
        "status": status,
        "endpoint": response.endpoint,
        "collected_at": response.collected_at.isoformat(),
        "path": str(path),
        "sha256": sha256(response.raw_bytes).hexdigest(),
        "uncompressed_bytes": len(response.raw_bytes),
    }


def _cache_inventory(cache_directory: Path) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for path in sorted(cache_directory.rglob("*.json.gz")):
        raw_bytes = _read_gzip_json(path)
        inventory.append(
            {
                "path": str(path.relative_to(cache_directory)),
                "sha256": sha256(raw_bytes).hexdigest(),
                "compressed_sha256": sha256(path.read_bytes()).hexdigest(),
                "uncompressed_bytes": len(raw_bytes),
            }
        )
    return inventory


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _json(raw_bytes: bytes) -> Any:
    try:
        return json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise TennisResearchCollectionError(
            "provider response is not valid JSON"
        ) from error


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return " ".join(
        "".join(
            character
            for character in decomposed
            if character.isalnum() or character.isspace()
        )
        .casefold()
        .split()
    )


def _provider_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TennisResearchCollectionError(
            f"fixture {field} must be a non-empty string"
        )
    return value.strip()


def _provider_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TennisResearchCollectionError(
            f"fixture {field} must be a positive integer"
        )
    return value


def _provider_datetime(value: object, field: str) -> datetime:
    raw = _provider_string(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise TennisResearchCollectionError(
            f"fixture {field} must be ISO 8601"
        ) from error
    if parsed.tzinfo is None:
        raise TennisResearchCollectionError(f"fixture {field} must be timezone-aware")
    return parsed.astimezone(UTC)
