"""Offline ATP/WTA calibration, conservative selection and research exports."""

import csv
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

from betting_core.calibration import (
    BootstrapCalibration,
    CalibrationObservation,
    fit_bootstrap_calibration,
)
from tennis_value.backtesting import (
    KellyBacktestReport,
    KellySettings,
    MatchResult,
    ResultStatus,
    SelectedCandidate,
    backtest_kelly_candidates,
    select_one_candidate_per_match,
)
from tennis_value.config import AppSettings
from tennis_value.data.domain import Match, OddsSnapshot
from tennis_value.oddspapi_backtest import snapshots_at_decision
from tennis_value.oddspapi_research import (
    ResearchFixture,
    load_cached_histories,
    load_cached_research_fixtures,
)
from tennis_value.pricing import power_devig
from tennis_value.research_config import TennisResearchSettings
from tennis_value.results_csv import ResultMatching, match_result_csvs
from tennis_value.signals import MarketEvaluationError, OfferEvaluation, evaluate_market


@dataclass(frozen=True, slots=True)
class MatchAnalysis:
    fixture: ResearchFixture
    phase: str
    result_winner_id: int | None
    result_matched: bool
    entry_bookmakers: int
    raw_player_one_probability: Decimal | None
    calibrated_player_one_probability: Decimal | None
    safe_player_one_probability: Decimal | None
    entry_quote_age_seconds: Decimal | None
    closing_player_one_probability: Decimal | None


@dataclass(frozen=True, slots=True)
class CalibratedOffer:
    fixture: ResearchFixture
    phase: str
    raw: OfferEvaluation
    quote_observed_at: datetime
    quote_age_seconds: Decimal
    winner_player_id: int
    calibrated_probability: Decimal | None
    safe_probability: Decimal | None
    raw_expected_value: Decimal
    calibrated_expected_value: Decimal | None
    safe_expected_value: Decimal | None
    closing_probability: Decimal | None
    closing_value: Decimal | None
    selected: bool = False


@dataclass(frozen=True, slots=True)
class TourFit:
    tour: str
    phases: dict[str, tuple[CalibrationObservation, ...]]
    validation_model: BootstrapCalibration | None
    test_model: BootstrapCalibration | None
    match_rate: Decimal
    conclusion: str


@dataclass(frozen=True, slots=True)
class TennisCalibrationExport:
    output_directory: Path
    artifact_directory: Path
    matches_path: Path
    offers_path: Path
    candidates_path: Path
    calibration_bins_path: Path
    equity_curve_path: Path
    exclusions_path: Path
    summary_csv_path: Path
    summary_json_path: Path
    fixture_count: int
    matched_results: int
    offer_count: int
    candidate_count: int
    conclusion: str


@dataclass(frozen=True, slots=True)
class _EligibleMarket:
    fixture: ResearchFixture
    result_winner_id: int
    entry_snapshots: tuple[OddsSnapshot, ...]
    closing_snapshots: tuple[OddsSnapshot, ...]
    raw_player_one_probability: Decimal
    closing_player_one_probability: Decimal | None
    offers: tuple[OfferEvaluation, ...]


