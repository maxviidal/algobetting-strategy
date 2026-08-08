"""Backtest orchestration and reproducible CSV/JSON exports."""

import csv
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from statistics import mean, stdev
from typing import Any

from basketball_value.calibration import (
    BootstrapCalibration,
    CalibrationObservation,
    CalibrationPrediction,
    fit_bootstrap_calibration,
)
from basketball_value.config import BasketballSettings
from basketball_value.domain import Game, GameResult, MoneylineSnapshot
from basketball_value.strategy import (
    Candidate,
    OfferEvaluation,
    SettledCandidate,
    evaluate_game,
    market_home_probability,
    settle_candidates,
)
from basketball_value.workflow import BasketballDataset


@dataclass(frozen=True, slots=True)
class CalibrationRunMetadata:
    enabled: bool
    phase_game_counts: tuple[tuple[str, int], ...]
    phase_observation_counts: tuple[tuple[str, int], ...]
    validation_fit_observations: int
    test_fit_observations: int
    validation_trained_through: str | None
    test_trained_through: str | None


@dataclass(frozen=True, slots=True)
class BasketballBacktest:
    offers: tuple[OfferEvaluation, ...]
    candidates: tuple[Candidate, ...]
    settled: tuple[SettledCandidate, ...]
    games_with_entry_coverage: int
    calibration_predictions: tuple[CalibrationPrediction, ...]
    calibration_metadata: CalibrationRunMetadata


def run_backtest(
    dataset: BasketballDataset, settings: BasketballSettings
) -> BasketballBacktest:
    """Run the locked strategy with optional within-season calibration."""

    offers: list[OfferEvaluation] = []
    candidates: list[Candidate] = []
    settlement_inputs: list[tuple[Game, Candidate, GameResult]] = []
    covered = 0
    ordered_games = tuple(
        sorted(dataset.games, key=lambda value: value.scheduled_start)
    )
    if settings.calibration_enabled:
        unexpected_seasons = sorted(
            {game.season for game in ordered_games} - {"2023-24"}
        )
        if unexpected_seasons:
            raise ValueError(
                "the calibrated pilot accepts only 2023-24 games; found "
                + ", ".join(unexpected_seasons)
            )
    entries_by_game = {
        game.game_id: tuple(
            value for value in dataset.entry_snapshots if value.game_id == game.game_id
        )
        for game in ordered_games
    }
    phases = (
        _chronological_phases(
            ordered_games,
            training_fraction=settings.calibration_training_fraction,
            validation_fraction=settings.calibration_validation_fraction,
        )
        if settings.calibration_enabled
        else {game.game_id: "uncalibrated" for game in ordered_games}
    )
    observations = {
        game.game_id: observation
        for game in ordered_games
        if (
            observation := _calibration_observation(
                game,
                entries_by_game[game.game_id],
                dataset=dataset,
                minimum_bookmakers=settings.minimum_bookmakers,
            )
        )
        is not None
    }
    validation_calibration: BootstrapCalibration | None = None
    test_calibration: BootstrapCalibration | None = None
    if settings.calibration_enabled:
        training_observations = tuple(
            observations[game.game_id]
            for game in ordered_games
            if phases[game.game_id] == "training" and game.game_id in observations
        )
        validation_observations = tuple(
            observations[game.game_id]
            for game in ordered_games
            if phases[game.game_id] == "validation" and game.game_id in observations
        )
        validation_calibration = _fit_calibration_if_ready(
            training_observations,
            settings=settings,
            random_seed=settings.calibration_random_seed,
        )
        test_calibration = _fit_calibration_if_ready(
            training_observations + validation_observations,
            settings=settings,
            random_seed=settings.calibration_random_seed + 1,
        )
    predictions: list[CalibrationPrediction] = []
    for game in ordered_games:
        entries = entries_by_game[game.game_id]
        if (
            len({value.bookmaker_id for value in entries})
            >= settings.minimum_bookmakers
        ):
            covered += 1
        phase = phases[game.game_id]
        calibration = (
            validation_calibration
            if phase == "validation"
            else test_calibration
            if phase == "test"
            else None
        )
        game_offers, candidate = evaluate_game(
            game,
            entries,
            dataset.closing_snapshots,
            minimum_bookmakers=settings.minimum_bookmakers,
            ev_threshold=settings.primary_ev_threshold,
            calibration=calibration,
            calibration_phase=phase,
            select_candidate=(
                not settings.calibration_enabled or calibration is not None
            ),
        )
        offers.extend(game_offers)
        observation = observations.get(game.game_id)
        if calibration is not None and observation is not None:
            predictions.append(
                CalibrationPrediction(
                    game_id=game.game_id,
                    phase=phase,
                    raw_home_probability=observation.home_probability,
                    calibrated_home_probability=(
                        calibration.point_model.calibrate(observation.home_probability)
                    ),
                    home_won=observation.home_won,
                )
            )
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
    phase_game_counts = tuple(
        (phase, sum(value == phase for value in phases.values()))
        for phase in ("training", "validation", "test", "uncalibrated")
        if phase in phases.values()
    )
    phase_observation_counts = tuple(
        (
            phase,
            sum(
                game_id in observations and value == phase
                for game_id, value in phases.items()
            ),
        )
        for phase, _ in phase_game_counts
    )
    metadata = CalibrationRunMetadata(
        enabled=settings.calibration_enabled,
        phase_game_counts=phase_game_counts,
        phase_observation_counts=phase_observation_counts,
        validation_fit_observations=(
            validation_calibration.training_observations
            if validation_calibration is not None
            else 0
        ),
        test_fit_observations=(
            test_calibration.training_observations
            if test_calibration is not None
            else 0
        ),
        validation_trained_through=(
            validation_calibration.trained_through.isoformat()
            if validation_calibration is not None
            else None
        ),
        test_trained_through=(
            test_calibration.trained_through.isoformat()
            if test_calibration is not None
            else None
        ),
    )
    return BasketballBacktest(
        tuple(offers),
        tuple(candidates),
        settled,
        covered,
        tuple(predictions),
        metadata,
    )


