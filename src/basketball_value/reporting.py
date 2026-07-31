"""Backtest orchestration and reproducible CSV/JSON exports."""

import csv
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from statistics import mean, stdev
from typing import Any

from basketball_value.config import BasketballSettings
from basketball_value.strategy import (
    Candidate,
    OfferEvaluation,
    SettledCandidate,
    evaluate_game,
    settle_candidates,
)
from basketball_value.workflow import BasketballDataset


@dataclass(frozen=True, slots=True)
class BasketballBacktest:
    offers: tuple[OfferEvaluation, ...]
    candidates: tuple[Candidate, ...]
    settled: tuple[SettledCandidate, ...]
    games_with_entry_coverage: int


def run_backtest(
    dataset: BasketballDataset, settings: BasketballSettings
) -> BasketballBacktest:
    """Run the locked threshold once across the chronological dataset."""

    offers: list[OfferEvaluation] = []
    candidates: list[Candidate] = []
    settlement_inputs = []
    covered = 0
    for game in dataset.games:
        entries = tuple(
            value
            for value in dataset.entry_snapshots
            if value.game_id == game.game_id
        )
        if (
            len({value.bookmaker_id for value in entries})
            >= settings.minimum_bookmakers
        ):
            covered += 1
        game_offers, candidate = evaluate_game(
            game,
            entries,
            dataset.closing_snapshots,
            minimum_bookmakers=settings.minimum_bookmakers,
            ev_threshold=settings.primary_ev_threshold,
        )
        offers.extend(game_offers)
        if candidate is None:
            continue
        candidates.append(candidate)
        result = dataset.results.get(game.game_id)
        if result is not None:
            settlement_inputs.append((game, candidate, result))
    settled = settle_candidates(
        tuple(settlement_inputs),
        starting_equity=settings.kelly_starting_equity,
        kelly_fraction=settings.kelly_fraction,
    )
    return BasketballBacktest(tuple(offers), tuple(candidates), settled, covered)


def build_summary(
    dataset: BasketballDataset,
    run: BasketballBacktest,
    settings: BasketballSettings,
) -> dict[str, Any]:
    """Build coverage, performance, risk, calibration, and acceptance metrics."""

    total_games = len(dataset.games)
    event_total = dataset.matched_events + dataset.unmatched_events
    match_rate = (
        Decimal(dataset.matched_events) / Decimal(event_total)
        if event_total
        else Decimal(0)
    )
    entry_coverage = (
        Decimal(run.games_with_entry_coverage) / Decimal(dataset.matched_games)
        if dataset.matched_games
        else Decimal(0)
    )
    holdout = tuple(
        value
        for value in run.settled
        if value.candidate.offer.season in settings.holdout_seasons
    )
    holdout_metrics = _performance(holdout)
    data_complete = (
        dataset.completed_timestamps == dataset.requested_timestamps
        and match_rate >= settings.minimum_result_match_rate
        and entry_coverage >= settings.minimum_entry_coverage_rate
    )
    sample_large_enough = (
        len(holdout) >= settings.minimum_holdout_candidates
    )
    supported = (
        data_complete
        and sample_large_enough
        and holdout_metrics["closing_value_count"] == len(holdout)
        and _decimal_or_zero(holdout_metrics["average_closing_value"]) > 0
        and _decimal_or_zero(holdout_metrics["flat_roi"]) > 0
        and _decimal_or_zero(holdout_metrics["mean_profit_ci95_lower"]) > 0
    )
    if supported:
        conclusion = "supported"
    elif not data_complete or not sample_large_enough:
        conclusion = "inconclusive"
    else:
        conclusion = "negative"
    return {
        "strategy": {
            "market": "NBA regular-season moneyline including overtime",
            "entry_minutes_before_tip": settings.entry_minutes_before_tip,
            "closing_minutes_before_tip": settings.closing_minutes_before_tip,
            "minimum_bookmakers": settings.minimum_bookmakers,
            "primary_ev_threshold": settings.primary_ev_threshold,
            "selection": "one highest-EV candidate per game",
            "confidence_interval": "normal 95% interval for mean unit profit",
        },
        "coverage": {
            "games": total_games,
            "matched_games": dataset.matched_games,
            "requested_timestamps": dataset.requested_timestamps,
            "completed_timestamps": dataset.completed_timestamps,
            "matched_odds_events": dataset.matched_events,
            "unmatched_odds_events": dataset.unmatched_events,
            "quarantined_event_ids": dataset.quarantined_event_ids,
            "market_exclusion_count": len(dataset.market_exclusions),
            "market_exclusions": dataset.market_exclusions,
            "result_quarantines": dataset.result_quarantines,
            "result_match_rate": match_rate,
            "games_with_entry_coverage": run.games_with_entry_coverage,
            "entry_coverage_rate": entry_coverage,
            "data_complete": data_complete,
        },
        "overall": _performance(run.settled),
        "holdout_2025_26": holdout_metrics,
        "development_threshold_exploration": _development_thresholds(
            dataset, settings
        ),
        "breakdowns": _breakdowns(run.settled),
        "acceptance": {
            "minimum_holdout_candidates": settings.minimum_holdout_candidates,
            "sample_large_enough": sample_large_enough,
            "supported": supported,
            "conclusion": conclusion,
        },
    }


