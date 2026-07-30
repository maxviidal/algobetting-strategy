"""Conservative settlement and reporting for point-in-time value signals."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from tennis_value.domain import PlayerId
from tennis_value.signals import OfferEvaluation

DEFAULT_EXCLUDED_BOOKMAKERS = frozenset(
    {
        "betfair_ex_uk",
        "matchbook",
        "smarkets",
    }
)


class ResultStatus(StrEnum):
    """Settlement states recognised by the baseline backtest."""

    COMPLETED = "completed"
    RETIRED = "retired"
    CANCELLED = "cancelled"
    VOID = "void"


class SettlementStatus(StrEnum):
    """How a selected signal was handled by the backtest."""

    WIN = "win"
    LOSS = "loss"
    VOID = "void"
    MISSING_RESULT = "missing_result"


@dataclass(frozen=True, slots=True)
class KellySettings:
    """Bankroll and fractional-Kelly parameters for a research simulation."""

    initial_equity: Decimal = Decimal("10000")
    kelly_fraction: Decimal = Decimal("0.25")

    def __post_init__(self) -> None:
        if not self.initial_equity.is_finite() or self.initial_equity <= 0:
            raise ValueError("initial_equity must be finite and greater than zero")
        if (
            not self.kelly_fraction.is_finite()
            or self.kelly_fraction <= 0
            or self.kelly_fraction > Decimal(1)
        ):
            raise ValueError("kelly_fraction must be greater than zero and at most one")


_DEFAULT_KELLY_SETTINGS = KellySettings()


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Provider-neutral result input for one normalized match."""

    match_id: str
    status: ResultStatus
    settled_at: datetime
    source: str
    winner_player_id: PlayerId | None = None

    def __post_init__(self) -> None:
        if not self.match_id.strip():
            raise ValueError("match_id must not be empty")
        if not self.source.strip():
            raise ValueError("source must not be empty")
        if not isinstance(self.status, ResultStatus):
            raise ValueError("status must be a ResultStatus")
        _require_utc(self.settled_at, "settled_at")
        if self.status is ResultStatus.COMPLETED:
            if (
                self.winner_player_id is None
                or isinstance(self.winner_player_id, bool)
                or self.winner_player_id <= 0
            ):
                raise ValueError(
                    "completed results require a positive winner_player_id"
                )
        elif self.winner_player_id is not None:
            raise ValueError(
                "non-completed results must not specify winner_player_id"
            )


@dataclass(frozen=True, slots=True)
class SelectedCandidate:
    """The single best qualifying sportsbook offer for one player and match."""

    match_id: str
    player_id: PlayerId
    bookmaker_id: str
    snapshot_id: str
    offered_odds: Decimal
    consensus_probability: Decimal
    expected_value: Decimal
    decision_at: datetime


@dataclass(frozen=True, slots=True)
class SettledBet:
    """One selected candidate after result matching and settlement."""

    candidate: SelectedCandidate
    status: SettlementStatus
    profit: Decimal
    cumulative_profit: Decimal
    drawdown: Decimal


@dataclass(frozen=True, slots=True)
class BacktestReport:
    """Aggregate unit-stake performance metrics for selected signals."""

    selected_candidates: int
    settled_bets: int
    wins: int
    losses: int
    void_bets: int
    missing_results: int
    turnover: Decimal
    profit: Decimal
    roi: Decimal
    yield_: Decimal
    hit_rate: Decimal
    maximum_drawdown: Decimal
    bets: tuple[SettledBet, ...]


@dataclass(frozen=True, slots=True)
class KellySettledBet:
    """A Kelly-sized bet with the available cash before and after settlement."""

    candidate: SelectedCandidate
    status: SettlementStatus
    stake: Decimal
    available_equity_before: Decimal
    available_equity_after: Decimal
    profit: Decimal
    drawdown: Decimal


@dataclass(frozen=True, slots=True)
class KellyBacktestReport:
    """Results of a fractional-Kelly simulation using a finite bankroll."""

    settings: KellySettings
    selected_candidates: int
    settled_bets: int
    wins: int
    losses: int
    void_bets: int
    missing_results: int
    turnover: Decimal
    profit: Decimal
    final_equity: Decimal
    roi: Decimal
    yield_: Decimal
    hit_rate: Decimal
    maximum_drawdown: Decimal
    bets: tuple[KellySettledBet, ...]


def select_best_candidates(
    evaluations: Iterable[OfferEvaluation],
    *,
    excluded_bookmakers: frozenset[str] = DEFAULT_EXCLUDED_BOOKMAKERS,
) -> tuple[SelectedCandidate, ...]:
    """Choose the best qualifying non-exchange offer per match and player."""

    best_by_selection: dict[tuple[str, PlayerId], SelectedCandidate] = {}
    for evaluation in evaluations:
        if not evaluation.is_candidate:
            continue
        if evaluation.bookmaker_id in excluded_bookmakers:
            continue
        candidate = SelectedCandidate(
            match_id=evaluation.match_id,
            player_id=evaluation.player_id,
            bookmaker_id=evaluation.bookmaker_id,
            snapshot_id=evaluation.snapshot_id,
            offered_odds=evaluation.offered_odds,
            consensus_probability=evaluation.consensus_probability,
            expected_value=evaluation.expected_value,
            decision_at=evaluation.decision_at,
        )
        key = (candidate.match_id, candidate.player_id)
        existing = best_by_selection.get(key)
        if existing is None or _is_better_candidate(candidate, existing):
            best_by_selection[key] = candidate
    return tuple(
        sorted(
            best_by_selection.values(),
            key=lambda candidate: (
                candidate.decision_at,
                candidate.match_id,
                candidate.player_id,
                candidate.bookmaker_id,
            ),
        )
    )


