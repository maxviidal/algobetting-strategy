"""Resumable NBA collection and cached-dataset loading."""

import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from basketball_value.cache import (
    read_cached_response,
    write_cached_response,
    write_json_atomic,
)
from basketball_value.config import BasketballSettings
from basketball_value.domain import Game, GameResult, MoneylineSnapshot
from basketball_value.normalization import (
    balldontlie_quarantines,
    normalize_balldontlie_games,
    normalize_odds_snapshot,
)
from basketball_value.providers import (
    BallDontLieClient,
    BasketballNetworkError,
    BasketballProviderError,
    BasketballRateLimitError,
    BasketballTransientProviderError,
    ProviderResponse,
    TheOddsApiHistoricalClient,
)

_BACKTEST_RESULTS_REQUEST_INTERVAL_SECONDS = 13.0
_BACKTEST_RATE_LIMIT_FALLBACK_SECONDS = 60.0
_BACKTEST_RATE_LIMIT_RETRIES = 5
_BACKTEST_ODDS_REQUEST_INTERVAL_SECONDS = 0.25
_BACKTEST_TRANSIENT_RETRY_SECONDS = (2.0, 5.0, 15.0, 30.0, 60.0)


@dataclass(slots=True)
class _RequestPacer:
    interval_seconds: float
    sleep: Callable[[float], None]
    clock: Callable[[], float]
    last_request_started_at: float | None = None

    def wait(self) -> None:
        """Wait until the backtest-only request interval has elapsed."""

        now = self.clock()
        if self.last_request_started_at is not None:
            remaining = self.interval_seconds - (now - self.last_request_started_at)
            if remaining > 0:
                self.sleep(remaining)
        self.last_request_started_at = self.clock()

    def reset(self) -> None:
        """Allow the next request immediately after an explicit retry wait."""

        self.last_request_started_at = None


@dataclass(frozen=True, slots=True)
class QuotaManifest:
    path: Path
    distinct_timestamps: int
    total_expected_credits: int
    expected_credits: int
    missing_requests: int
    cached_requests: int


@dataclass(frozen=True, slots=True)
class PreflightReport:
    manifest: QuotaManifest
    games: int
    result_quarantines: int
    available_credits: int | None
    credit_headroom: int | None
    results_key_present: bool
    odds_key_present: bool
    cache_writable: bool
    reports_writable: bool
    status: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BasketballDataset:
    games: tuple[Game, ...]
    results: dict[str, GameResult]
    entry_snapshots: tuple[MoneylineSnapshot, ...]
    closing_snapshots: tuple[MoneylineSnapshot, ...]
    requested_timestamps: int
    completed_timestamps: int
    unmatched_events: int
    matched_events: int
    matched_games: int
    quarantined_event_ids: tuple[str, ...]
    market_exclusions: tuple[str, ...]
    result_quarantines: tuple[str, ...]