def _chronological_phases(
    games: tuple[Game, ...],
    *,
    training_fraction: Decimal,
    validation_fraction: Decimal,
) -> dict[str, str]:
    """Split whole UTC game dates into 60/20/20 chronological phases."""

    if not games:
        return {}
    games_by_date: dict[date, list[Game]] = defaultdict(list)
    for game in games:
        games_by_date[game.scheduled_start.date()].append(game)
    training_target = float(training_fraction) * len(games)
    validation_target = float(training_fraction + validation_fraction) * len(games)
    cumulative = 0
    phases: dict[str, str] = {}
    for game_date in sorted(games_by_date):
        if cumulative < training_target:
            phase = "training"
        elif cumulative < validation_target:
            phase = "validation"
        else:
            phase = "test"
        for game in games_by_date[game_date]:
            phases[game.game_id] = phase
        cumulative += len(games_by_date[game_date])
    return phases


def _calibration_observation(
    game: Game,
    entries: tuple[MoneylineSnapshot, ...],
    *,
    dataset: BasketballDataset,
    minimum_bookmakers: int,
) -> CalibrationObservation | None:
    result = dataset.results.get(game.game_id)
    if result is None or result.winner_team_side is None:
        return None
    probability = market_home_probability(
        game,
        entries,
        minimum_bookmakers=minimum_bookmakers,
    )
    if probability is None:
        return None
    return CalibrationObservation(
        game_id=game.game_id,
        scheduled_start=game.scheduled_start,
        home_probability=probability,
        home_won=result.winner_team_side == "home",
    )


