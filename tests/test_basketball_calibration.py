from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from basketball_value.calibration import (
    CalibrationObservation,
    fit_bootstrap_calibration,
    fit_logistic_calibrator,
)
from basketball_value.config import load_basketball_settings
from basketball_value.domain import (
    Game,
    GameResult,
    MoneylinePrice,
    MoneylineSnapshot,
    stable_game_id,
)
from basketball_value.reporting import build_summary, run_backtest
from basketball_value.workflow import BasketballDataset


def test_logistic_calibration_shrinks_overconfident_probabilities() -> None:
    start = datetime(2023, 10, 1, tzinfo=UTC)
    observations = tuple(
        CalibrationObservation(
            game_id=str(index),
            scheduled_start=start + timedelta(days=index),
            home_probability=Decimal("0.2") if index < 100 else Decimal("0.8"),
            home_won=(index % 5 < 2) if index < 100 else (index % 5 < 3),
        )
        for index in range(200)
    )

    model = fit_logistic_calibrator(
        observations,
        regularization=Decimal("10"),
    )

    assert model.calibrate(Decimal("0.2")) > Decimal("0.2")
    assert model.calibrate(Decimal("0.8")) < Decimal("0.8")


def test_block_bootstrap_is_deterministic_and_returns_lower_side_bounds() -> None:
    start = datetime(2023, 10, 1, tzinfo=UTC)
    observations = tuple(
        CalibrationObservation(
            game_id=str(index),
            scheduled_start=start + timedelta(days=index),
            home_probability=Decimal("0.5"),
            home_won=index % 2 == 0,
        )
        for index in range(40)
    )
    first = fit_bootstrap_calibration(
        observations,
        bootstrap_samples=20,
        block_days=7,
        lower_quantile=Decimal("0.05"),
        regularization=Decimal("10"),
        random_seed=202324,
    )
    second = fit_bootstrap_calibration(
        observations,
        bootstrap_samples=20,
        block_days=7,
        lower_quantile=Decimal("0.05"),
        regularization=Decimal("10"),
        random_seed=202324,
    )
    home_point, home_safe = first.probabilities_for_side(Decimal("0.5"), home_side=True)
    away_point, away_safe = first.probabilities_for_side(
        Decimal("0.5"), home_side=False
    )

    assert first == second
    assert home_point + away_point == Decimal(1)
    assert home_safe <= home_point
    assert away_safe <= away_point


def test_pilot_uses_earlier_2023_24_games_and_keeps_final_20_percent_test() -> None:
    games = tuple(_game(index) for index in range(50))
    entries = tuple(snapshot for game in games for snapshot in _entry_snapshots(game))
    dataset = BasketballDataset(
        games=games,
        results={
            game.game_id: GameResult(
                game_id=game.game_id,
                home_score=110 if index % 2 == 0 else 100,
                away_score=100 if index % 2 == 0 else 110,
                final=True,
                postponed=False,
            )
            for index, game in enumerate(games)
        },
        entry_snapshots=entries,
        closing_snapshots=(),
        requested_timestamps=100,
        completed_timestamps=100,
        unmatched_events=0,
        matched_events=100,
        matched_games=50,
        quarantined_event_ids=(),
        market_exclusions=(),
        result_quarantines=(),
    )
    settings = replace(
        load_basketball_settings(Path("configs/basketball_pilot_2023_24.toml")),
        calibration_minimum_training_games=10,
        calibration_bootstrap_samples=20,
    )

    run = run_backtest(dataset, settings)
    summary = build_summary(dataset, run, settings)

    assert dict(run.calibration_metadata.phase_game_counts) == {
        "training": 30,
        "validation": 10,
        "test": 10,
    }
    assert run.calibration_metadata.validation_fit_observations == 30
    assert run.calibration_metadata.test_fit_observations == 40
    assert len(run.calibration_predictions) == 20
    assert {value.phase for value in run.calibration_predictions} == {
        "validation",
        "test",
    }
    assert run.candidates
    assert {candidate.offer.calibration_phase for candidate in run.candidates} == {
        "validation",
        "test",
    }
    assert all(
        candidate.offer.conservative_probability
        <= candidate.offer.calibrated_probability
        for candidate in run.candidates
        if candidate.offer.conservative_probability is not None
        and candidate.offer.calibrated_probability is not None
    )
    assert all(
        candidate.offer.expected_value
        == candidate.offer.offered_odds * candidate.offer.decision_probability
        - Decimal(1)
        for candidate in run.candidates
    )
    assert summary["probability_calibration"]["test"]["observations"] == 10
    assert summary["acceptance"]["conclusion"] == "exploratory_only"


def test_calibrated_pilot_rejects_games_from_other_seasons() -> None:
    game = replace(_game(0), season="2024-25")
    dataset = BasketballDataset(
        games=(game,),
        results={},
        entry_snapshots=(),
        closing_snapshots=(),
        requested_timestamps=0,
        completed_timestamps=0,
        unmatched_events=0,
        matched_events=0,
        matched_games=0,
        quarantined_event_ids=(),
        market_exclusions=(),
        result_quarantines=(),
    )
    settings = load_basketball_settings(Path("configs/basketball_pilot_2023_24.toml"))

    with pytest.raises(ValueError, match="only 2023-24"):
        run_backtest(dataset, settings)


def _game(index: int) -> Game:
    source_event_id = str(index)
    return Game(
        game_id=stable_game_id("balldontlie", source_event_id),
        source="balldontlie",
        source_event_id=source_event_id,
        season="2023-24",
        home_team_id="LAL",
        away_team_id="BOS",
        scheduled_start=datetime(2023, 10, 1, 3, tzinfo=UTC) + timedelta(days=index),
    )


def _entry_snapshots(game: Game) -> tuple[MoneylineSnapshot, ...]:
    return tuple(
        MoneylineSnapshot(
            snapshot_id=f"{game.game_id}-{bookmaker}",
            game_id=game.game_id,
            bookmaker_id=bookmaker,
            observed_at=game.scheduled_start - timedelta(hours=1),
            prices=(
                MoneylinePrice(
                    "LAL", Decimal("3.0") if bookmaker == "a" else Decimal("2.0")
                ),
                MoneylinePrice(
                    "BOS", Decimal("1.5") if bookmaker == "a" else Decimal("2.0")
                ),
            ),
            source="test",
            source_event_id=f"odds-{game.source_event_id}",
        )
        for bookmaker in ("a", "b", "c", "d", "e")
    )
