"""Resumable OddsPapi ATP Wimbledon 2026 backtest."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

from tennis_value.backtesting import (
    KellyBacktestReport,
    KellySettings,
    MatchResult,
    ResultStatus,
    backtest_kelly_candidates,
    select_best_candidates,
)
from tennis_value.config import AppSettings
from tennis_value.domain import Match, MatchWinnerPrice, OddsSnapshot
from tennis_value.odds_papi import OddsPapiClient, OddsPapiResponse
from tennis_value.signals import (
    InsufficientBookmakersError,
    OfferEvaluation,
    evaluate_market,
)

ATP_WIMBLEDON_TOURNAMENT_ID = 2555
WIMBLEDON_BOOKMAKERS = (
    "pinnacle",
    "bet365",
    "betano",
    "bwin",
    "betway",
    "coral",
    "ladbrokes",
    "leovegas",
    "unibet",
)
MATCH_WINNER_MARKET_ID = "121"
FIRST_PLAYER_OUTCOME_ID = "121"
SECOND_PLAYER_OUTCOME_ID = "122"
FIXTURE_FROM = datetime(2026, 6, 28, tzinfo=UTC)
FIXTURE_TO = datetime(2026, 7, 13, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class WimbledonFixture:
    """Provider fixture details required for evaluation and settlement."""

    fixture_id: str
    player_one_id: int
    player_two_id: int
    player_one_name: str
    player_two_name: str
    scheduled_start: datetime
    settled_at: datetime


@dataclass(frozen=True, slots=True)
class WimbledonBacktestRun:
    """Complete ATP run plus observable coverage counts."""

    fixture_count: int
    evaluated_matches: int
    skipped_matches: int
    offer_evaluations: int
    candidates: int
    report: KellyBacktestReport


def run_atp_wimbledon_backtest(
    client: OddsPapiClient,
    *,
    model_settings: AppSettings,
    cache_directory: Path,
    sleep: Callable[[float], None] = time.sleep,
    historical_cooldown_seconds: float = 5,
    settlement_cooldown_seconds: float = 1,
) -> WimbledonBacktestRun:
    """Download/cache provider inputs and run the 60-minute 25%-Kelly model."""

    fixtures_response = _cached_response(
        cache_directory / "fixtures.json",
        lambda: client.fetch_fixtures(
            tournament_id=ATP_WIMBLEDON_TOURNAMENT_ID,
            status_id=2,
            from_time=FIXTURE_FROM,
            to_time=FIXTURE_TO,
        ),
    )
    fixtures = parse_fixtures(fixtures_response.raw_bytes)
    evaluations: list[OfferEvaluation] = []
    results: dict[str, MatchResult] = {}
    evaluated_matches = 0
    skipped_matches = 0
    made_settlement_request = False
    made_historical_request = False

    for fixture in fixtures:
        settlement_path = cache_directory / "settlements" / f"{fixture.fixture_id}.json"
        if not settlement_path.exists() and made_settlement_request:
            sleep(settlement_cooldown_seconds)

        def fetch_settlement(
            fixture_id: str = fixture.fixture_id,
        ) -> OddsPapiResponse:
            return client.fetch_settlement(fixture_id)

        settlement = _cached_response(settlement_path, fetch_settlement)
        if not settlement.existed_before:
            made_settlement_request = True
        results[fixture.fixture_id] = parse_settlement(fixture, settlement.raw_bytes)

        history_payloads: list[bytes] = []
        for group_index, group in enumerate(_bookmaker_groups()):
            history_path = (
                cache_directory
                / "historical"
                / f"{fixture.fixture_id}_{group_index}.json"
            )
            if not history_path.exists() and made_historical_request:
                sleep(historical_cooldown_seconds)

            def fetch_history(
                fixture_id: str = fixture.fixture_id,
                books: tuple[str, ...] = group,
            ) -> OddsPapiResponse:
                return client.fetch_historical_odds_group(
                    fixture_id,
                    bookmakers=books,
                )

            history = _cached_response(history_path, fetch_history)
            if not history.existed_before:
                made_historical_request = True
            history_payloads.append(history.raw_bytes)

        decision_at = fixture.scheduled_start - timedelta(minutes=60)
        try:
            market_result = evaluate_market(
                match_for_fixture(fixture),
                snapshots_at_sixty_minutes(fixture, tuple(history_payloads)),
                decision_at=decision_at,
                calculated_at=decision_at,
                settings=model_settings,
            )
        except InsufficientBookmakersError:
            skipped_matches += 1
            continue
        evaluated_matches += 1
        evaluations.extend(market_result.evaluations)

    selected = select_best_candidates(evaluations, excluded_bookmakers=frozenset())
    report = backtest_kelly_candidates(
        selected,
        results,
        settings=KellySettings(
            initial_equity=Decimal("10000"),
            kelly_fraction=Decimal("0.25"),
        ),
    )
    return WimbledonBacktestRun(
        fixture_count=len(fixtures),
        evaluated_matches=evaluated_matches,
        skipped_matches=skipped_matches,
        offer_evaluations=len(evaluations),
        candidates=len(selected),
        report=report,
    )


@dataclass(frozen=True, slots=True)
class _CachedPayload:
    raw_bytes: bytes
    existed_before: bool


def _cached_response(
    path: Path,
    fetch: Callable[[], OddsPapiResponse],
) -> _CachedPayload:
    if path.exists():
        raw_bytes = path.read_bytes()
        _json(raw_bytes)
        return _CachedPayload(raw_bytes, True)
    response = fetch()
    _json(response.raw_bytes)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(response.raw_bytes)
    temporary.replace(path)
    return _CachedPayload(response.raw_bytes, False)


def parse_fixtures(raw_bytes: bytes) -> tuple[WimbledonFixture, ...]:
    payload = _json(raw_bytes)
    if not isinstance(payload, list):
        raise ValueError("OddsPapi fixtures response must be an array")
    fixtures = tuple(_fixture(item) for item in payload if isinstance(item, dict))
    if not fixtures:
        raise ValueError("OddsPapi returned no ATP Wimbledon 2026 fixtures")
    return tuple(
        sorted(fixtures, key=lambda item: (item.scheduled_start, item.fixture_id))
    )


def parse_settlement(
    fixture: WimbledonFixture,
    raw_bytes: bytes,
) -> MatchResult:
    payload = _json(raw_bytes)
    if not isinstance(payload, dict) or payload.get("fixtureId") != fixture.fixture_id:
        raise ValueError("settlement fixtureId does not match the requested fixture")
    market = _as_dict(_as_dict(payload.get("markets")).get(MATCH_WINNER_MARKET_ID))
    outcomes = _as_dict(market.get("outcomes"))
    first = _settlement_value(outcomes.get(FIRST_PLAYER_OUTCOME_ID))
    second = _settlement_value(outcomes.get(SECOND_PLAYER_OUTCOME_ID))
    if (first, second) == ("WIN", "LOSE"):
        winner = fixture.player_one_id
    elif (first, second) == ("LOSE", "WIN"):
        winner = fixture.player_two_id
    else:
        return MatchResult(
            fixture.fixture_id,
            ResultStatus.VOID,
            fixture.settled_at,
            "oddspapi:v4:settlements",
        )
    return MatchResult(
        fixture.fixture_id,
        ResultStatus.COMPLETED,
        fixture.settled_at,
        "oddspapi:v4:settlements",
        winner,
    )


def snapshots_at_sixty_minutes(
    fixture: WimbledonFixture,
    payloads: tuple[bytes, ...],
) -> tuple[OddsSnapshot, ...]:
    decision_at = fixture.scheduled_start - timedelta(minutes=60)
    snapshots: list[OddsSnapshot] = []
    for raw_bytes in payloads:
        payload = _json(raw_bytes)
        books = payload.get("bookmakers") if isinstance(payload, dict) else None
        if not isinstance(books, dict):
            continue
        for bookmaker_id, book_data in books.items():
            if not isinstance(bookmaker_id, str) or not isinstance(book_data, dict):
                continue
            market = _as_dict(book_data.get("markets")).get(MATCH_WINNER_MARKET_ID)
            outcomes = _as_dict(_as_dict(market).get("outcomes"))
            first = _latest_price(outcomes.get(FIRST_PLAYER_OUTCOME_ID), decision_at)
            second = _latest_price(outcomes.get(SECOND_PLAYER_OUTCOME_ID), decision_at)
            if first is None or second is None:
                continue
            odds_one, observed_one = first
            odds_two, observed_two = second
            observed_at = min(observed_one, observed_two)
            snapshots.append(
                OddsSnapshot(
                    snapshot_id=_snapshot_id(
                        fixture.fixture_id,
                        bookmaker_id,
                        observed_at,
                        odds_one,
                        odds_two,
                    ),
                    match_id=fixture.fixture_id,
                    bookmaker_id=bookmaker_id,
                    observed_at=observed_at,
                    prices=(
                        MatchWinnerPrice(fixture.player_one_id, odds_one),
                        MatchWinnerPrice(fixture.player_two_id, odds_two),
                    ),
                    source="oddspapi:v4:historical",
                    source_event_id=fixture.fixture_id,
                )
            )
    return tuple(sorted(snapshots, key=lambda item: item.bookmaker_id))


def match_for_fixture(fixture: WimbledonFixture) -> Match:
    return Match(
        fixture.fixture_id,
        "oddspapi:wimbledon-men-singles:2026",
        (fixture.player_one_id, fixture.player_two_id),
        fixture.scheduled_start,
    )


def _latest_price(
    outcome: object,
    decision_at: datetime,
) -> tuple[Decimal, datetime] | None:
    records = _as_dict(_as_dict(outcome).get("players")).get("0")
    if not isinstance(records, list):
        return None
    eligible: list[tuple[Decimal, datetime]] = []
    for record in records:
        if not isinstance(record, dict) or record.get("active") is not True:
            continue
        try:
            observed = _utc(str(record["createdAt"]))
            odds = Decimal(str(record["price"]))
        except (KeyError, ValueError):
            continue
        if observed <= decision_at and odds.is_finite() and odds > 1:
            eligible.append((odds, observed))
    return max(eligible, key=lambda item: item[1]) if eligible else None


def _fixture(value: dict[str, Any]) -> WimbledonFixture:
    if int(value.get("tournamentId", 0)) != ATP_WIMBLEDON_TOURNAMENT_ID:
        raise ValueError("fixture has an unexpected tournamentId")
    scheduled_start = _utc(str(value["startTime"]))
    settled_value = value.get("trueEndTime") or value.get("updatedAt")
    return WimbledonFixture(
        fixture_id=str(value["fixtureId"]),
        player_one_id=int(value["participant1Id"]),
        player_two_id=int(value["participant2Id"]),
        player_one_name=str(value["participant1Name"]),
        player_two_name=str(value["participant2Name"]),
        scheduled_start=scheduled_start,
        settled_at=_utc(str(settled_value)),
    )


def _settlement_value(outcome: object) -> str | None:
    player = _as_dict(_as_dict(outcome).get("players")).get("0")
    result = _as_dict(player).get("result")
    return result if isinstance(result, str) else None


def _bookmaker_groups() -> tuple[tuple[str, ...], ...]:
    return tuple(
        WIMBLEDON_BOOKMAKERS[index : index + 3]
        for index in range(0, len(WIMBLEDON_BOOKMAKERS), 3)
    )


def _snapshot_id(
    fixture_id: str,
    bookmaker_id: str,
    observed_at: datetime,
    odds_one: Decimal,
    odds_two: Decimal,
) -> str:
    value = (
        f"{fixture_id}|{bookmaker_id}|{observed_at.isoformat()}|{odds_one}|{odds_two}"
    )
    return sha256(value.encode()).hexdigest()


def _json(value: bytes) -> object:
    return json.loads(value)


def _as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("provider timestamp must be timezone-aware")
    return parsed.astimezone(UTC)