def export_reports(
    *,
    output_directory: Path,
    dataset: BasketballDataset,
    run: BasketballBacktest,
    settings: BasketballSettings,
) -> tuple[Path, Path, Path]:
    """Write game-, offer-, and summary-level reports without network access."""

    output_directory.mkdir(parents=True, exist_ok=True)
    games_path = output_directory / "nba_moneyline_games.csv"
    offers_path = output_directory / "nba_moneyline_offers.csv"
    summary_path = output_directory / "nba_moneyline_summary.json"
    candidate_by_game = {
        candidate.offer.game_id: candidate for candidate in run.candidates
    }
    settled_by_game = {
        value.candidate.offer.game_id: value for value in run.settled
    }
    with games_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "game_id",
                "season",
                "scheduled_start",
                "home_team",
                "away_team",
                "candidate_team",
                "bookmaker",
                "entry_odds",
                "expected_value",
                "closing_value",
                "won",
                "flat_profit",
            ],
        )
        writer.writeheader()
        for game in dataset.games:
            candidate = candidate_by_game.get(game.game_id)
            settled = settled_by_game.get(game.game_id)
            writer.writerow(
                {
                    "game_id": game.game_id,
                    "season": game.season,
                    "scheduled_start": game.scheduled_start.isoformat(),
                    "home_team": game.home_team_id,
                    "away_team": game.away_team_id,
                    "candidate_team": (
                        candidate.offer.team_id if candidate else ""
                    ),
                    "bookmaker": (
                        candidate.offer.bookmaker_id if candidate else ""
                    ),
                    "entry_odds": (
                        candidate.offer.offered_odds if candidate else ""
                    ),
                    "expected_value": (
                        candidate.offer.expected_value if candidate else ""
                    ),
                    "closing_value": (
                        candidate.closing_value
                        if candidate and candidate.closing_value is not None
                        else ""
                    ),
                    "won": settled.won if settled else "",
                    "flat_profit": settled.flat_profit if settled else "",
                }
            )
    with offers_path.open("w", newline="", encoding="utf-8") as handle:
        fields = tuple(asdict(run.offers[0])) if run.offers else (
            "game_id",
            "season",
            "bookmaker_id",
            "team_id",
            "opponent_id",
            "offered_odds",
            "consensus_probability",
            "expected_value",
            "observed_at",
            "favourite",
        )
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for offer in run.offers:
            row = asdict(offer)
            row["observed_at"] = offer.observed_at.isoformat()
            writer.writerow(row)
    summary_path.write_text(
        json.dumps(
            build_summary(dataset, run, settings),
            indent=2,
            sort_keys=True,
            default=_json_default,
        )
        + "\n"
    )
    return games_path, offers_path, summary_path