def fetch_nba_history(
    *,
    settings: BasketballSettings,
    cache_directory: Path,
    results_client: BallDontLieClient,
    odds_client: TheOddsApiHistoricalClient | None,
    confirmed_credits: int | None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    results_request_interval_seconds: float = (
        _BACKTEST_RESULTS_REQUEST_INTERVAL_SECONDS
    ),
    odds_request_interval_seconds: float = (_BACKTEST_ODDS_REQUEST_INTERVAL_SECONDS),
    progress: Callable[[int, int, str], None] | None = None,
) -> QuotaManifest:
    """Cache schedules first, then fetch paid odds only after exact approval."""

    if results_request_interval_seconds < 0:
        raise ValueError("results_request_interval_seconds must not be negative")
    pacer = _RequestPacer(
        results_request_interval_seconds,
        sleep,
        monotonic,
    )
    pages = tuple(
        payload
        for season in settings.season_start_years
        for payload in _fetch_result_pages(
            season,
            cache_directory=cache_directory,
            client=results_client,
            pacer=pacer,
            sleep=sleep,
        )
    )
    result_games = normalize_balldontlie_games(pages)
    result_quarantines = balldontlie_quarantines(pages)
    result, manifest, rows = _prepare_manifest(
        tuple(item.game for item in result_games),
        result_quarantines=result_quarantines,
        settings=settings,
        cache_directory=cache_directory,
    )
    if confirmed_credits is None:
        return result
    if confirmed_credits != result.expected_credits:
        raise ValueError(
            "approval must equal manifest expected additional credits: "
            f"{result.expected_credits}"
        )
    if odds_client is None:
        raise ValueError("ODDS_API_KEY is required after quota approval")
    account = odds_client.fetch_account_status()
    available = _header_int(account, "x-requests-remaining")
    if available is not None and available < result.expected_credits:
        raise ValueError(
            f"insufficient Odds API credits: {available} available, "
            f"{result.expected_credits} required"
        )
    odds_pacer = _RequestPacer(
        odds_request_interval_seconds,
        sleep,
        monotonic,
    )
    pending_total = result.missing_requests
    completed_in_run = 0
    for row in rows:
        if row["status"] in {"success", "no_data"}:
            continue
        if available is not None and available < 10:
            write_json_atomic(result.path, manifest)
            raise ValueError(
                "Odds API reported fewer than 10 remaining credits; "
                "download stopped before the next historical request"
            )
        query_at = _utc(str(row["query_at"]))
        path = cache_directory / str(row["cache_path"])
        try:
            response = _fetch_odds_with_retry(
                odds_client,
                settings=settings,
                query_at=query_at,
                pacer=odds_pacer,
                sleep=sleep,
            )
            root = response.payload if isinstance(response.payload, dict) else {}
            provider_time = (
                str(root["timestamp"]) if root.get("timestamp") is not None else None
            )
            write_cached_response(
                path,
                payload_bytes=response.raw_bytes,
                source="the_odds_api",
                query_time=query_at,
                provider_snapshot_at=provider_time,
                response_headers=response.headers,
            )
            data = root.get("data")
            row["status"] = "success" if isinstance(data, list) and data else "no_data"
            row["checksum"] = read_cached_response(path).checksum
            row["credits_last"] = _header_int(response, "x-requests-last")
            row["credits_remaining"] = _header_int(response, "x-requests-remaining")
            row["credits_used"] = _header_int(response, "x-requests-used")
            reported_remaining = _header_int(response, "x-requests-remaining")
            if reported_remaining is not None:
                available = reported_remaining
            row.pop("error", None)
        except BasketballProviderError as error:
            row["status"] = "failure"
            row["error"] = str(error)
            completed_in_run += 1
            if progress is not None:
                progress(completed_in_run, pending_total, "failure")
            write_json_atomic(result.path, manifest)
            raise
        completed_in_run += 1
        if progress is not None:
            progress(completed_in_run, pending_total, str(row["status"]))
        if completed_in_run % 25 == 0 or completed_in_run == pending_total:
            write_json_atomic(result.path, manifest)
    missing = sum(row["status"] not in {"success", "no_data"} for row in rows)
    return QuotaManifest(
        result.path,
        len(rows),
        len(rows) * 10,
        missing * 10,
        missing,
        len(rows) - missing,
    )