def select_one_candidate_per_match(
    candidates: Iterable[SelectedCandidate],
) -> tuple[SelectedCandidate, ...]:
    """Choose one bet per match, preventing exposure to both opponents."""

    best_by_match: dict[str, SelectedCandidate] = {}
    for candidate in candidates:
        existing = best_by_match.get(candidate.match_id)
        if existing is None or _is_better_match_candidate(candidate, existing):
            best_by_match[candidate.match_id] = candidate
    return tuple(
        sorted(
            best_by_match.values(),
            key=lambda candidate: (
                candidate.decision_at,
                candidate.match_id,
                candidate.player_id,
                candidate.bookmaker_id,
            ),
        )
    )


def kelly_fraction(candidate: SelectedCandidate) -> Decimal:
    """Return the full-Kelly bankroll fraction, floored at zero."""

    net_odds = candidate.offered_odds - Decimal(1)
    if not net_odds.is_finite() or net_odds <= 0:
        raise ValueError("offered_odds must be finite and greater than one")
    probability = candidate.consensus_probability
    if not probability.is_finite() or probability < 0 or probability > 1:
        raise ValueError("consensus_probability must be between zero and one")
    fraction = (candidate.offered_odds * probability - Decimal(1)) / net_odds
    return max(Decimal(0), fraction)


def kelly_stake(
    candidate: SelectedCandidate,
    *,
    available_equity: Decimal,
    settings: KellySettings,
) -> Decimal:
    """Calculate the configured fractional-Kelly stake from available cash."""

    if not available_equity.is_finite() or available_equity < 0:
        raise ValueError("available_equity must be finite and non-negative")
    return available_equity * settings.kelly_fraction * kelly_fraction(candidate)


def backtest_candidates(
    candidates: Iterable[SelectedCandidate],
    results: Mapping[str, MatchResult],
    *,
    stake: Decimal = Decimal(1),
) -> BacktestReport:
    """Settle selected unit-stake candidates without future result leakage."""

    if not stake.is_finite() or stake <= 0:
        raise ValueError("stake must be finite and greater than zero")

    ordered_candidates = tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                candidate.decision_at,
                candidate.match_id,
                candidate.player_id,
                candidate.bookmaker_id,
            ),
        )
    )
    cumulative_profit = Decimal(0)
    peak_profit = Decimal(0)
    maximum_drawdown = Decimal(0)
    wins = 0
    losses = 0
    void_bets = 0
    missing_results = 0
    settled_bets = 0
    bets: list[SettledBet] = []

    for candidate in ordered_candidates:
        result = results.get(candidate.match_id)
        status, profit = _settle_candidate(candidate, result, stake)
        if status is SettlementStatus.WIN:
            wins += 1
            settled_bets += 1
        elif status is SettlementStatus.LOSS:
            losses += 1
            settled_bets += 1
        elif status is SettlementStatus.VOID:
            void_bets += 1
        else:
            missing_results += 1

        cumulative_profit += profit
        peak_profit = max(peak_profit, cumulative_profit)
        drawdown = peak_profit - cumulative_profit
        maximum_drawdown = max(maximum_drawdown, drawdown)
        bets.append(
            SettledBet(
                candidate=candidate,
                status=status,
                profit=profit,
                cumulative_profit=cumulative_profit,
                drawdown=drawdown,
            )
        )

    turnover = stake * Decimal(settled_bets)
    profit = cumulative_profit
    roi = profit / turnover if turnover else Decimal(0)
    hit_rate = Decimal(wins) / Decimal(settled_bets) if settled_bets else Decimal(0)
    return BacktestReport(
        selected_candidates=len(ordered_candidates),
        settled_bets=settled_bets,
        wins=wins,
        losses=losses,
        void_bets=void_bets,
        missing_results=missing_results,
        turnover=turnover,
        profit=profit,
        roi=roi,
        yield_=roi,
        hit_rate=hit_rate,
        maximum_drawdown=maximum_drawdown,
        bets=tuple(bets),
    )