def _performance(values: tuple[SettledCandidate, ...]) -> dict[str, Any]:
    profits = [float(value.flat_profit) for value in values]
    closing = [
        float(value.candidate.closing_value)
        for value in values
        if value.candidate.closing_value is not None
    ]
    probabilities = [
        float(value.candidate.offer.consensus_probability) for value in values
    ]
    outcomes = [1.0 if value.won else 0.0 for value in values]
    lower, upper = _mean_ci(profits)
    cumulative = 0.0
    peak = 0.0
    maximum_drawdown = 0.0
    for profit in profits:
        cumulative += profit
        peak = max(peak, cumulative)
        maximum_drawdown = min(maximum_drawdown, cumulative - peak)
    log_losses = [
        -(outcome * math.log(max(probability, 1e-12)))
        - ((1 - outcome) * math.log(max(1 - probability, 1e-12)))
        for probability, outcome in zip(probabilities, outcomes, strict=True)
    ]
    return {
        "candidates": len(values),
        "turnover_units": len(values),
        "flat_profit_units": sum(profits),
        "flat_roi": mean(profits) if profits else None,
        "hit_rate": mean(outcomes) if outcomes else None,
        "average_closing_value": mean(closing) if closing else None,
        "closing_value_count": len(closing),
        "brier_score": (
            mean(
                (probability - outcome) ** 2
                for probability, outcome in zip(
                    probabilities, outcomes, strict=True
                )
            )
            if probabilities
            else None
        ),
        "calibration": _calibration(probabilities, outcomes),
        "log_loss": mean(log_losses) if log_losses else None,
        "maximum_flat_drawdown_units": maximum_drawdown,
        "mean_profit_ci95_lower": lower,
        "mean_profit_ci95_upper": upper,
        "kelly_turnover": sum(float(value.kelly_stake) for value in values),
        "kelly_profit": sum(float(value.kelly_profit) for value in values),
    }


def _mean_ci(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    average = mean(values)
    if len(values) == 1:
        return average, average
    half_width = 1.96 * stdev(values) / math.sqrt(len(values))
    return average - half_width, average + half_width


def _development_thresholds(
    dataset: BasketballDataset, settings: BasketballSettings
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for threshold in settings.development_thresholds:
        inputs = []
        for game in dataset.games:
            if game.season not in settings.development_seasons:
                continue
            _, candidate = evaluate_game(
                game,
                dataset.entry_snapshots,
                dataset.closing_snapshots,
                minimum_bookmakers=settings.minimum_bookmakers,
                ev_threshold=threshold,
            )
            game_result = dataset.results.get(game.game_id)
            if candidate is not None and game_result is not None:
                inputs.append((game, candidate, game_result))
        settled = settle_candidates(
            tuple(inputs),
            starting_equity=settings.kelly_starting_equity,
            kelly_fraction=settings.kelly_fraction,
        )
        result[str(threshold)] = _performance(settled)
    return result


def _calibration(
    probabilities: list[float], outcomes: list[float]
) -> list[dict[str, float | int]]:
    bins: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for probability, outcome in zip(probabilities, outcomes, strict=True):
        bins[min(9, int(probability * 10))].append((probability, outcome))
    return [
        {
            "lower_probability": index / 10,
            "upper_probability": (index + 1) / 10,
            "count": len(values),
            "mean_predicted_probability": mean(value[0] for value in values),
            "observed_win_rate": mean(value[1] for value in values),
        }
        for index, values in sorted(bins.items())
    ]


def _breakdowns(
    values: tuple[SettledCandidate, ...],
) -> dict[str, dict[str, dict[str, Any]]]:
    dimensions: dict[str, dict[str, list[SettledCandidate]]] = {
        key: defaultdict(list)
        for key in ("season", "bookmaker", "side", "odds_band", "ev_band")
    }
    for value in values:
        offer = value.candidate.offer
        dimensions["season"][offer.season].append(value)
        dimensions["bookmaker"][offer.bookmaker_id].append(value)
        dimensions["side"]["favourite" if offer.favourite else "underdog"].append(
            value
        )
        dimensions["odds_band"][_band(float(offer.offered_odds), (1.5, 2, 3))].append(
            value
        )
        dimensions["ev_band"][
            _band(float(offer.expected_value), (0.075, 0.10, 0.15))
        ].append(value)
    return {
        dimension: {
            label: _performance(tuple(group))
            for label, group in sorted(groups.items())
        }
        for dimension, groups in dimensions.items()
    }


def _band(value: float, boundaries: tuple[float, ...]) -> str:
    lower = float("-inf")
    for upper in boundaries:
        if value < upper:
            return f"{lower:g}_to_{upper:g}"
        lower = upper
    return f"{lower:g}_plus"


def _decimal_or_zero(value: object) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")