def _fit_calibration_if_ready(
    observations: tuple[CalibrationObservation, ...],
    *,
    settings: BasketballSettings,
    random_seed: int,
) -> BootstrapCalibration | None:
    if len(observations) < settings.calibration_minimum_training_games:
        return None
    return fit_bootstrap_calibration(
        observations,
        bootstrap_samples=settings.calibration_bootstrap_samples,
        block_days=settings.calibration_block_days,
        lower_quantile=settings.calibration_lower_quantile,
        regularization=settings.calibration_regularization,
        random_seed=random_seed,
    )


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
    pilot_validation = tuple(
        value
        for value in run.settled
        if value.candidate.offer.calibration_phase == "validation"
    )
    pilot_test = tuple(
        value
        for value in run.settled
        if value.candidate.offer.calibration_phase == "test"
    )
    pilot = settings.profile == "development_pilot_2023_24"
    acceptance_values = pilot_test if pilot else holdout
    acceptance_metrics = _performance(acceptance_values)
    data_complete = (
        dataset.completed_timestamps == dataset.requested_timestamps
        and match_rate >= settings.minimum_result_match_rate
        and entry_coverage >= settings.minimum_entry_coverage_rate
    )
    sample_large_enough = (
        not pilot and len(acceptance_values) >= settings.minimum_holdout_candidates
    )
    supported = (
        not pilot
        and data_complete
        and sample_large_enough
        and acceptance_metrics["closing_value_count"] == len(acceptance_values)
        and _decimal_or_zero(acceptance_metrics["average_closing_value"]) > 0
        and _decimal_or_zero(acceptance_metrics["flat_roi"]) > 0
        and _decimal_or_zero(acceptance_metrics["mean_profit_ci95_lower"]) > 0
    )
    if pilot:
        conclusion = "exploratory_only"
        supported = False
    elif supported:
        conclusion = "supported"
    elif not data_complete or not sample_large_enough:
        conclusion = "inconclusive"
    else:
        conclusion = "negative"
    summary: dict[str, Any] = {
        "strategy": {
            "profile": settings.profile,
            "market": "NBA regular-season moneyline including overtime",
            "entry_minutes_before_tip": settings.entry_minutes_before_tip,
            "closing_minutes_before_tip": settings.closing_minutes_before_tip,
            "minimum_bookmakers": settings.minimum_bookmakers,
            "primary_ev_threshold": settings.primary_ev_threshold,
            "selection": "one highest-EV candidate per game",
            "probability_used_for_ev_and_kelly": (
                "5th-percentile weekly-block-bootstrap lower bound"
                if settings.calibration_enabled
                else "leave-one-bookmaker-out consensus"
            ),
            "calibration_enabled": settings.calibration_enabled,
            "calibration_method": settings.calibration_method,
            "calibration_training_fraction": (settings.calibration_training_fraction),
            "calibration_validation_fraction": (
                settings.calibration_validation_fraction
            ),
            "calibration_test_fraction": (
                Decimal(1)
                - settings.calibration_training_fraction
                - settings.calibration_validation_fraction
            ),
            "calibration_bootstrap_samples": (settings.calibration_bootstrap_samples),
            "calibration_block_days": settings.calibration_block_days,
            "calibration_lower_quantile": (settings.calibration_lower_quantile),
            "calibration_regularization": (settings.calibration_regularization),
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
        "probability_calibration": _calibration_comparison(run.calibration_predictions),
        "calibration_run": {
            "phase_game_counts": dict(run.calibration_metadata.phase_game_counts),
            "phase_observation_counts": dict(
                run.calibration_metadata.phase_observation_counts
            ),
            "validation_fit_observations": (
                run.calibration_metadata.validation_fit_observations
            ),
            "test_fit_observations": (run.calibration_metadata.test_fit_observations),
            "validation_trained_through": (
                run.calibration_metadata.validation_trained_through
            ),
            "test_trained_through": (run.calibration_metadata.test_trained_through),
            "single_season_limitation": (
                "2023-24 only; exploratory and not evidence of cross-season stability"
                if pilot
                else None
            ),
        },
        "development_threshold_exploration": (
            {}
            if settings.calibration_enabled
            else _development_thresholds(dataset, settings)
        ),
        "breakdowns": _breakdowns(run.settled),
        "acceptance": {
            "minimum_holdout_candidates": settings.minimum_holdout_candidates,
            "confirmatory_sample_check_applicable": not pilot,
            "sample_large_enough": sample_large_enough,
            "supported": supported,
            "conclusion": conclusion,
        },
    }
    if pilot:
        summary["pilot_validation_2023_24"] = _performance(pilot_validation)
        summary["pilot_test_2023_24"] = _performance(pilot_test)
        summary["threshold_exploration_note"] = (
            "Disabled for the calibrated pilot so the final 20% remains untouched."
        )
    return summary


def export_reports(
    *,
    output_directory: Path,
    dataset: BasketballDataset,
    run: BasketballBacktest,
    settings: BasketballSettings,
) -> tuple[Path, ...]:
    """Write game-, offer-, and summary-level reports without network access."""

    output_directory.mkdir(parents=True, exist_ok=True)
    games_path = output_directory / "nba_moneyline_games.csv"
    offers_path = output_directory / "nba_moneyline_offers.csv"
    summary_path = output_directory / "nba_moneyline_summary.json"
    candidates_path = output_directory / "nba_moneyline_candidates.csv"
    equity_path = output_directory / "nba_moneyline_equity_curve.csv"
    exclusions_path = output_directory / "nba_moneyline_exclusions.csv"
    summary_csv_path = output_directory / "nba_moneyline_summary.csv"
    candidate_by_game = {
        candidate.offer.game_id: candidate for candidate in run.candidates
    }
    settled_by_game = {value.candidate.offer.game_id: value for value in run.settled}
    with games_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "game_id",
                "season",
                "scheduled_start",
                "home_team",
                "away_team",
                "home_score",
                "away_score",
                "entry_bookmakers",
                "closing_bookmakers",
                "candidate_team",
                "bookmaker",
                "entry_odds",
                "consensus_probability",
                "calibrated_probability",
                "p_safe",
                "raw_expected_value",
                "expected_value",
                "safe_expected_value",
                "calibration_phase",
                "calibration_training_games",
                "closing_consensus_probability",
                "closing_value",
                "won",
                "flat_profit",
            ],
        )
        writer.writeheader()
        for game in dataset.games:
            candidate = candidate_by_game.get(game.game_id)
            settled = settled_by_game.get(game.game_id)
            result = dataset.results.get(game.game_id)
            writer.writerow(
                {
                    "game_id": game.game_id,
                    "season": game.season,
                    "scheduled_start": game.scheduled_start.isoformat(),
                    "home_team": game.home_team_id,
                    "away_team": game.away_team_id,
                    "home_score": result.home_score if result else "",
                    "away_score": result.away_score if result else "",
                    "entry_bookmakers": len(
                        {
                            snapshot.bookmaker_id
                            for snapshot in dataset.entry_snapshots
                            if snapshot.game_id == game.game_id
                        }
                    ),
                    "closing_bookmakers": len(
                        {
                            snapshot.bookmaker_id
                            for snapshot in dataset.closing_snapshots
                            if snapshot.game_id == game.game_id
                        }
                    ),
                    "candidate_team": (candidate.offer.team_id if candidate else ""),
                    "bookmaker": (candidate.offer.bookmaker_id if candidate else ""),
                    "entry_odds": (candidate.offer.offered_odds if candidate else ""),
                    "consensus_probability": (
                        candidate.offer.consensus_probability if candidate else ""
                    ),
                    "calibrated_probability": (
                        candidate.offer.calibrated_probability
                        if candidate
                        and candidate.offer.calibrated_probability is not None
                        else ""
                    ),
                    "p_safe": (
                        candidate.offer.conservative_probability
                        if candidate
                        and candidate.offer.conservative_probability is not None
                        else ""
                    ),
                    "raw_expected_value": (
                        candidate.offer.raw_expected_value if candidate else ""
                    ),
                    "expected_value": (
                        candidate.offer.expected_value if candidate else ""
                    ),
                    "safe_expected_value": (
                        candidate.offer.expected_value if candidate else ""
                    ),
                    "calibration_phase": (
                        candidate.offer.calibration_phase if candidate else ""
                    ),
                    "calibration_training_games": (
                        candidate.offer.calibration_training_games if candidate else ""
                    ),
                    "closing_value": (
                        candidate.closing_value
                        if candidate and candidate.closing_value is not None
                        else ""
                    ),
                    "closing_consensus_probability": (
                        candidate.closing_consensus_probability
                        if candidate
                        and candidate.closing_consensus_probability is not None
                        else ""
                    ),
                    "won": settled.won if settled else "",
                    "flat_profit": settled.flat_profit if settled else "",
                }
            )
    with offers_path.open("w", newline="", encoding="utf-8") as handle:
        base_fields = (
            tuple(asdict(run.offers[0]))
            if run.offers
            else (
                "game_id",
                "season",
                "bookmaker_id",
                "team_id",
                "opponent_id",
                "offered_odds",
                "consensus_probability",
                "calibrated_probability",
                "conservative_probability",
                "raw_expected_value",
                "expected_value",
                "observed_at",
                "favourite",
                "calibration_phase",
                "calibration_training_games",
            )
        )
        fields = base_fields + ("safe_expected_value",)
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for offer in run.offers:
            row = asdict(offer)
            row["observed_at"] = offer.observed_at.isoformat()
            row["safe_expected_value"] = offer.expected_value
            writer.writerow(row)
    _write_candidates_csv(
        candidates_path,
        dataset=dataset,
        run=run,
    )
    _write_equity_csv(
        equity_path,
        dataset=dataset,
        run=run,
        settings=settings,
    )
    _write_exclusions_csv(exclusions_path, dataset)
    summary = build_summary(dataset, run, settings)
    summary_path.write_text(
        json.dumps(
            summary,
            indent=2,
            sort_keys=True,
            default=_json_default,
        )
        + "\n"
    )
    _write_summary_csv(summary_csv_path, summary)
    return (
        games_path,
        offers_path,
        summary_path,
        candidates_path,
        equity_path,
        exclusions_path,
        summary_csv_path,
    )