def preflight_nba_history(
    *,
    settings: BasketballSettings,
    cache_directory: Path,
    reports_directory: Path,
    results_key_present: bool,
    odds_client: TheOddsApiHistoricalClient | None,
) -> PreflightReport:
    """Validate a historical run without making paid odds requests."""

    pages = _read_all_result_pages(settings, cache_directory)
    result_games = normalize_balldontlie_games(pages)
    quarantines = balldontlie_quarantines(pages)
    manifest, _, _ = _prepare_manifest(
        tuple(item.game for item in result_games),
        result_quarantines=quarantines,
        settings=settings,
        cache_directory=cache_directory,
    )
    available = None
    reasons: list[str] = []
    if not results_key_present:
        reasons.append("BALLDONTLIE_API_KEY is missing")
    if odds_client is None:
        reasons.append("ODDS_API_KEY is missing")
    else:
        try:
            available = _header_int(
                odds_client.fetch_account_status(), "x-requests-remaining"
            )
        except BasketballProviderError as error:
            reasons.append(f"Odds API account check failed: {error}")
        else:
            if available is None:
                reasons.append("Odds API did not report remaining credits")
    cache_writable = _directory_writable(cache_directory)
    reports_writable = _directory_writable(reports_directory)
    if not cache_writable:
        reasons.append("cache directory is not writable")
    if not reports_writable:
        reasons.append("reports directory is not writable")
    credit_headroom = (
        available - manifest.expected_credits if available is not None else None
    )
    if reasons:
        status = "NOT_READY"
    elif credit_headroom is not None and credit_headroom < 0:
        status = "READY_TO_PURCHASE"
    else:
        status = "READY_TO_DOWNLOAD"
    return PreflightReport(
        manifest=manifest,
        games=len(result_games),
        result_quarantines=len(quarantines),
        available_credits=available,
        credit_headroom=credit_headroom,
        results_key_present=results_key_present,
        odds_key_present=odds_client is not None,
        cache_writable=cache_writable,
        reports_writable=reports_writable,
        status=status,
        reasons=tuple(reasons),
    )


def _prepare_manifest(
    games: tuple[Game, ...],
    *,
    result_quarantines: tuple[str, ...],
    settings: BasketballSettings,
    cache_directory: Path,
) -> tuple[QuotaManifest, dict[str, Any], list[dict[str, Any]]]:
    rows = _manifest_rows(
        games,
        settings=settings,
        cache_directory=cache_directory,
    )
    path = _manifest_path(cache_directory, settings)
    if path.exists():
        previous = _read_json(path).get("requests")
        previous_rows = previous if isinstance(previous, list) else []
        previous_by_key = {
            (row.get("kind"), row.get("query_at")): row
            for row in previous_rows
            if isinstance(row, dict)
        }
        for row in rows:
            earlier = previous_by_key.get((row["kind"], row["query_at"]))
            if (
                isinstance(earlier, dict)
                and earlier.get("status") == "failure"
                and row["status"] == "pending"
            ):
                row["status"] = "failure"
                row["error"] = earlier.get("error")
    missing = sum(row["status"] not in {"success", "no_data"} for row in rows)
    manifest: dict[str, Any] = {
        "profile": settings.profile,
        "source": "the_odds_api",
        "sport_key": settings.sport_key,
        "region": settings.region,
        "market": settings.market_key,
        "credits_per_request": 10,
        "distinct_timestamps": len(rows),
        "total_expected_credits": len(rows) * 10,
        "expected_additional_credits": missing * 10,
        "cached_requests": len(rows) - missing,
        "pending_requests": missing,
        "result_quarantines": result_quarantines,
        "requests": rows,
    }
    write_json_atomic(path, manifest)
    return (
        QuotaManifest(
            path=path,
            distinct_timestamps=len(rows),
            total_expected_credits=len(rows) * 10,
            expected_credits=missing * 10,
            missing_requests=missing,
            cached_requests=len(rows) - missing,
        ),
        manifest,
        rows,
    )


def _manifest_path(cache_directory: Path, settings: BasketballSettings) -> Path:
    if settings.profile == "five_season_research":
        return cache_directory / "quota_manifest.json"
    return cache_directory / f"quota_manifest_{settings.profile}.json"


