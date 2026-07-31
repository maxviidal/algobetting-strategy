"""Locked NBA leave-one-bookmaker-out consensus strategy."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from basketball_value.domain import (
    Game,
    GameResult,
    MoneylineSnapshot,
)
from betting_core import expected_value, median_decimal, proportional_probabilities


@dataclass(frozen=True, slots=True)
class OfferEvaluation:
    game_id: str
    season: str
    bookmaker_id: str
    team_id: str
    opponent_id: str
    offered_odds: Decimal
    consensus_probability: Decimal
    expected_value: Decimal
    observed_at: datetime
    favourite: bool


@dataclass(frozen=True, slots=True)
class Candidate:
    offer: OfferEvaluation
    closing_consensus_probability: Decimal | None

    @property
    def closing_value(self) -> Decimal | None:
        if self.closing_consensus_probability is None:
            return None
        return (
            self.offer.offered_odds * self.closing_consensus_probability - Decimal(1)
        )


@dataclass(frozen=True, slots=True)
class SettledCandidate:
    candidate: Candidate
    won: bool
    flat_profit: Decimal
    kelly_stake: Decimal
    kelly_profit: Decimal


def evaluate_game(
    game: Game,
    entry_snapshots: tuple[MoneylineSnapshot, ...],
    closing_snapshots: tuple[MoneylineSnapshot, ...],
    *,
    minimum_bookmakers: int,
    ev_threshold: Decimal,
) -> tuple[tuple[OfferEvaluation, ...], Candidate | None]:
    """Evaluate every entry offer and select at most one highest-EV candidate."""

    entries = tuple(
        snapshot for snapshot in entry_snapshots if snapshot.game_id == game.game_id
    )
    if len({snapshot.bookmaker_id for snapshot in entries}) < minimum_bookmakers:
        return (), None
    probabilities = {
        snapshot.bookmaker_id: dict(
            proportional_probabilities(
                (
                    (
                        game.home_team_id,
                        snapshot.price_for(game.home_team_id),
                    ),
                    (
                        game.away_team_id,
                        snapshot.price_for(game.away_team_id),
                    ),
                )
            )
        )
        for snapshot in entries
    }
    evaluations: list[OfferEvaluation] = []
    for snapshot in entries:
        for team_id, opponent_id in (
            (game.home_team_id, game.away_team_id),
            (game.away_team_id, game.home_team_id),
        ):
            peers = tuple(
                prices[team_id]
                for bookmaker_id, prices in probabilities.items()
                if bookmaker_id != snapshot.bookmaker_id
            )
            if not peers:
                continue
            offered = snapshot.price_for(team_id)
            probability = median_decimal(peers)
            evaluations.append(
                OfferEvaluation(
                    game_id=game.game_id,
                    season=game.season,
                    bookmaker_id=snapshot.bookmaker_id,
                    team_id=team_id,
                    opponent_id=opponent_id,
                    offered_odds=offered,
                    consensus_probability=probability,
                    expected_value=expected_value(offered, probability),
                    observed_at=snapshot.observed_at,
                    favourite=offered < Decimal("2"),
                )
            )
    qualifying = tuple(
        evaluation
        for evaluation in evaluations
        if evaluation.expected_value >= ev_threshold
    )
    if not qualifying:
        return tuple(evaluations), None
    best = max(
        qualifying,
        key=lambda evaluation: (
            evaluation.expected_value,
            evaluation.offered_odds,
            evaluation.bookmaker_id,
        ),
    )
    closing = _closing_probability(
        game,
        closing_snapshots,
        team_id=best.team_id,
        excluded_bookmaker=best.bookmaker_id,
        minimum_bookmakers=minimum_bookmakers,
    )
    return tuple(evaluations), Candidate(best, closing)


def settle_candidates(
    values: tuple[tuple[Game, Candidate, GameResult], ...],
    *,
    starting_equity: Decimal,
    kelly_fraction: Decimal,
) -> tuple[SettledCandidate, ...]:
    """Settle flat stakes and a chronological fractional-Kelly sensitivity."""

    equity = starting_equity
    settled: list[SettledCandidate] = []
    for game, candidate, result in sorted(
        values, key=lambda item: item[0].scheduled_start
    ):
        winning_side = result.winner_team_side
        if winning_side is None:
            continue
        winner = (
            game.home_team_id if winning_side == "home" else game.away_team_id
        )
        won = candidate.offer.team_id == winner
        flat_profit = (
            candidate.offer.offered_odds - Decimal(1) if won else Decimal("-1")
        )
        probability = candidate.offer.consensus_probability
        b = candidate.offer.offered_odds - Decimal(1)
        raw_fraction = (b * probability - (Decimal(1) - probability)) / b
        stake = equity * kelly_fraction * max(Decimal(0), raw_fraction)
        kelly_profit = stake * b if won else -stake
        equity += kelly_profit
        settled.append(
            SettledCandidate(
                candidate=candidate,
                won=won,
                flat_profit=flat_profit,
                kelly_stake=stake,
                kelly_profit=kelly_profit,
            )
        )
    return tuple(settled)


def _closing_probability(
    game: Game,
    snapshots: tuple[MoneylineSnapshot, ...],
    *,
    team_id: str,
    excluded_bookmaker: str,
    minimum_bookmakers: int,
) -> Decimal | None:
    values = tuple(
        dict(
            proportional_probabilities(
                (
                    (
                        game.home_team_id,
                        snapshot.price_for(game.home_team_id),
                    ),
                    (
                        game.away_team_id,
                        snapshot.price_for(game.away_team_id),
                    ),
                )
            )
        )[team_id]
        for snapshot in snapshots
        if snapshot.game_id == game.game_id
        and snapshot.bookmaker_id != excluded_bookmaker
    )
    if len(values) < minimum_bookmakers - 1:
        return None
    return median_decimal(values)