def _write_candidates_csv(
    path: Path,
    *,
    dataset: BasketballDataset,
    run: BasketballBacktest,
) -> None:
    games = {game.game_id: game for game in dataset.games}
    settled = {value.candidate.offer.game_id: value for value in run.settled}
    fields = (
        "game_id",
        "season",
        "scheduled_start",
        "home_team",
        "away_team",
        "selected_team",
        "opponent",
        "bookmaker",
        "entry_observed_at",
        "entry_odds",
        "consensus_probability",
        "calibrated_probability",
        "p_safe",
        "raw_expected_value",
        "expected_value",
        "safe_expected_value",
        "calibration_phase",
        "calibration_training_games",
        "closing_consensus_probability",
        "closing_value",
        "favourite",
        "won",
        "flat_profit",
        "kelly_stake",
        "kelly_profit",
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for candidate in run.candidates:
            offer = candidate.offer
            game = games[offer.game_id]
            result = settled.get(offer.game_id)
            writer.writerow(
                {
                    "game_id": game.game_id,
                    "season": game.season,
                    "scheduled_start": game.scheduled_start.isoformat(),
                    "home_team": game.home_team_id,
                    "away_team": game.away_team_id,
                    "selected_team": offer.team_id,
                    "opponent": offer.opponent_id,
                    "bookmaker": offer.bookmaker_id,
                    "entry_observed_at": offer.observed_at.isoformat(),
                    "entry_odds": offer.offered_odds,
                    "consensus_probability": offer.consensus_probability,
                    "calibrated_probability": (
                        offer.calibrated_probability
                        if offer.calibrated_probability is not None
                        else ""
                    ),
                    "p_safe": (
                        offer.conservative_probability
                        if offer.conservative_probability is not None
                        else ""
                    ),
                    "raw_expected_value": offer.raw_expected_value,
                    "expected_value": offer.expected_value,
                    "safe_expected_value": offer.expected_value,
                    "calibration_phase": offer.calibration_phase,
                    "calibration_training_games": (offer.calibration_training_games),
                    "closing_consensus_probability": (
                        candidate.closing_consensus_probability
                        if candidate.closing_consensus_probability is not None
                        else ""
                    ),
                    "closing_value": (
                        candidate.closing_value
                        if candidate.closing_value is not None
                        else ""
                    ),
                    "favourite": offer.favourite,
                    "won": result.won if result else "",
                    "flat_profit": result.flat_profit if result else "",
                    "kelly_stake": result.kelly_stake if result else "",
                    "kelly_profit": result.kelly_profit if result else "",
                }
            )


def _write_equity_csv(
    path: Path,
    *,
    dataset: BasketballDataset,
    run: BasketballBacktest,
    settings: BasketballSettings,
) -> None:
    games = {game.game_id: game for game in dataset.games}
    ordered = sorted(
        run.settled,
        key=lambda value: games[value.candidate.offer.game_id].scheduled_start,
    )
    flat_equity = 0.0
    flat_peak = 0.0
    kelly_equity = float(settings.kelly_starting_equity)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "sequence",
                "game_id",
                "scheduled_start",
                "flat_equity_units",
                "flat_drawdown_units",
                "kelly_equity",
            ),
        )
        writer.writeheader()
        for sequence, value in enumerate(ordered, start=1):
            game = games[value.candidate.offer.game_id]
            flat_equity += float(value.flat_profit)
            flat_peak = max(flat_peak, flat_equity)
            kelly_equity += float(value.kelly_profit)
            writer.writerow(
                {
                    "sequence": sequence,
                    "game_id": game.game_id,
                    "scheduled_start": game.scheduled_start.isoformat(),
                    "flat_equity_units": flat_equity,
                    "flat_drawdown_units": flat_equity - flat_peak,
                    "kelly_equity": kelly_equity,
                }
            )