def _fetch_odds_with_retry(
    client: TheOddsApiHistoricalClient,
    *,
    settings: BasketballSettings,
    query_at: datetime,
    pacer: _RequestPacer,
    sleep: Callable[[float], None],
) -> ProviderResponse:
    for attempt in range(len(_BACKTEST_TRANSIENT_RETRY_SECONDS) + 1):
        pacer.wait()
        try:
            return client.fetch_snapshot(
                sport_key=settings.sport_key,
                query_at=query_at,
                region=settings.region,
                market_key=settings.market_key,
            )
        except BasketballRateLimitError as error:
            if attempt == len(_BACKTEST_TRANSIENT_RETRY_SECONDS):
                raise
            wait = (
                error.retry_after_seconds
                if error.retry_after_seconds is not None
                else _BACKTEST_RATE_LIMIT_FALLBACK_SECONDS
            )
        except (BasketballNetworkError, BasketballTransientProviderError):
            if attempt == len(_BACKTEST_TRANSIENT_RETRY_SECONDS):
                raise
            wait = _BACKTEST_TRANSIENT_RETRY_SECONDS[attempt]
        sleep(wait)
        pacer.reset()
    raise AssertionError("historical-odds retry loop did not return")


def _header_int(response: ProviderResponse, key: str) -> int | None:
    if response.headers is None:
        return None
    value = response.headers.get(key.casefold())
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _directory_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path):
            pass
    except OSError:
        return False
    return True


def load_cached_dataset(
    *,
    settings: BasketballSettings,
    cache_directory: Path,
) -> BasketballDataset:
    """Normalize an already-cached run without contacting either provider."""

    pages = _read_all_result_pages(settings, cache_directory)
    result_games = normalize_balldontlie_games(pages)
    result_quarantines = balldontlie_quarantines(pages)
    games = tuple(item.game for item in result_games)
    results = {item.game.game_id: item.result for item in result_games}
    manifest = _read_json(_manifest_path(cache_directory, settings))
    rows = manifest.get("requests")
    if not isinstance(rows, list):
        raise ValueError("quota manifest has no requests")
    entry: list[MoneylineSnapshot] = []
    closing: list[MoneylineSnapshot] = []
    unmatched: set[str] = set()
    matched: set[str] = set()
    matched_games: set[str] = set()
    exclusions: set[str] = set()
    completed = 0
    for raw_row in rows:
        if not isinstance(raw_row, dict) or raw_row.get("status") not in {
            "success",
            "no_data",
        }:
            continue
        completed += 1
        if raw_row["status"] == "no_data":
            continue
        cached = read_cached_response(cache_directory / str(raw_row["cache_path"]))
        query_at = _utc(str(raw_row["query_at"]))
        normalized = normalize_odds_snapshot(
            cached.payload,
            games,
            decision_at=query_at,
            maximum_age=timedelta(minutes=settings.maximum_quote_age_minutes),
        )
        requested_games = {str(game_id) for game_id in raw_row.get("game_ids", [])}
        selected = tuple(
            snapshot
            for snapshot in normalized.snapshots
            if snapshot.game_id in requested_games
        )
        unmatched.update(normalized.unmatched_event_ids)
        matched.update(normalized.matched_event_ids)
        matched_games.update(normalized.matched_game_ids)
        exclusions.update(normalized.excluded)
        if raw_row["kind"] == "entry":
            entry.extend(selected)
        else:
            closing.extend(selected)
    return BasketballDataset(
        games=games,
        results=results,
        entry_snapshots=tuple(entry),
        closing_snapshots=tuple(closing),
        requested_timestamps=len(rows),
        completed_timestamps=completed,
        unmatched_events=len(unmatched),
        matched_events=len(matched),
        matched_games=len(matched_games),
        quarantined_event_ids=tuple(sorted(unmatched)),
        market_exclusions=tuple(sorted(exclusions)),
        result_quarantines=result_quarantines,
    )