def export_tennis_calibration_report(
    *,
    research_settings: TennisResearchSettings,
    model_settings: AppSettings,
    cache_directory: Path,
    atp_results_csv: Path,
    wta_results_csv: Path,
    artifact_directory: Path,
    output_directory: Path,
) -> TennisCalibrationExport:
    """Run the complete cached study and write inspectable CSV/JSON outputs."""

    _validate_model_settings(research_settings, model_settings)
    fixtures = load_cached_research_fixtures(
        settings=research_settings,
        cache_directory=cache_directory,
    )
    result_matching = match_result_csvs(
        fixtures,
        atp_path=atp_results_csv,
        wta_path=wta_results_csv,
    )
    eligible, market_exclusions = _build_eligible_markets(
        fixtures,
        result_matching=result_matching,
        research_settings=research_settings,
        model_settings=model_settings,
        cache_directory=cache_directory,
    )
    fits = _fit_tours(
        fixtures,
        eligible=eligible,
        result_matching=result_matching,
        settings=research_settings,
    )
    analyses, offers = _predict_all(eligible, fits=fits)
    candidates, selected_offers = _select_candidates(
        offers,
        threshold=research_settings.ev_threshold,
    )
    results = _backtest_results(fixtures, result_matching)
    kelly = backtest_kelly_candidates(
        candidates,
        results,
        settings=KellySettings(
            initial_equity=research_settings.starting_equity,
            kelly_fraction=research_settings.kelly_fraction,
        ),
    )
    all_analyses = _include_unavailable_matches(
        fixtures,
        eligible=eligible,
        predicted=analyses,
        result_matching=result_matching,
    )
    source_checksums = _source_checksums(
        cache_directory,
        result_matching=result_matching,
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    artifact_directory.mkdir(parents=True, exist_ok=True)
    _write_model_artifacts(
        fits,
        settings=research_settings,
        result_matching=result_matching,
        source_checksums=source_checksums,
        artifact_directory=artifact_directory,
    )
    paths = _report_paths(output_directory)
    _write_matches(paths["matches"], all_analyses)
    _write_offers(paths["offers"], selected_offers)
    _write_candidates(paths["candidates"], selected_offers, kelly)
    _write_calibration_bins(paths["bins"], analyses)
    _write_equity_curve(paths["equity"], kelly)
    _write_exclusions(
        paths["exclusions"],
        result_matching=result_matching,
        market_exclusions=market_exclusions,
    )
    summary = _summary(
        fixtures=fixtures,
        analyses=analyses,
        offers=selected_offers,
        fits=fits,
        kelly=kelly,
        source_checksums=source_checksums,
        settings=research_settings,
    )
    _atomic_text(
        paths["summary_json"], json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    _write_summary_csv(paths["summary_csv"], summary)
    return TennisCalibrationExport(
        output_directory=output_directory,
        artifact_directory=artifact_directory,
        matches_path=paths["matches"],
        offers_path=paths["offers"],
        candidates_path=paths["candidates"],
        calibration_bins_path=paths["bins"],
        equity_curve_path=paths["equity"],
        exclusions_path=paths["exclusions"],
        summary_csv_path=paths["summary_csv"],
        summary_json_path=paths["summary_json"],
        fixture_count=len(fixtures),
        matched_results=len(result_matching.results),
        offer_count=len(selected_offers),
        candidate_count=len(candidates),
        conclusion="exploratory_only",
    )


def assign_chronological_phases(
    observations: Sequence[CalibrationObservation],
    *,
    training_fraction: Decimal,
    validation_fraction: Decimal,
) -> dict[str, tuple[CalibrationObservation, ...]]:
    """Create whole-date 60/20/20 phases without crossing a future boundary."""

    ordered = tuple(
        sorted(observations, key=lambda value: (value.scheduled_start, value.game_id))
    )
    if not ordered:
        return {"training": (), "validation": (), "test": ()}
    by_date: dict[date, list[CalibrationObservation]] = defaultdict(list)
    for observation in ordered:
        by_date[observation.scheduled_start.date()].append(observation)
    train_target = Decimal(len(ordered)) * training_fraction
    validation_target = Decimal(len(ordered)) * (
        training_fraction + validation_fraction
    )
    phases: dict[str, list[CalibrationObservation]] = {
        "training": [],
        "validation": [],
        "test": [],
    }
    assigned = 0
    for match_date in sorted(by_date):
        rows = by_date[match_date]
        midpoint = Decimal(assigned) + Decimal(len(rows)) / Decimal(2)
        if midpoint <= train_target:
            phase = "training"
        elif midpoint <= validation_target:
            phase = "validation"
        else:
            phase = "test"
        phases[phase].extend(rows)
        assigned += len(rows)
    return {key: tuple(value) for key, value in phases.items()}


def _build_eligible_markets(
    fixtures: tuple[ResearchFixture, ...],
    *,
    result_matching: ResultMatching,
    research_settings: TennisResearchSettings,
    model_settings: AppSettings,
    cache_directory: Path,
) -> tuple[dict[str, _EligibleMarket], list[dict[str, str]]]:
    eligible: dict[str, _EligibleMarket] = {}
    exclusions: list[dict[str, str]] = []
    for fixture in fixtures:
        matched = result_matching.results.get(fixture.fixture_id)
        if matched is None:
            continue
        try:
            payloads = load_cached_histories(
                fixture.fixture_id,
                settings=research_settings,
                cache_directory=cache_directory,
            )
            entry = snapshots_at_decision(
                fixture_id=fixture.fixture_id,
                player_one_id=fixture.player_one_id,
                player_two_id=fixture.player_two_id,
                scheduled_start=fixture.scheduled_start,
                payloads=payloads,
                minutes_before_start=research_settings.entry_minutes_before_start,
            )
            closing = snapshots_at_decision(
                fixture_id=fixture.fixture_id,
                player_one_id=fixture.player_one_id,
                player_two_id=fixture.player_two_id,
                scheduled_start=fixture.scheduled_start,
                payloads=payloads,
                minutes_before_start=research_settings.closing_minutes_before_start,
            )
            decision_at = fixture.scheduled_start - timedelta(
                minutes=research_settings.entry_minutes_before_start
            )
            match = _match(fixture)
            evaluated = evaluate_market(
                match,
                entry,
                decision_at=decision_at,
                calculated_at=decision_at,
                settings=model_settings,
            )
            pinnacle = _pinnacle_probability(
                entry,
                fixture.player_one_id,
                calculated_at=decision_at,
            )
            closing_probability = _pinnacle_probability_or_none(
                closing,
                fixture.player_one_id,
                calculated_at=fixture.scheduled_start
                - timedelta(minutes=research_settings.closing_minutes_before_start),
            )
        except (FileNotFoundError, ValueError, MarketEvaluationError) as error:
            exclusions.append(
                {
                    "fixture_id": fixture.fixture_id,
                    "tour": fixture.tour,
                    "stage": "market",
                    "reason": type(error).__name__,
                    "detail": str(error),
                }
            )
            continue
        eligible[fixture.fixture_id] = _EligibleMarket(
            fixture=fixture,
            result_winner_id=matched.winner_player_id,
            entry_snapshots=entry,
            closing_snapshots=closing,
            raw_player_one_probability=pinnacle,
            closing_player_one_probability=closing_probability,
            offers=evaluated.evaluations,
        )
    return eligible, exclusions


def _fit_tours(
    fixtures: tuple[ResearchFixture, ...],
    *,
    eligible: Mapping[str, _EligibleMarket],
    result_matching: ResultMatching,
    settings: TennisResearchSettings,
) -> dict[str, TourFit]:
    observations_by_tour = {
        tour: tuple(
            CalibrationObservation(
                game_id=value.fixture.fixture_id,
                scheduled_start=value.fixture.scheduled_start,
                home_probability=value.raw_player_one_probability,
                home_won=value.result_winner_id == value.fixture.player_one_id,
            )
            for value in eligible.values()
            if value.fixture.tour == tour
        )
        for tour in ("ATP", "WTA")
    }
    match_rates: dict[str, Decimal] = {}
    for tour in ("ATP", "WTA"):
        tour_fixtures = sum(value.tour == tour for value in fixtures)
        tour_matches = sum(
            fixture.tour == tour and fixture.fixture_id in result_matching.results
            for fixture in fixtures
        )
        match_rates[tour] = (
            Decimal(tour_matches) / Decimal(tour_fixtures)
            if tour_fixtures
            else Decimal(0)
        )
    return fit_tour_calibrations(
        observations_by_tour,
        match_rates=match_rates,
        settings=settings,
    )


def fit_tour_calibrations(
    observations_by_tour: Mapping[str, tuple[CalibrationObservation, ...]],
    *,
    match_rates: Mapping[str, Decimal],
    settings: TennisResearchSettings,
) -> dict[str, TourFit]:
    """Fit isolated ATP/WTA validation and test models."""

    if set(observations_by_tour) != {"ATP", "WTA"}:
        raise ValueError("observations_by_tour must contain exactly ATP and WTA")
    if set(match_rates) != {"ATP", "WTA"}:
        raise ValueError("match_rates must contain exactly ATP and WTA")
    fits: dict[str, TourFit] = {}
    for tour_index, tour in enumerate(("ATP", "WTA")):
        observations = observations_by_tour[tour]
        phases = assign_chronological_phases(
            observations,
            training_fraction=settings.training_fraction,
            validation_fraction=settings.validation_fraction,
        )
        validation_model = None
        test_model = None
        if len(phases["training"]) >= settings.minimum_training_matches:
            validation_model = _fit(phases["training"], settings, tour_index)
            test_model = _fit(
                phases["training"] + phases["validation"],
                settings,
                tour_index + 10,
            )
        match_rate = match_rates[tour]
        conclusion = (
            "inconclusive"
            if validation_model is None or match_rate < Decimal("0.95")
            else "exploratory_only"
        )
        fits[tour] = TourFit(
            tour=tour,
            phases=phases,
            validation_model=validation_model,
            test_model=test_model,
            match_rate=match_rate,
            conclusion=conclusion,
        )
    return fits


def _fit(
    observations: tuple[CalibrationObservation, ...],
    settings: TennisResearchSettings,
    seed_offset: int,
) -> BootstrapCalibration:
    return fit_bootstrap_calibration(
        observations,
        bootstrap_samples=settings.bootstrap_samples,
        block_days=settings.block_days,
        lower_quantile=settings.lower_quantile,
        regularization=settings.regularization,
        random_seed=settings.random_seed + seed_offset,
    )


def _predict_all(
    eligible: Mapping[str, _EligibleMarket],
    *,
    fits: Mapping[str, TourFit],
) -> tuple[list[MatchAnalysis], list[CalibratedOffer]]:
    analyses: list[MatchAnalysis] = []
    offers: list[CalibratedOffer] = []
    phase_by_match = {
        observation.game_id: phase
        for fit in fits.values()
        for phase, observations in fit.phases.items()
        for observation in observations
    }
    for fixture_id, market in sorted(
        eligible.items(), key=lambda item: item[1].fixture.scheduled_start
    ):
        phase = phase_by_match[fixture_id]
        model = _model_for_phase(fits[market.fixture.tour], phase)
        calibrated_home: Decimal | None = None
        safe_home: Decimal | None = None
        if model is not None:
            calibrated_home, safe_home = model.probabilities_for_side(
                market.raw_player_one_probability,
                home_side=True,
            )
        for raw in market.offers:
            point: Decimal | None = None
            safe: Decimal | None = None
            if model is not None:
                point, safe = model.probabilities_for_side(
                    market.raw_player_one_probability,
                    home_side=raw.player_id == market.fixture.player_one_id,
                )
            snapshot = next(
                value
                for value in market.entry_snapshots
                if value.snapshot_id == raw.snapshot_id
            )
            closing = _side_probability(
                market.closing_player_one_probability,
                home_side=raw.player_id == market.fixture.player_one_id,
            )
            offers.append(
                CalibratedOffer(
                    fixture=market.fixture,
                    phase=phase,
                    raw=raw,
                    quote_observed_at=snapshot.observed_at,
                    quote_age_seconds=Decimal(
                        str((raw.decision_at - snapshot.observed_at).total_seconds())
                    ),
                    winner_player_id=market.result_winner_id,
                    calibrated_probability=point,
                    safe_probability=safe,
                    raw_expected_value=raw.expected_value,
                    calibrated_expected_value=(
                        raw.offered_odds * point - Decimal(1)
                        if point is not None
                        else None
                    ),
                    safe_expected_value=(
                        raw.offered_odds * safe - Decimal(1)
                        if safe is not None
                        else None
                    ),
                    closing_probability=closing,
                    closing_value=(
                        closing - raw.consensus_probability
                        if closing is not None
                        else None
                    ),
                )
            )
        oldest_quote_age = max(
            (
                Decimal(
                    str(
                        (
                            market.fixture.scheduled_start
                            - timedelta(minutes=60)
                            - snapshot.observed_at
                        ).total_seconds()
                    )
                )
                for snapshot in market.entry_snapshots
            ),
            default=None,
        )
        analyses.append(
            MatchAnalysis(
                fixture=market.fixture,
                phase=phase,
                result_winner_id=market.result_winner_id,
                result_matched=True,
                entry_bookmakers=len(market.entry_snapshots),
                raw_player_one_probability=market.raw_player_one_probability,
                calibrated_player_one_probability=calibrated_home,
                safe_player_one_probability=safe_home,
                entry_quote_age_seconds=oldest_quote_age,
                closing_player_one_probability=market.closing_player_one_probability,
            )
        )
    return analyses, offers


def _select_candidates(
    offers: list[CalibratedOffer],
    *,
    threshold: Decimal,
) -> tuple[tuple[SelectedCandidate, ...], list[CalibratedOffer]]:
    candidates = tuple(
        SelectedCandidate(
            match_id=offer.fixture.fixture_id,
            player_id=offer.raw.player_id,
            bookmaker_id=offer.raw.bookmaker_id,
            snapshot_id=offer.raw.snapshot_id,
            offered_odds=offer.raw.offered_odds,
            consensus_probability=offer.raw.consensus_probability,
            expected_value=_required(offer.safe_expected_value),
            decision_at=offer.raw.decision_at,
            staking_probability=_required(offer.safe_probability),
        )
        for offer in offers
        if offer.phase in {"validation", "test"}
        and offer.safe_probability is not None
        and offer.safe_expected_value is not None
        and offer.safe_expected_value >= threshold
    )
    selected = select_one_candidate_per_match(candidates)
    selected_keys = {
        (value.match_id, value.player_id, value.snapshot_id) for value in selected
    }
    return selected, [
        replace(
            offer,
            selected=(
                offer.fixture.fixture_id,
                offer.raw.player_id,
                offer.raw.snapshot_id,
            )
            in selected_keys,
        )
        for offer in offers
    ]


def _backtest_results(
    fixtures: tuple[ResearchFixture, ...],
    matching: ResultMatching,
) -> dict[str, MatchResult]:
    fixture_by_id = {value.fixture_id: value for value in fixtures}
    return {
        fixture_id: MatchResult(
            match_id=fixture_id,
            status=ResultStatus.COMPLETED,
            settled_at=fixture_by_id[fixture_id].scheduled_start + timedelta(hours=4),
            source="tennisdata.app:local-csv",
            winner_player_id=value.winner_player_id,
        )
        for fixture_id, value in matching.results.items()
    }


def _include_unavailable_matches(
    fixtures: tuple[ResearchFixture, ...],
    *,
    eligible: Mapping[str, _EligibleMarket],
    predicted: list[MatchAnalysis],
    result_matching: ResultMatching,
) -> list[MatchAnalysis]:
    by_id = {value.fixture.fixture_id: value for value in predicted}
    for fixture in fixtures:
        if fixture.fixture_id in by_id:
            continue
        by_id[fixture.fixture_id] = MatchAnalysis(
            fixture=fixture,
            phase="excluded",
            result_winner_id=(
                result_matching.results[fixture.fixture_id].winner_player_id
                if fixture.fixture_id in result_matching.results
                else None
            ),
            result_matched=fixture.fixture_id in result_matching.results,
            entry_bookmakers=(
                len(eligible[fixture.fixture_id].entry_snapshots)
                if fixture.fixture_id in eligible
                else 0
            ),
            raw_player_one_probability=None,
            calibrated_player_one_probability=None,
            safe_player_one_probability=None,
            entry_quote_age_seconds=None,
            closing_player_one_probability=None,
        )
    return sorted(by_id.values(), key=lambda value: value.fixture.scheduled_start)


def _write_model_artifacts(
    fits: Mapping[str, TourFit],
    *,
    settings: TennisResearchSettings,
    result_matching: ResultMatching,
    source_checksums: Mapping[str, str],
    artifact_directory: Path,
) -> None:
    for tour, fit in fits.items():
        observations = fit.phases["training"] + fit.phases["validation"]
        payload: dict[str, Any] = {
            "schema_version": 1,
            "sport": "tennis",
            "tour": tour,
            "profile": settings.profile,
            "conclusion": fit.conclusion,
            "configuration": _configuration_payload(settings),
            "phase_match_counts": {
                key: len(value) for key, value in fit.phases.items()
            },
            "result_match_rate": str(fit.match_rate),
            "result_csv_sha256": result_matching.source_checksums[tour],
            "source_checksums": dict(source_checksums),
            "match_count": len(observations),
            "match_hash": _observation_hash(observations),
            "model": _model_payload(fit.test_model),
        }
        _atomic_text(
            artifact_directory / f"{tour.lower()}.json",
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )


def _write_matches(path: Path, analyses: Iterable[MatchAnalysis]) -> None:
    fields = (
        "fixture_id",
        "tour",
        "surface",
        "tournament_key",
        "tournament_name",
        "scheduled_start",
        "phase",
        "player_one_id",
        "player_one_name",
        "player_two_id",
        "player_two_name",
        "winner_player_id",
        "result_matched",
        "entry_bookmakers",
        "raw_player_one_probability",
        "calibrated_player_one_probability",
        "safe_player_one_probability",
        "closing_player_one_probability",
        "oldest_entry_quote_age_seconds",
        "freshness_enforced",
    )
    rows = (
        {
            "fixture_id": value.fixture.fixture_id,
            "tour": value.fixture.tour,
            "surface": value.fixture.surface,
            "tournament_key": value.fixture.tournament_key,
            "tournament_name": value.fixture.tournament_name,
            "scheduled_start": value.fixture.scheduled_start.isoformat(),
            "phase": value.phase,
            "player_one_id": value.fixture.player_one_id,
            "player_one_name": value.fixture.player_one_name,
            "player_two_id": value.fixture.player_two_id,
            "player_two_name": value.fixture.player_two_name,
            "winner_player_id": value.result_winner_id or "",
            "result_matched": value.result_matched,
            "entry_bookmakers": value.entry_bookmakers,
            "raw_player_one_probability": _optional(value.raw_player_one_probability),
            "calibrated_player_one_probability": _optional(
                value.calibrated_player_one_probability
            ),
            "safe_player_one_probability": _optional(value.safe_player_one_probability),
            "closing_player_one_probability": _optional(
                value.closing_player_one_probability
            ),
            "oldest_entry_quote_age_seconds": _optional(value.entry_quote_age_seconds),
            "freshness_enforced": False,
        }
        for value in analyses
    )
    _write_csv(path, fields, rows)


def _write_offers(path: Path, offers: Iterable[CalibratedOffer]) -> None:
    fields = (
        "fixture_id",
        "tour",
        "surface",
        "tournament_key",
        "scheduled_start",
        "phase",
        "decision_at",
        "bookmaker",
        "snapshot_id",
        "quote_observed_at",
        "quote_age_seconds",
        "freshness_enforced",
        "player_id",
        "offered_odds",
        "raw_consensus_probability",
        "calibrated_probability",
        "p_safe",
        "raw_expected_value",
        "calibrated_expected_value",
        "safe_expected_value",
        "passes_safe_ev_threshold",
        "selected",
        "closing_probability",
        "closing_value",
    )
    rows = (
        {
            "fixture_id": value.fixture.fixture_id,
            "tour": value.fixture.tour,
            "surface": value.fixture.surface,
            "tournament_key": value.fixture.tournament_key,
            "scheduled_start": value.fixture.scheduled_start.isoformat(),
            "phase": value.phase,
            "decision_at": value.raw.decision_at.isoformat(),
            "bookmaker": value.raw.bookmaker_id,
            "snapshot_id": value.raw.snapshot_id,
            "quote_observed_at": value.quote_observed_at.isoformat(),
            "quote_age_seconds": value.quote_age_seconds,
            "freshness_enforced": False,
            "player_id": value.raw.player_id,
            "offered_odds": value.raw.offered_odds,
            "raw_consensus_probability": value.raw.consensus_probability,
            "calibrated_probability": _optional(value.calibrated_probability),
            "p_safe": _optional(value.safe_probability),
            "raw_expected_value": value.raw_expected_value,
            "calibrated_expected_value": _optional(value.calibrated_expected_value),
            "safe_expected_value": _optional(value.safe_expected_value),
            "passes_safe_ev_threshold": (
                value.safe_expected_value is not None
                and value.safe_expected_value >= Decimal("0.04")
            ),
            "selected": value.selected,
            "closing_probability": _optional(value.closing_probability),
            "closing_value": _optional(value.closing_value),
        }
        for value in offers
    )
    _write_csv(path, fields, rows)


def _write_candidates(
    path: Path,
    offers: Iterable[CalibratedOffer],
    report: KellyBacktestReport,
) -> None:
    bet_by_key = {
        (
            bet.candidate.match_id,
            bet.candidate.player_id,
            bet.candidate.snapshot_id,
        ): bet
        for bet in report.bets
    }
    fields = (
        "fixture_id",
        "tour",
        "surface",
        "phase",
        "bookmaker",
        "player_id",
        "offered_odds",
        "raw_probability",
        "calibrated_probability",
        "p_safe",
        "raw_ev",
        "calibrated_ev",
        "safe_ev",
        "kelly_stake",
        "settlement",
        "profit",
        "equity_after",
        "drawdown",
        "closing_value",
    )
    rows: list[dict[str, Any]] = []
    for value in offers:
        if not value.selected:
            continue
        if value.safe_probability is None or value.safe_expected_value is None:
            raise AssertionError("selected candidates require conservative values")
        key = (
            value.fixture.fixture_id,
            value.raw.player_id,
            value.raw.snapshot_id,
        )
        bet = bet_by_key[key]
        rows.append(
            {
                "fixture_id": value.fixture.fixture_id,
                "tour": value.fixture.tour,
                "surface": value.fixture.surface,
                "phase": value.phase,
                "bookmaker": value.raw.bookmaker_id,
                "player_id": value.raw.player_id,
                "offered_odds": value.raw.offered_odds,
                "raw_probability": value.raw.consensus_probability,
                "calibrated_probability": value.calibrated_probability,
                "p_safe": value.safe_probability,
                "raw_ev": value.raw_expected_value,
                "calibrated_ev": value.calibrated_expected_value,
                "safe_ev": value.safe_expected_value,
                "kelly_stake": bet.stake,
                "settlement": bet.status.value,
                "profit": bet.profit,
                "equity_after": bet.available_equity_after,
                "drawdown": bet.drawdown,
                "closing_value": _optional(value.closing_value),
            }
        )
    _write_csv(path, fields, rows)


def _write_calibration_bins(path: Path, analyses: Iterable[MatchAnalysis]) -> None:
    grouped: dict[tuple[str, str, str, int], list[tuple[Decimal, bool]]] = defaultdict(
        list
    )
    for value in analyses:
        if value.phase not in {"validation", "test"}:
            continue
        prediction_values = {
            "raw": value.raw_player_one_probability,
            "calibrated": value.calibrated_player_one_probability,
            "p_safe": value.safe_player_one_probability,
        }
        for kind, probability in prediction_values.items():
            if probability is None or value.result_winner_id is None:
                continue
            index = min(9, int(probability * Decimal(10)))
            grouped[(value.fixture.tour, value.phase, kind, index)].append(
                (probability, value.result_winner_id == value.fixture.player_one_id)
            )
    fields = (
        "tour",
        "phase",
        "probability_type",
        "bin",
        "lower_bound",
        "upper_bound",
        "count",
        "average_probability",
        "observed_win_rate",
    )
    bin_rows: list[dict[str, Any]] = []
    for (tour, phase, kind, index), bin_values in sorted(grouped.items()):
        count = len(bin_values)
        bin_rows.append(
            {
                "tour": tour,
                "phase": phase,
                "probability_type": kind,
                "bin": index,
                "lower_bound": Decimal(index) / Decimal(10),
                "upper_bound": Decimal(index + 1) / Decimal(10),
                "count": count,
                "average_probability": sum(
                    (value[0] for value in bin_values), start=Decimal(0)
                )
                / Decimal(count),
                "observed_win_rate": Decimal(sum(value[1] for value in bin_values))
                / Decimal(count),
            }
        )
    _write_csv(path, fields, bin_rows)


def _write_equity_curve(path: Path, report: KellyBacktestReport) -> None:
    fields = (
        "sequence",
        "fixture_id",
        "decision_at",
        "settlement",
        "stake",
        "profit",
        "available_equity_before_settlement",
        "available_equity_after_settlement",
        "drawdown",
    )
    rows = (
        {
            "sequence": index,
            "fixture_id": bet.candidate.match_id,
            "decision_at": bet.candidate.decision_at.isoformat(),
            "settlement": bet.status.value,
            "stake": bet.stake,
            "profit": bet.profit,
            "available_equity_before_settlement": bet.available_equity_before,
            "available_equity_after_settlement": bet.available_equity_after,
            "drawdown": bet.drawdown,
        }
        for index, bet in enumerate(report.bets, start=1)
    )
    _write_csv(path, fields, rows)


def _write_exclusions(
    path: Path,
    *,
    result_matching: ResultMatching,
    market_exclusions: list[dict[str, str]],
) -> None:
    fields = ("fixture_id", "tour", "stage", "reason", "detail")
    rows = [
        {
            "fixture_id": value.fixture_id,
            "tour": value.tour,
            "stage": "result_matching",
            "reason": value.reason,
            "detail": "",
        }
        for value in result_matching.quarantines
    ] + market_exclusions
    _write_csv(path, fields, rows)


def _summary(
    *,
    fixtures: tuple[ResearchFixture, ...],
    analyses: list[MatchAnalysis],
    offers: list[CalibratedOffer],
    fits: Mapping[str, TourFit],
    kelly: KellyBacktestReport,
    source_checksums: Mapping[str, str],
    settings: TennisResearchSettings,
) -> dict[str, Any]:
    validation_test = [
        value
        for value in analyses
        if value.phase in {"validation", "test"}
        and value.calibrated_player_one_probability is not None
        and value.result_winner_id is not None
    ]
    raw = [value.raw_player_one_probability for value in validation_test]
    calibrated = [value.calibrated_player_one_probability for value in validation_test]
    outcomes = [
        value.result_winner_id == value.fixture.player_one_id
        for value in validation_test
    ]
    selected_offers = [value for value in offers if value.selected]
    return {
        "schema_version": 1,
        "profile": settings.profile,
        "overall_conclusion": "exploratory_only",
        "scope_note": (
            "One partial 2026 season is insufficient to establish production "
            "calibration. No quote freshness guarantee is enforced."
        ),
        "settlement_timing_assumption": (
            "Local result CSVs do not provide settlement timestamps; bankroll "
            "simulation uses scheduled start plus four hours."
        ),
        "fixture_count": len(fixtures),
        "eligible_calibration_matches": len(analyses),
        "validation_test_predictions": len(validation_test),
        "offer_count": len(offers),
        "selected_candidates": kelly.selected_candidates,
        "raw_brier_score": _brier(raw, outcomes),
        "calibrated_brier_score": _brier(calibrated, outcomes),
        "raw_log_loss": _log_loss(raw, outcomes),
        "calibrated_log_loss": _log_loss(calibrated, outcomes),
        "coverage": (
            str(Decimal(len(analyses)) / Decimal(len(fixtures))) if fixtures else "0"
        ),
        "roi": str(kelly.roi),
        "yield": str(kelly.yield_),
        "turnover": str(kelly.turnover),
        "profit": str(kelly.profit),
        "final_equity": str(kelly.final_equity),
        "maximum_drawdown": str(kelly.maximum_drawdown),
        "average_closing_value": _average_optional(
            value.closing_value for value in selected_offers
        ),
        "tour_metrics": {
            tour: _tour_summary(tour, analyses, offers, fits[tour])
            for tour in ("ATP", "WTA")
        },
        "bookmaker_breakdown": _candidate_breakdown(selected_offers, "bookmaker"),
        "surface_breakdown": _candidate_breakdown(selected_offers, "surface"),
        "phase_metrics": {
            phase: _phase_summary(analyses, phase) for phase in ("validation", "test")
        },
        "source_checksums": dict(source_checksums),
        "configuration": _configuration_payload(settings),
    }


def _tour_summary(
    tour: str,
    analyses: list[MatchAnalysis],
    offers: list[CalibratedOffer],
    fit: TourFit,
) -> dict[str, Any]:
    rows = [
        value
        for value in analyses
        if value.fixture.tour == tour
        and value.phase in {"validation", "test"}
        and value.calibrated_player_one_probability is not None
        and value.result_winner_id is not None
    ]
    outcomes = [value.result_winner_id == value.fixture.player_one_id for value in rows]
    selected = [
        value for value in offers if value.fixture.tour == tour and value.selected
    ]
    unit_profit, unit_roi = _unit_stake_performance(selected)
    return {
        "conclusion": fit.conclusion,
        "result_match_rate": str(fit.match_rate),
        "phase_match_counts": {key: len(value) for key, value in fit.phases.items()},
        "validation_test_predictions": len(rows),
        "raw_brier_score": _brier(
            [value.raw_player_one_probability for value in rows], outcomes
        ),
        "calibrated_brier_score": _brier(
            [value.calibrated_player_one_probability for value in rows], outcomes
        ),
        "raw_log_loss": _log_loss(
            [value.raw_player_one_probability for value in rows], outcomes
        ),
        "calibrated_log_loss": _log_loss(
            [value.calibrated_player_one_probability for value in rows], outcomes
        ),
        "selected_candidates": len(selected),
        "unit_stake_profit": unit_profit,
        "unit_stake_roi": unit_roi,
        "average_closing_value": _average_optional(
            value.closing_value for value in selected
        ),
    }


def _candidate_breakdown(
    offers: list[CalibratedOffer],
    dimension: str,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[CalibratedOffer]] = defaultdict(list)
    for value in offers:
        if dimension == "bookmaker":
            key = value.raw.bookmaker_id
        else:
            key = value.fixture.surface
        grouped[key].append(value)
    result: dict[str, dict[str, Any]] = {}
    for key, values in sorted(grouped.items()):
        safe_values = [
            value.safe_expected_value
            for value in values
            if value.safe_expected_value is not None
        ]
        unit_profit, unit_roi = _unit_stake_performance(values)
        result[key] = {
            "selected_candidates": len(values),
            "unit_stake_profit": unit_profit,
            "unit_stake_roi": unit_roi,
            "average_safe_ev": (
                str(sum(safe_values, start=Decimal(0)) / Decimal(len(safe_values)))
                if safe_values
                else None
            ),
            "average_closing_value": _average_optional(
                value.closing_value for value in values
            ),
        }
    return result


def _phase_summary(analyses: list[MatchAnalysis], phase: str) -> dict[str, Any]:
    rows = [
        value
        for value in analyses
        if value.phase == phase
        and value.calibrated_player_one_probability is not None
        and value.result_winner_id is not None
    ]
    outcomes = [value.result_winner_id == value.fixture.player_one_id for value in rows]
    return {
        "predictions": len(rows),
        "raw_brier_score": _brier(
            [value.raw_player_one_probability for value in rows], outcomes
        ),
        "calibrated_brier_score": _brier(
            [value.calibrated_player_one_probability for value in rows], outcomes
        ),
        "raw_log_loss": _log_loss(
            [value.raw_player_one_probability for value in rows], outcomes
        ),
        "calibrated_log_loss": _log_loss(
            [value.calibrated_player_one_probability for value in rows], outcomes
        ),
    }


def _unit_stake_performance(
    offers: list[CalibratedOffer],
) -> tuple[str, str]:
    profit = sum(
        (
            value.raw.offered_odds - Decimal(1)
            if value.winner_player_id == value.raw.player_id
            else -Decimal(1)
            for value in offers
        ),
        start=Decimal(0),
    )
    roi = profit / Decimal(len(offers)) if offers else Decimal(0)
    return str(profit), str(roi)


def _write_summary_csv(path: Path, summary: Mapping[str, Any]) -> None:
    fields = ("metric", "value")
    scalar = (
        "overall_conclusion",
        "fixture_count",
        "eligible_calibration_matches",
        "validation_test_predictions",
        "offer_count",
        "selected_candidates",
        "raw_brier_score",
        "calibrated_brier_score",
        "raw_log_loss",
        "calibrated_log_loss",
        "coverage",
        "roi",
        "turnover",
        "profit",
        "final_equity",
        "maximum_drawdown",
        "average_closing_value",
    )
    _write_csv(
        path,
        fields,
        ({"metric": key, "value": summary[key]} for key in scalar),
    )


def _pinnacle_probability(
    snapshots: tuple[OddsSnapshot, ...],
    player_id: int,
    *,
    calculated_at: datetime,
) -> Decimal:
    snapshot = next(
        (value for value in snapshots if value.bookmaker_id == "pinnacle"),
        None,
    )
    if snapshot is None:
        raise ValueError("Pinnacle is required for calibration")
    return power_devig(snapshot, calculated_at=calculated_at).probability_for(player_id)


def _pinnacle_probability_or_none(
    snapshots: tuple[OddsSnapshot, ...],
    player_id: int,
    *,
    calculated_at: datetime,
) -> Decimal | None:
    try:
        return _pinnacle_probability(
            snapshots,
            player_id,
            calculated_at=calculated_at,
        )
    except ValueError:
        return None


def _model_for_phase(fit: TourFit, phase: str) -> BootstrapCalibration | None:
    if phase == "validation":
        return fit.validation_model
    if phase == "test":
        return fit.test_model
    return None


def _side_probability(
    home_probability: Decimal | None,
    *,
    home_side: bool,
) -> Decimal | None:
    if home_probability is None:
        return None
    return home_probability if home_side else Decimal(1) - home_probability


def _match(fixture: ResearchFixture) -> Match:
    return Match(
        match_id=fixture.fixture_id,
        tournament_id=f"oddspapi:{fixture.tournament_key}",
        player_ids=(fixture.player_one_id, fixture.player_two_id),
        scheduled_start=fixture.scheduled_start,
    )


def _configuration_payload(settings: TennisResearchSettings) -> dict[str, Any]:
    return {
        "entry_minutes_before_start": settings.entry_minutes_before_start,
        "closing_minutes_before_start": settings.closing_minutes_before_start,
        "bookmakers": settings.bookmakers,
        "minimum_bookmakers": settings.minimum_bookmakers,
        "training_fraction": str(settings.training_fraction),
        "validation_fraction": str(settings.validation_fraction),
        "minimum_training_matches": settings.minimum_training_matches,
        "bootstrap_samples": settings.bootstrap_samples,
        "block_days": settings.block_days,
        "lower_quantile": str(settings.lower_quantile),
        "regularization": str(settings.regularization),
        "random_seed": settings.random_seed,
        "minimum_expected_value": str(settings.ev_threshold),
        "quote_freshness_enforced": False,
    }


def _model_payload(model: BootstrapCalibration | None) -> dict[str, Any] | None:
    if model is None:
        return None
    return {
        "intercept": model.point_model.intercept,
        "slope": model.point_model.slope,
        "training_observations": model.training_observations,
        "trained_through": model.trained_through.isoformat(),
        "lower_quantile": str(model.lower_quantile),
        "bootstrap_models": [
            {"intercept": value.intercept, "slope": value.slope}
            for value in model.bootstrap_models
        ],
    }


def _observation_hash(observations: tuple[CalibrationObservation, ...]) -> str:
    values = [
        {
            "fixture_id": value.game_id,
            "scheduled_start": value.scheduled_start.isoformat(),
            "raw_probability": str(value.home_probability),
            "outcome": value.home_won,
        }
        for value in observations
    ]
    return sha256(
        json.dumps(values, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _source_checksums(
    cache_directory: Path,
    *,
    result_matching: ResultMatching,
) -> dict[str, str]:
    paths = sorted(path for path in cache_directory.rglob("*") if path.is_file())
    digest = sha256()
    for path in paths:
        relative = str(path.relative_to(cache_directory))
        digest.update(relative.encode())
        digest.update(sha256(path.read_bytes()).digest())
    return {
        "compressed_provider_cache_tree": digest.hexdigest(),
        "atp_results_csv": result_matching.source_checksums["ATP"],
        "wta_results_csv": result_matching.source_checksums["WTA"],
    }


def _brier(
    probabilities: Sequence[Decimal | None],
    outcomes: Sequence[bool],
) -> float | None:
    values = [value for value in probabilities if value is not None]
    if not values or len(values) != len(outcomes):
        return None
    return sum(
        (float(value) - float(outcome)) ** 2
        for value, outcome in zip(values, outcomes, strict=True)
    ) / len(values)


def _log_loss(
    probabilities: Sequence[Decimal | None],
    outcomes: Sequence[bool],
) -> float | None:
    values = [value for value in probabilities if value is not None]
    if not values or len(values) != len(outcomes):
        return None
    total = 0.0
    for value, outcome in zip(values, outcomes, strict=True):
        probability = min(1 - 1e-12, max(1e-12, float(value)))
        total -= math.log(probability if outcome else 1 - probability)
    return total / len(values)


def _average_optional(values: Iterable[Decimal | None]) -> str | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return str(sum(present, start=Decimal(0)) / Decimal(len(present)))


def _report_paths(output_directory: Path) -> dict[str, Path]:
    return {
        "matches": output_directory / "matches_predictions.csv",
        "offers": output_directory / "every_offer.csv",
        "candidates": output_directory / "selected_candidates.csv",
        "bins": output_directory / "calibration_bins.csv",
        "equity": output_directory / "equity_curve.csv",
        "exclusions": output_directory / "exclusions.csv",
        "summary_csv": output_directory / "summary.csv",
        "summary_json": output_directory / "summary.json",
    }


def _validate_model_settings(
    research: TennisResearchSettings,
    model: AppSettings,
) -> None:
    if model.collection.minimum_bookmakers != research.minimum_bookmakers:
        raise ValueError("research and tennis model minimum bookmakers differ")
    if model.signals.minimum_expected_value != research.ev_threshold:
        raise ValueError("research and tennis model EV thresholds differ")
    if model.pricing.consensus_method != "pinnacle":
        raise ValueError("tennis calibration requires Pinnacle consensus")


def _write_csv(
    path: Path,
    fields: tuple[str, ...],
    rows: Iterable[Mapping[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _optional(value: object | None) -> object:
    return "" if value is None else value


def _required(value: Decimal | None) -> Decimal:
    if value is None:
        raise AssertionError("required calibrated value is missing")
    return value