def _write_exclusions_csv(path: Path, dataset: BasketballDataset) -> None:
    rows: list[dict[str, str]] = []
    for value in dataset.market_exclusions:
        event_id, bookmaker, reason = value.split(":", 2)
        rows.append(
            {
                "scope": "market",
                "event_id": event_id,
                "bookmaker": bookmaker,
                "reason": reason,
            }
        )
    rows.extend(
        {
            "scope": "odds_event",
            "event_id": event_id,
            "bookmaker": "",
            "reason": "unmatched_or_ambiguous_result",
        }
        for event_id in dataset.quarantined_event_ids
    )
    for value in dataset.result_quarantines:
        event_id, reason = value.split(":", 1)
        rows.append(
            {
                "scope": "result",
                "event_id": event_id,
                "bookmaker": "",
                "reason": reason,
            }
        )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("scope", "event_id", "bookmaker", "reason"),
        )
        writer.writeheader()
        writer.writerows(rows)


def _write_summary_csv(path: Path, summary: dict[str, Any]) -> None:
    rows: list[tuple[str, object]] = []
    for section in ("strategy", "coverage", "overall", "acceptance"):
        values = summary.get(section)
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if isinstance(value, dict | list | tuple):
                continue
            rows.append((f"{section}.{key}", value))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("metric", "value"))
        writer.writerows(rows)