def backtest_kelly_candidates(
    candidates: Iterable[SelectedCandidate],
    results: Mapping[str, MatchResult],
    *,
    settings: KellySettings = _DEFAULT_KELLY_SETTINGS,
) -> KellyBacktestReport:
    """Settle one selection per match using 25%-Kelly by default.

    Stakes are calculated when the signal occurs from cash not already committed
    to an unsettled bet. Results known before a later decision release their
    stake and update cash first; this prevents overlapping tennis matches from
    spending the same bankroll twice.
    """

    ordered = select_one_candidate_per_match(candidates)
    available_equity = settings.initial_equity
    peak_equity = settings.initial_equity
    maximum_drawdown = Decimal(0)
    turnover = Decimal(0)
    wins = losses = void_bets = missing_results = settled_bets = 0
    bets: list[KellySettledBet] = []
    open_bets: list[tuple[SelectedCandidate, Decimal]] = []

    def settle_open_through(through: datetime | None) -> None:
        nonlocal available_equity, peak_equity, maximum_drawdown
        nonlocal wins, losses, void_bets, missing_results, settled_bets
        remaining: list[tuple[SelectedCandidate, Decimal]] = []
        for open_candidate, stake in open_bets:
            result = results.get(open_candidate.match_id)
            if result is None:
                if through is None:
                    missing_results += 1
                    bets.append(
                        KellySettledBet(
                            candidate=open_candidate,
                            status=SettlementStatus.MISSING_RESULT,
                            stake=stake,
                            available_equity_before=available_equity,
                            available_equity_after=available_equity,
                            profit=Decimal(0),
                            drawdown=maximum_drawdown,
                        )
                    )
                else:
                    remaining.append((open_candidate, stake))
                continue
            if result.settled_at < open_candidate.decision_at:
                raise ValueError(
                    f"result for match {open_candidate.match_id!r} predates "
                    "its decision time"
                )
            if through is not None and result.settled_at > through:
                remaining.append((open_candidate, stake))
                continue
            before = available_equity
            status, profit = _settle_candidate(open_candidate, result, stake)
            if status is SettlementStatus.WIN:
                wins += 1
                settled_bets += 1
                available_equity += stake + profit
            elif status is SettlementStatus.LOSS:
                losses += 1
                settled_bets += 1
            elif status is SettlementStatus.VOID:
                void_bets += 1
                available_equity += stake
            peak_equity = max(peak_equity, available_equity)
            drawdown = peak_equity - available_equity
            maximum_drawdown = max(maximum_drawdown, drawdown)
            bets.append(
                KellySettledBet(
                    candidate=open_candidate,
                    status=status,
                    stake=stake,
                    available_equity_before=before,
                    available_equity_after=available_equity,
                    profit=profit,
                    drawdown=drawdown,
                )
            )
        open_bets[:] = remaining

    for candidate in ordered:
        settle_open_through(candidate.decision_at)
        if candidate.match_id not in results:
            missing_results += 1
            bets.append(
                KellySettledBet(
                    candidate=candidate,
                    status=SettlementStatus.MISSING_RESULT,
                    stake=Decimal(0),
                    available_equity_before=available_equity,
                    available_equity_after=available_equity,
                    profit=Decimal(0),
                    drawdown=maximum_drawdown,
                )
            )
            continue
        stake = kelly_stake(
            candidate,
            available_equity=available_equity,
            settings=settings,
        )
        if stake > available_equity:
            raise ValueError("Kelly stake exceeds available equity")
        available_equity -= stake
        turnover += stake
        open_bets.append((candidate, stake))

    settle_open_through(None)
    profit = available_equity - settings.initial_equity
    roi = profit / turnover if turnover else Decimal(0)
    hit_rate = Decimal(wins) / Decimal(settled_bets) if settled_bets else Decimal(0)
    return KellyBacktestReport(
        settings=settings,
        selected_candidates=len(ordered),
        settled_bets=settled_bets,
        wins=wins,
        losses=losses,
        void_bets=void_bets,
        missing_results=missing_results,
        turnover=turnover,
        profit=profit,
        final_equity=available_equity,
        roi=roi,
        yield_=roi,
        hit_rate=hit_rate,
        maximum_drawdown=maximum_drawdown,
        bets=tuple(bets),
    )


def _is_better_candidate(
    candidate: SelectedCandidate,
    existing: SelectedCandidate,
) -> bool:
    if candidate.offered_odds != existing.offered_odds:
        return candidate.offered_odds > existing.offered_odds
    if candidate.expected_value != existing.expected_value:
        return candidate.expected_value > existing.expected_value
    return candidate.bookmaker_id < existing.bookmaker_id


def _is_better_match_candidate(
    candidate: SelectedCandidate,
    existing: SelectedCandidate,
) -> bool:
    if candidate.expected_value != existing.expected_value:
        return candidate.expected_value > existing.expected_value
    return _is_better_candidate(candidate, existing)


def _settle_candidate(
    candidate: SelectedCandidate,
    result: MatchResult | None,
    stake: Decimal,
) -> tuple[SettlementStatus, Decimal]:
    if result is None:
        return SettlementStatus.MISSING_RESULT, Decimal(0)
    if result.settled_at < candidate.decision_at:
        raise ValueError(
            f"result for match {candidate.match_id!r} predates its decision time"
        )
    if result.status is not ResultStatus.COMPLETED:
        return SettlementStatus.VOID, Decimal(0)
    if result.winner_player_id == candidate.player_id:
        return SettlementStatus.WIN, stake * (candidate.offered_odds - Decimal(1))
    return SettlementStatus.LOSS, -stake


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must be in UTC")
