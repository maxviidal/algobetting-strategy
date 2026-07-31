"""Resumable NBA collection and cached-dataset loading."""

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
    BasketballProviderError,
    BasketballRateLimitError,
    ProviderResponse,
    TheOddsApiHistoricalClient,
)

_BACKTEST_RESULTS_REQUEST_INTERVAL_SECONDS = 13.0
_BACKTEST_RATE_LIMIT_FALLBACK_SECONDS = 60.0
_BACKTEST_RATE_LIMIT_RETRIES = 5


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
    expected_credits: int
    missing_requests: int


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
    rows = _manifest_rows(
        tuple(item.game for item in result_games),
        settings=settings,
        cache_directory=cache_directory,
    )
    expected_credits = len(rows) * 10
    manifest_path = cache_directory / "quota_manifest.json"
    if manifest_path.exists():
        previous = _read_json(manifest_path).get("requests")
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
    manifest: dict[str, Any] = {
        "source": "the_odds_api",
        "sport_key": settings.sport_key,
        "region": settings.region,
        "market": settings.market_key,
        "credits_per_request": 10,
        "distinct_timestamps": len(rows),
        "expected_credits": expected_credits,
        "result_quarantines": result_quarantines,
        "requests": rows,
    }
    write_json_atomic(manifest_path, manifest)
    missing = sum(row["status"] not in {"success", "no_data"} for row in rows)
    result = QuotaManifest(manifest_path, len(rows), expected_credits, missing)
    if confirmed_credits is None:
        return result
    if confirmed_credits != expected_credits:
        raise ValueError(
            f"approval must equal manifest expected credits: {expected_credits}"
        )
    if odds_client is None:
        raise ValueError("ODDS_API_KEY is required after quota approval")
    for row in rows:
        if row["status"] in {"success", "no_data", "failure"}:
            continue
        query_at = _utc(str(row["query_at"]))
        path = cache_directory / str(row["cache_path"])
        try:
            response = odds_client.fetch_snapshot(
                sport_key=settings.sport_key,
                query_at=query_at,
                region=settings.region,
                market_key=settings.market_key,
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
            )
            data = root.get("data")
            row["status"] = "success" if isinstance(data, list) and data else "no_data"
            row["checksum"] = read_cached_response(path).checksum
        except BasketballProviderError as error:
            row["status"] = "failure"
            row["error"] = str(error)
        write_json_atomic(manifest_path, manifest)
    missing = sum(row["status"] not in {"success", "no_data"} for row in rows)
    return QuotaManifest(manifest_path, len(rows), expected_credits, missing)


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
    manifest = _read_json(cache_directory / "quota_manifest.json")
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
        requested_games = {
            str(game_id) for game_id in raw_row.get("game_ids", [])
        }
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
        rows.append(
            {
                "kind": kind,
                "query_at": query_at.isoformat(),
                "game_ids": sorted(game_ids),
                "cache_path": str(relative),
                "status": status,
                "checksum": checksum,
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
