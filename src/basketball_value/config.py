"""Strict, typed settings for the locked NBA research design."""

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


class BasketballConfigurationError(ValueError):
    """Raised when the locked research configuration is invalid."""


@dataclass(frozen=True, slots=True)
class BasketballSettings:
    profile: str
    sport_key: str
    season_start_years: tuple[int, ...]
    development_seasons: tuple[str, ...]
    validation_seasons: tuple[str, ...]
    holdout_seasons: tuple[str, ...]
    region: str
    market_key: str
    entry_minutes_before_tip: int
    closing_minutes_before_tip: int
    maximum_quote_age_minutes: int
    minimum_bookmakers: int
    primary_ev_threshold: Decimal
    development_thresholds: tuple[Decimal, ...]
    calibration_enabled: bool
    calibration_method: str
    calibration_training_fraction: Decimal
    calibration_validation_fraction: Decimal
    calibration_minimum_training_games: int
    calibration_bootstrap_samples: int
    calibration_block_days: int
    calibration_lower_quantile: Decimal
    calibration_regularization: Decimal
    calibration_random_seed: int
    kelly_fraction: Decimal
    kelly_starting_equity: Decimal
    minimum_result_match_rate: Decimal
    minimum_entry_coverage_rate: Decimal
    minimum_holdout_candidates: int

    @property
    def all_seasons(self) -> tuple[str, ...]:
        return self.development_seasons + self.validation_seasons + self.holdout_seasons


def load_basketball_settings(path: Path) -> BasketballSettings:
    """Load settings and reject changes to strategy-defining invariants."""

    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise BasketballConfigurationError(str(error)) from error
    competition = _table(raw, "competition")
    market = _table(raw, "market")
    selection = _table(raw, "selection")
    calibration = _table(raw, "calibration")
    staking = _table(raw, "staking")
    acceptance = _table(raw, "acceptance")
    _locked(competition, "league", "NBA")
    _locked(competition, "regular_season_only", True)
    _locked(market, "odds_format", "decimal")
    _locked(market, "include_overtime", True)
    _locked(market, "exclude_exchanges", True)
    _locked(selection, "margin_method", "power")
    _locked(selection, "consensus_method", "median")
    _locked(selection, "leave_one_out", True)
    _locked(selection, "maximum_candidates_per_game", 1)
    regions = _strings(market, "regions")
    if regions != ("eu",):
        raise BasketballConfigurationError("market.regions must be exactly ['eu']")
    settings = BasketballSettings(
        profile=_string(competition, "profile"),
        sport_key=_string(competition, "sport_key"),
        season_start_years=_integers(competition, "season_start_years"),
        development_seasons=_strings(competition, "development_seasons"),
        validation_seasons=_strings(competition, "validation_seasons"),
        holdout_seasons=_strings(competition, "holdout_seasons"),
        region=regions[0],
        market_key=_string(market, "market_key"),
        entry_minutes_before_tip=_positive_int(selection, "entry_minutes_before_tip"),
        closing_minutes_before_tip=_positive_int(
            selection, "closing_minutes_before_tip"
        ),
        maximum_quote_age_minutes=_positive_int(selection, "maximum_quote_age_minutes"),
        minimum_bookmakers=_positive_int(selection, "minimum_bookmakers"),
        primary_ev_threshold=_decimal(selection, "primary_ev_threshold"),
        development_thresholds=tuple(
            Decimal(str(value)) for value in _list(selection, "development_thresholds")
        ),
        calibration_enabled=_boolean(calibration, "enabled"),
        calibration_method=_string(calibration, "method"),
        calibration_training_fraction=_decimal(calibration, "training_fraction"),
        calibration_validation_fraction=_decimal(calibration, "validation_fraction"),
        calibration_minimum_training_games=_positive_int(
            calibration, "minimum_training_games"
        ),
        calibration_bootstrap_samples=_positive_int(calibration, "bootstrap_samples"),
        calibration_block_days=_positive_int(calibration, "block_days"),
        calibration_lower_quantile=_decimal(calibration, "lower_quantile"),
        calibration_regularization=_decimal(calibration, "regularization"),
        calibration_random_seed=_integer(calibration, "random_seed"),
        kelly_fraction=_decimal(staking, "kelly_fraction"),
        kelly_starting_equity=_decimal(staking, "kelly_starting_equity"),
        minimum_result_match_rate=_decimal(acceptance, "minimum_result_match_rate"),
        minimum_entry_coverage_rate=_decimal(acceptance, "minimum_entry_coverage_rate"),
        minimum_holdout_candidates=_positive_int(
            acceptance, "minimum_holdout_candidates"
        ),
    )
    if settings.entry_minutes_before_tip <= settings.closing_minutes_before_tip:
        raise BasketballConfigurationError(
            "entry must occur earlier than the closing benchmark"
        )
    if not Decimal(0) < settings.calibration_training_fraction < Decimal(1):
        raise BasketballConfigurationError(
            "calibration.training_fraction must be between 0 and 1"
        )
    if not Decimal(0) < settings.calibration_validation_fraction < Decimal(1):
        raise BasketballConfigurationError(
            "calibration.validation_fraction must be between 0 and 1"
        )
    if (
        settings.calibration_training_fraction
        + settings.calibration_validation_fraction
        >= Decimal(1)
    ):
        raise BasketballConfigurationError(
            "calibration fractions must leave a positive test fraction"
        )
    if not Decimal(0) < settings.calibration_lower_quantile < Decimal("0.5"):
        raise BasketballConfigurationError(
            "calibration.lower_quantile must be between 0 and 0.5"
        )
    if settings.calibration_regularization <= 0:
        raise BasketballConfigurationError(
            "calibration.regularization must be positive"
        )
    locked_values: dict[str, tuple[object, object]] = {
        "sport_key": (settings.sport_key, "basketball_nba"),
        "market_key": (settings.market_key, "h2h"),
        "entry_minutes_before_tip": (settings.entry_minutes_before_tip, 60),
        "closing_minutes_before_tip": (settings.closing_minutes_before_tip, 5),
        "maximum_quote_age_minutes": (
            settings.maximum_quote_age_minutes,
            30,
        ),
        "minimum_bookmakers": (settings.minimum_bookmakers, 5),
        "primary_ev_threshold": (
            settings.primary_ev_threshold,
            Decimal("0.05"),
        ),
        "development_thresholds": (
            settings.development_thresholds,
            (
                Decimal("0.02"),
                Decimal("0.03"),
                Decimal("0.075"),
                Decimal("0.10"),
            ),
        ),
        "calibration_method": (settings.calibration_method, "logistic"),
        "calibration_training_fraction": (
            settings.calibration_training_fraction,
            Decimal("0.60"),
        ),
        "calibration_validation_fraction": (
            settings.calibration_validation_fraction,
            Decimal("0.20"),
        ),
        "calibration_minimum_training_games": (
            settings.calibration_minimum_training_games,
            300,
        ),
        "calibration_bootstrap_samples": (
            settings.calibration_bootstrap_samples,
            200,
        ),
        "calibration_block_days": (settings.calibration_block_days, 7),
        "calibration_lower_quantile": (
            settings.calibration_lower_quantile,
            Decimal("0.05"),
        ),
        "calibration_regularization": (
            settings.calibration_regularization,
            Decimal("10.0"),
        ),
        "calibration_random_seed": (
            settings.calibration_random_seed,
            202324,
        ),
        "kelly_fraction": (settings.kelly_fraction, Decimal("0.25")),
        "kelly_starting_equity": (
            settings.kelly_starting_equity,
            Decimal("10000.0"),
        ),
        "minimum_result_match_rate": (
            settings.minimum_result_match_rate,
            Decimal("0.98"),
        ),
        "minimum_entry_coverage_rate": (
            settings.minimum_entry_coverage_rate,
            Decimal("0.80"),
        ),
        "minimum_holdout_candidates": (
            settings.minimum_holdout_candidates,
            300,
        ),
    }
    profile_seasons: dict[
        str,
        tuple[tuple[int, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]],
    ] = {
        "five_season_research": (
            (2021, 2022, 2023, 2024, 2025),
            ("2021-22", "2022-23", "2023-24"),
            ("2024-25",),
            ("2025-26",),
        ),
        "development_pilot_2023_24": (
            (2023,),
            ("2023-24",),
            (),
            (),
        ),
    }
    expected_seasons = profile_seasons.get(settings.profile)
    if expected_seasons is None:
        raise BasketballConfigurationError(
            f"unsupported competition profile: {settings.profile!r}"
        )
    expected_calibration_enabled = settings.profile == "development_pilot_2023_24"
    if settings.calibration_enabled is not expected_calibration_enabled:
        raise BasketballConfigurationError(
            "calibration.enabled is locked at "
            f"{expected_calibration_enabled!r} for {settings.profile!r}"
        )
    locked_values.update(
        {
            "season_start_years": (
                settings.season_start_years,
                expected_seasons[0],
            ),
            "development_seasons": (
                settings.development_seasons,
                expected_seasons[1],
            ),
            "validation_seasons": (
                settings.validation_seasons,
                expected_seasons[2],
            ),
            "holdout_seasons": (
                settings.holdout_seasons,
                expected_seasons[3],
            ),
        }
    )
    for name, (actual, expected) in locked_values.items():
        if actual != expected:
            raise BasketballConfigurationError(f"{name} is locked at {expected!r}")
    return settings