def _performance(values: tuple[SettledCandidate, ...]) -> dict[str, Any]:
    profits = [float(value.flat_profit) for value in values]
    closing = [
        float(value.candidate.closing_value)
        for value in values
        if value.candidate.closing_value is not None
    ]
    raw_probabilities = [
        float(value.candidate.offer.consensus_probability) for value in values
    ]
    calibrated_probabilities = [
        float(
            value.candidate.offer.calibrated_probability
            if value.candidate.offer.calibrated_probability is not None
            else value.candidate.offer.consensus_probability
        )
        for value in values
    ]
    probabilities = [
        float(value.candidate.offer.decision_probability) for value in values
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
    raw_probability_metrics = _probability_metrics(raw_probabilities, outcomes)
    calibrated_probability_metrics = _probability_metrics(
        calibrated_probabilities, outcomes
    )
    decision_probability_metrics = _probability_metrics(probabilities, outcomes)
    return {
        "candidates": len(values),
        "turnover_units": len(values),
        "flat_profit_units": sum(profits),
        "flat_roi": mean(profits) if profits else None,
        "hit_rate": mean(outcomes) if outcomes else None,
        "average_closing_value": mean(closing) if closing else None,
        "closing_value_count": len(closing),
        "raw_brier_score": raw_probability_metrics["brier_score"],
        "point_calibrated_brier_score": (calibrated_probability_metrics["brier_score"]),
        "brier_score": decision_probability_metrics["brier_score"],
        "calibration": decision_probability_metrics["calibration"],
        "raw_log_loss": raw_probability_metrics["log_loss"],
        "point_calibrated_log_loss": (calibrated_probability_metrics["log_loss"]),
        "log_loss": decision_probability_metrics["log_loss"],
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


def _calibration_comparison(
    predictions: tuple[CalibrationPrediction, ...],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for phase in ("validation", "test", "combined"):
        selected = (
            predictions
            if phase == "combined"
            else tuple(value for value in predictions if value.phase == phase)
        )
        raw = [float(value.raw_home_probability) for value in selected]
        calibrated = [float(value.calibrated_home_probability) for value in selected]
        outcomes = [1.0 if value.home_won else 0.0 for value in selected]
        raw_metrics = _probability_metrics(raw, outcomes)
        calibrated_metrics = _probability_metrics(calibrated, outcomes)
        result[phase] = {
            "observations": len(selected),
            "raw": raw_metrics,
            "calibrated": calibrated_metrics,
            "brier_change": _metric_change(
                calibrated_metrics["brier_score"],
                raw_metrics["brier_score"],
            ),
            "log_loss_change": _metric_change(
                calibrated_metrics["log_loss"],
                raw_metrics["log_loss"],
            ),
        }
    return result


def _probability_metrics(
    probabilities: list[float], outcomes: list[float]
) -> dict[str, Any]:
    if not probabilities:
        return {"brier_score": None, "log_loss": None, "calibration": []}
    log_losses = [
        -(outcome * math.log(max(probability, 1e-12)))
        - ((1 - outcome) * math.log(max(1 - probability, 1e-12)))
        for probability, outcome in zip(probabilities, outcomes, strict=True)
    ]
    return {
        "brier_score": mean(
            (probability - outcome) ** 2
            for probability, outcome in zip(probabilities, outcomes, strict=True)
        ),
        "log_loss": mean(log_losses),
        "calibration": _calibration(probabilities, outcomes),
    }


def _metric_change(
    calibrated: object,
    raw: object,
) -> float | None:
    if not isinstance(calibrated, float) or not isinstance(raw, float):
        return None
    return calibrated - raw


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
        dimensions["side"]["favourite" if offer.favourite else "underdog"].append(value)
        dimensions["odds_band"][_band(float(offer.offered_odds), (1.5, 2, 3))].append(
            value
        )
        dimensions["ev_band"][
            _band(float(offer.expected_value), (0.075, 0.10, 0.15))
        ].append(value)
    return {
        dimension: {
            label: _performance(tuple(group)) for label, group in sorted(groups.items())
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