def _fetch_result_pages(
    season: int,
    *,
    cache_directory: Path,
    client: BallDontLieClient,
    pacer: _RequestPacer,
    sleep: Callable[[float], None],
) -> tuple[object, ...]:
    payloads: list[object] = []
    cursor: int | None = None
    page = 1
    while True:
        path = cache_directory / "balldontlie" / str(season) / f"page_{page}.json"
        if path.exists():
            cached = read_cached_response(path)
            payload = cached.payload
        else:
            response = _fetch_results_page_with_retry(
                client,
                season=season,
                cursor=cursor,
                pacer=pacer,
                sleep=sleep,
            )
            cached = write_cached_response(
                path,
                payload_bytes=response.raw_bytes,
                source="balldontlie",
                query_time=f"season={season};cursor={cursor}",
                provider_snapshot_at=None,
            )
            payload = cached.payload
        payloads.append(payload)
        meta = _dictionary(payload).get("meta")
        next_cursor = _dictionary(meta).get("next_cursor")
        if next_cursor is None:
            return tuple(payloads)
        cursor = int(next_cursor)
        page += 1


def _fetch_results_page_with_retry(
    client: BallDontLieClient,
    *,
    season: int,
    cursor: int | None,
    pacer: _RequestPacer,
    sleep: Callable[[float], None],
) -> ProviderResponse:
    for attempt in range(_BACKTEST_RATE_LIMIT_RETRIES + 1):
        pacer.wait()
        try:
            return client.fetch_games_page(season, cursor=cursor)
        except BasketballRateLimitError as error:
            if attempt == _BACKTEST_RATE_LIMIT_RETRIES:
                raise
            sleep(
                error.retry_after_seconds
                if error.retry_after_seconds is not None
                else _BACKTEST_RATE_LIMIT_FALLBACK_SECONDS
            )
            pacer.reset()
    raise AssertionError("rate-limit retry loop did not return")


def _read_all_result_pages(
    settings: BasketballSettings, cache_directory: Path
) -> tuple[object, ...]:
    payloads: list[object] = []
    for season in settings.season_start_years:
        paths = sorted(
            (cache_directory / "balldontlie" / str(season)).glob("page_*.json")
        )
        if not paths:
            raise FileNotFoundError(f"no cached BALLDONTLIE season {season}")
        payloads.extend(read_cached_response(path).payload for path in paths)
    return tuple(payloads)


def _manifest_rows(
    games: tuple[Game, ...],
    *,
    settings: BasketballSettings,
    cache_directory: Path,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, datetime], list[str]] = {}
    for game in games:
        for kind, minutes in (
            ("entry", settings.entry_minutes_before_tip),
            ("closing", settings.closing_minutes_before_tip),
        ):
            grouped.setdefault(
                (kind, game.scheduled_start - timedelta(minutes=minutes)), []
            ).append(game.game_id)
    rows: list[dict[str, Any]] = []
    for (kind, query_at), game_ids in sorted(
        grouped.items(), key=lambda value: value[0][1]
    ):
        token = query_at.strftime("%Y%m%dT%H%M%SZ")
        relative = Path("the_odds_api") / kind / f"{token}.json"
        status = "pending"
        checksum = None
        path = cache_directory / relative
        if path.exists():
            cached = read_cached_response(path)
            root = cached.payload if isinstance(cached.payload, dict) else {}
            data = root.get("data")
            status = "success" if isinstance(data, list) and data else "no_data"
            checksum = cached.checksum
            cached_headers = cached.response_headers or {}
        else:
            cached_headers = {}
        rows.append(
            {
                "kind": kind,
                "query_at": query_at.isoformat(),
                "game_ids": sorted(game_ids),
                "cache_path": str(relative),
                "status": status,
                "checksum": checksum,
                "credits_last": _optional_header_int(cached_headers, "x-requests-last"),
                "credits_remaining": _optional_header_int(
                    cached_headers, "x-requests-remaining"
                ),
                "credits_used": _optional_header_int(cached_headers, "x-requests-used"),
            }
        )
    return rows


def _read_json(path: Path) -> dict[str, Any]:
    value = __import__("json").loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _dictionary(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _optional_header_int(headers: dict[str, str], key: str) -> int | None:
    value = headers.get(key)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