def _table(value: dict[str, Any], key: str) -> dict[str, Any]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise BasketballConfigurationError(f"{key} must be a table")
    return result


def _locked(table: dict[str, Any], key: str, expected: object) -> None:
    if table.get(key) != expected:
        raise BasketballConfigurationError(f"{key} must be {expected!r}")


def _list(table: dict[str, Any], key: str) -> list[object]:
    value = table.get(key)
    if not isinstance(value, list):
        raise BasketballConfigurationError(f"{key} must be an array")
    return value


def _strings(table: dict[str, Any], key: str) -> tuple[str, ...]:
    values = _list(table, key)
    if not all(isinstance(value, str) and value for value in values):
        raise BasketballConfigurationError(f"{key} must contain strings")
    return tuple(str(value) for value in values)


def _integers(table: dict[str, Any], key: str) -> tuple[int, ...]:
    values = _list(table, key)
    if not all(
        isinstance(value, int) and not isinstance(value, bool) for value in values
    ):
        raise BasketballConfigurationError(f"{key} must contain integers")
    return tuple(value for value in values if isinstance(value, int))


def _string(table: dict[str, Any], key: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise BasketballConfigurationError(f"{key} must be a non-empty string")
    return value


def _positive_int(table: dict[str, Any], key: str) -> int:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BasketballConfigurationError(f"{key} must be a positive integer")
    return value


def _integer(table: dict[str, Any], key: str) -> int:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BasketballConfigurationError(f"{key} must be an integer")
    return value


def _boolean(table: dict[str, Any], key: str) -> bool:
    value = table.get(key)
    if not isinstance(value, bool):
        raise BasketballConfigurationError(f"{key} must be a boolean")
    return value


def _decimal(table: dict[str, Any], key: str) -> Decimal:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BasketballConfigurationError(f"{key} must be numeric")
    return Decimal(str(value))
