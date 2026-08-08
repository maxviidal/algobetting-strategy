"""Chronological probability calibration for the NBA pilot."""

import math
import random
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

_PROBABILITY_EPSILON = 1e-6


@dataclass(frozen=True, slots=True)
class CalibrationObservation:
    """One independent home-win calibration row for one game."""

    game_id: str
    scheduled_start: datetime
    home_probability: Decimal
    home_won: bool


@dataclass(frozen=True, slots=True)
class CalibrationPrediction:
    """A point-in-time prediction used to evaluate calibration out of sample."""

    game_id: str
    phase: str
    raw_home_probability: Decimal
    calibrated_home_probability: Decimal
    home_won: bool


@dataclass(frozen=True, slots=True)
class LogisticCalibrator:
    """Regularized logistic calibration anchored to the identity mapping."""

    intercept: float
    slope: float

    def calibrate(self, probability: Decimal) -> Decimal:
        """Map an uncalibrated probability to a calibrated probability."""

        raw = _clip_probability(float(probability))
        log_odds = math.log(raw / (1.0 - raw))
        calibrated = _sigmoid(self.intercept + self.slope * log_odds)
        return Decimal(str(_clip_probability(calibrated)))


@dataclass(frozen=True, slots=True)
class BootstrapCalibration:
    """Point calibrator plus block-bootstrap models for a lower probability bound."""

    point_model: LogisticCalibrator
    bootstrap_models: tuple[LogisticCalibrator, ...]
    lower_quantile: Decimal
    training_observations: int
    trained_through: datetime

    def probabilities_for_side(
        self,
        home_probability: Decimal,
        *,
        home_side: bool,
    ) -> tuple[Decimal, Decimal]:
        """Return point-calibrated and conservative probabilities for one side."""

        calibrated_home = self.point_model.calibrate(home_probability)
        point = calibrated_home if home_side else Decimal(1) - calibrated_home
        bootstrap = [
            (
                model.calibrate(home_probability)
                if home_side
                else Decimal(1) - model.calibrate(home_probability)
            )
            for model in self.bootstrap_models
        ]
        lower = _quantile(bootstrap, self.lower_quantile)
        return point, min(point, lower)


def fit_bootstrap_calibration(
    observations: tuple[CalibrationObservation, ...],
    *,
    bootstrap_samples: int,
    block_days: int,
    lower_quantile: Decimal,
    regularization: Decimal,
    random_seed: int,
) -> BootstrapCalibration:
    """Fit logistic calibration and deterministic chronological block bootstrap."""

    if not observations:
        raise ValueError("calibration requires at least one observation")
    if bootstrap_samples <= 0:
        raise ValueError("bootstrap_samples must be positive")
    if block_days <= 0:
        raise ValueError("block_days must be positive")
    if not Decimal(0) < lower_quantile < Decimal(0.5):
        raise ValueError("lower_quantile must be between 0 and 0.5")
    if regularization <= 0:
        raise ValueError("regularization must be positive")

    ordered = tuple(sorted(observations, key=lambda value: value.scheduled_start))
    point_model = fit_logistic_calibrator(
        ordered,
        regularization=regularization,
    )
    blocks = _chronological_blocks(ordered, block_days=block_days)
    generator = random.Random(random_seed)
    models: list[LogisticCalibrator] = []
    for _ in range(bootstrap_samples):
        sample = tuple(
            observation
            for _ in range(len(blocks))
            for observation in blocks[generator.randrange(len(blocks))]
        )
        models.append(
            fit_logistic_calibrator(
                sample,
                regularization=regularization,
            )
        )
    return BootstrapCalibration(
        point_model=point_model,
        bootstrap_models=tuple(models),
        lower_quantile=lower_quantile,
        training_observations=len(ordered),
        trained_through=ordered[-1].scheduled_start,
    )


def fit_logistic_calibrator(
    observations: tuple[CalibrationObservation, ...],
    *,
    regularization: Decimal,
) -> LogisticCalibrator:
    """Fit Platt-style calibration using a stable two-parameter Newton solve."""

    if not observations:
        raise ValueError("calibration requires at least one observation")
    penalty = float(regularization)
    if not math.isfinite(penalty) or penalty <= 0:
        raise ValueError("regularization must be finite and positive")
    rows = tuple(
        (
            math.log(
                _clip_probability(float(value.home_probability))
                / (1.0 - _clip_probability(float(value.home_probability)))
            ),
            1.0 if value.home_won else 0.0,
        )
        for value in observations
    )
    intercept = 0.0
    slope = 1.0
    for _ in range(100):
        gradient_intercept = penalty * intercept
        gradient_slope = penalty * (slope - 1.0)
        hessian_aa = penalty
        hessian_ab = 0.0
        hessian_bb = penalty
        for log_odds, outcome in rows:
            probability = _sigmoid(intercept + slope * log_odds)
            residual = probability - outcome
            weight = max(probability * (1.0 - probability), 1e-12)
            gradient_intercept += residual
            gradient_slope += residual * log_odds
            hessian_aa += weight
            hessian_ab += weight * log_odds
            hessian_bb += weight * log_odds * log_odds
        determinant = hessian_aa * hessian_bb - hessian_ab * hessian_ab
        if determinant <= 1e-18:
            break
        intercept_step = (
            hessian_bb * gradient_intercept - hessian_ab * gradient_slope
        ) / determinant
        slope_step = (
            hessian_aa * gradient_slope - hessian_ab * gradient_intercept
        ) / determinant
        intercept -= max(-1.0, min(1.0, intercept_step))
        slope -= max(-1.0, min(1.0, slope_step))
        if max(abs(intercept_step), abs(slope_step)) < 1e-10:
            break
    return LogisticCalibrator(intercept=intercept, slope=slope)


def _chronological_blocks(
    observations: tuple[CalibrationObservation, ...],
    *,
    block_days: int,
) -> tuple[tuple[CalibrationObservation, ...], ...]:
    origin = observations[0].scheduled_start.date()
    grouped: dict[int, list[CalibrationObservation]] = {}
    for observation in observations:
        key = (observation.scheduled_start.date() - origin).days // block_days
        grouped.setdefault(key, []).append(observation)
    return tuple(tuple(grouped[key]) for key in sorted(grouped))


def _quantile(values: list[Decimal], quantile: Decimal) -> Decimal:
    if not values:
        raise ValueError("quantile requires at least one value")
    ordered = sorted(values)
    position = float(quantile) * (len(ordered) - 1)
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    weight = Decimal(str(position - lower_index))
    return ordered[lower_index] * (Decimal(1) - weight) + ordered[upper_index] * weight


def _clip_probability(value: float) -> float:
    return min(1.0 - _PROBABILITY_EPSILON, max(_PROBABILITY_EPSILON, value))


def _sigmoid(value: float) -> float:
    if value >= 0:
        exponential = math.exp(-value)
        return 1.0 / (1.0 + exponential)
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)
