"""Resumable OddsPapi ATP Wimbledon 2026 backtest."""

import csv
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

from betting_core import QuoteRecord, select_latest_record
from tennis_value.backtesting import (
    KellyBacktestReport,
    KellySettings,
    KellySettledBet,
    MatchResult,
    ResultStatus,
    backtest_kelly_candidates,
    select_best_candidates,
)
from tennis_value.config import AppSettings
from tennis_value.data.domain import Match, MatchWinnerPrice, OddsSnapshot
from tennis_value.data.odds_papi import OddsPapiClient, OddsPapiResponse
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


@dataclass(frozen=True, slots=True)
class WimbledonCsvExport:
    """Paths and coverage counts for locally generated research exports."""

    matches_path: Path
    offers_path: Path
    match_rows: int
    offer_rows: int


type EvaluatedFixture = tuple[
    WimbledonFixture,
    MatchResult,
    tuple[OddsSnapshot, ...],
    tuple[OfferEvaluation, ...],
]


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
                allow_stale_quotes=True,
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


def export_atp_wimbledon_csv(
    *,
    model_settings: AppSettings,
    cache_directory: Path,
    output_directory: Path,
) -> WimbledonCsvExport:
    """Export cached ATP data to detailed match and offer CSV files.

    This function makes no provider requests. Every fixture must already have a
    settlement file and all three historical bookmaker batches in the cache.
    """

    fixtures = parse_fixtures(_read_cached(cache_directory / "fixtures.json"))
    evaluated: list[EvaluatedFixture] = []
    all_evaluations: list[OfferEvaluation] = []
    results: dict[str, MatchResult] = {}
    for fixture in fixtures:
        settlement = parse_settlement(
            fixture,
            _read_cached(
                cache_directory / "settlements" / f"{fixture.fixture_id}.json"
            ),
        )
        payloads = tuple(
            _read_cached(
                cache_directory / "historical" / f"{fixture.fixture_id}_{index}.json"
            )
            for index in range(3)
        )
        snapshots = snapshots_at_sixty_minutes(fixture, payloads)
        decision_at = fixture.scheduled_start - timedelta(minutes=60)
        try:
            market = evaluate_market(
                match_for_fixture(fixture),
                snapshots,
                decision_at=decision_at,
                calculated_at=decision_at,
                settings=model_settings,
                allow_stale_quotes=True,
            )
        except InsufficientBookmakersError:
            market_evaluations: tuple[OfferEvaluation, ...] = ()
        else:
            market_evaluations = market.evaluations
        results[fixture.fixture_id] = settlement
        all_evaluations.extend(market_evaluations)
        evaluated.append((fixture, settlement, snapshots, market_evaluations))

    selected = select_best_candidates(all_evaluations, excluded_bookmakers=frozenset())
    report = backtest_kelly_candidates(
        selected,
        results,
        settings=KellySettings(Decimal("10000"), Decimal("0.25")),
    )
    bet_by_match = {bet.candidate.match_id: bet for bet in report.bets}
    output_directory.mkdir(parents=True, exist_ok=True)
    matches_path = output_directory / "wimbledon_atp_2026_matches.csv"
    offers_path = output_directory / "wimbledon_atp_2026_offers.csv"
    _write_matches_csv(matches_path, evaluated, bet_by_match)
    _write_offers_csv(offers_path, evaluated, bet_by_match)
    return WimbledonCsvExport(
        matches_path=matches_path,
        offers_path=offers_path,
        match_rows=len(evaluated),
        offer_rows=len(all_evaluations),
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


def _read_cached(path: Path) -> bytes:
    if not path.is_file():
        raise FileNotFoundError(f"required cached response is missing: {path}")
    raw_bytes = path.read_bytes()
    _json(raw_bytes)
    return raw_bytes


def _write_matches_csv(
    path: Path,
    evaluated: list[EvaluatedFixture],
    bet_by_match: dict[str, KellySettledBet],
) -> None:
    fields = (
        "fixture_id",
        "scheduled_start",
        "decision_at",
        "player_one_id",
        "player_one_name",
        "player_two_id",
        "player_two_name",
        "result_status",
        "winner_player_id",
        "winner_name",
        "eligible_bookmakers",
        "bookmakers",
        "offers_evaluated",
        "candidate_offers",
        "selected_player_id",
        "selected_player_name",
        "selected_bookmaker",
        "selected_odds",
        "selected_ev",
        "kelly_stake",
        "settlement",
        "profit",
        "equity_after",
        "drawdown",
    )
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for fixture, result, snapshots, offers in evaluated:
            player_names = {
                fixture.player_one_id: fixture.player_one_name,
                fixture.player_two_id: fixture.player_two_name,
            }
            bet = bet_by_match.get(fixture.fixture_id)
            candidate = bet.candidate if bet is not None else None
            writer.writerow(
                {
                    "fixture_id": fixture.fixture_id,
                    "scheduled_start": fixture.scheduled_start.isoformat(),
                    "decision_at": (
                        fixture.scheduled_start - timedelta(minutes=60)
                    ).isoformat(),
                    "player_one_id": fixture.player_one_id,
                    "player_one_name": fixture.player_one_name,
                    "player_two_id": fixture.player_two_id,
                    "player_two_name": fixture.player_two_name,
                    "result_status": result.status.value,
                    "winner_player_id": result.winner_player_id or "",
                    "winner_name": (
                        player_names[result.winner_player_id]
                        if result.winner_player_id is not None
                        else ""
                    ),
                    "eligible_bookmakers": len(snapshots),
                    "bookmakers": ",".join(
                        snapshot.bookmaker_id for snapshot in snapshots
                    ),
                    "offers_evaluated": len(offers),
                    "candidate_offers": sum(offer.is_candidate for offer in offers),
                    "selected_player_id": candidate.player_id if candidate else "",
                    "selected_player_name": (
                        player_names[candidate.player_id] if candidate else ""
                    ),
                    "selected_bookmaker": candidate.bookmaker_id if candidate else "",
                    "selected_odds": candidate.offered_odds if candidate else "",
                    "selected_ev": candidate.expected_value if candidate else "",
                    "kelly_stake": bet.stake if bet else "",
                    "settlement": bet.status.value if bet else "",
                    "profit": bet.profit if bet else "",
                    "equity_after": bet.available_equity_after if bet else "",
                    "drawdown": bet.drawdown if bet else "",
                }
            )


def _write_offers_csv(
    path: Path,
    evaluated: list[EvaluatedFixture],
    bet_by_match: dict[str, KellySettledBet],
) -> None:
    fields = (
        "fixture_id",
        "scheduled_start",
        "decision_at",
        "bookmaker",
        "snapshot_id",
        "quote_observed_at",
        "player_id",
        "player_name",
        "opponent_name",
        "offered_odds",
        "bookmaker_overround",
        "consensus_probability",
        "consensus_fair_odds",
        "expected_value",
        "is_candidate",
        "peer_count",
        "peer_bookmaker_snapshots",
        "peer_min_probability",
        "peer_max_probability",
        "peer_probability_range",
        "quality_flags",
        "selected_for_kelly",
        "kelly_stake",
        "settlement",
        "profit",
    )
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for fixture, _, snapshots, offers in evaluated:
            player_names = {
                fixture.player_one_id: fixture.player_one_name,
                fixture.player_two_id: fixture.player_two_name,
            }
            observed_at = {
                snapshot.snapshot_id: snapshot.observed_at for snapshot in snapshots
            }
            bet = bet_by_match.get(fixture.fixture_id)
            selected = bet.candidate if bet is not None else None
            for offer in offers:
                is_selected = (
                    selected is not None
                    and offer.snapshot_id == selected.snapshot_id
                    and offer.player_id == selected.player_id
                )
                writer.writerow(
                    {
                        "fixture_id": fixture.fixture_id,
                        "scheduled_start": fixture.scheduled_start.isoformat(),
                        "decision_at": (
                            fixture.scheduled_start - timedelta(minutes=60)
                        ).isoformat(),
                        "bookmaker": offer.bookmaker_id,
                        "snapshot_id": offer.snapshot_id,
                        "quote_observed_at": observed_at[offer.snapshot_id].isoformat(),
                        "player_id": offer.player_id,
                        "player_name": player_names[offer.player_id],
                        "opponent_name": player_names[
                            next(
                                player_id
                                for player_id in player_names
                                if player_id != offer.player_id
                            )
                        ],
                        "offered_odds": offer.offered_odds,
                        "bookmaker_overround": offer.target_overround,
                        "consensus_probability": offer.consensus_probability,
                        "consensus_fair_odds": offer.consensus_fair_odds,
                        "expected_value": offer.expected_value,
                        "is_candidate": offer.is_candidate,
                        "peer_count": offer.peer_count,
                        "peer_bookmaker_snapshots": ",".join(offer.peer_snapshot_ids),
                        "peer_min_probability": offer.minimum_peer_probability,
                        "peer_max_probability": offer.maximum_peer_probability,
                        "peer_probability_range": offer.peer_probability_range,
                        "quality_flags": ",".join(
                            flag.value for flag in offer.quality_flags
                        ),
                        "selected_for_kelly": is_selected,
                        "kelly_stake": bet.stake if is_selected and bet else "",
                        "settlement": bet.status.value if is_selected and bet else "",
                        "profit": bet.profit if is_selected and bet else "",
                    }
                )


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
            observed_at = max(observed_one, observed_two)
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
    history: list[QuoteRecord] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            observed = _utc(str(record["createdAt"]))
        except (KeyError, ValueError):
            continue
        price_value = record.get("price")
        try:
            odds = Decimal(str(price_value)) if price_value is not None else None
        except ValueError:
            odds = None
        history.append(
            QuoteRecord(
                outcome_id="outcome",
                decimal_odds=odds,
                observed_at=observed,
                active=record.get("active") is True,
            )
        )
    selected = select_latest_record(
        tuple(history),
        decision_at=decision_at,
    )
    if (
        selected is None
        or not selected.active
        or selected.decimal_odds is None
        or not selected.decimal_odds.is_finite()
        or selected.decimal_odds <= 1
    ):
        return None
    return selected.decimal_odds, selected.observed_at


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
