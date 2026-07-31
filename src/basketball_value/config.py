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
    kelly_fraction: Decimal
    kelly_starting_equity: Decimal
    minimum_result_match_rate: Decimal
    minimum_entry_coverage_rate: Decimal
    minimum_holdout_candidates: int

    @property
    def all_seasons(self) -> tuple[str, ...]:
        return (
            self.development_seasons
            + self.validation_seasons
            + self.holdout_seasons
        )


def load_basketball_settings(path: Path) -> BasketballSettings:
    """Load settings and reject changes to strategy-defining invariants."""

    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise BasketballConfigurationError(str(error)) from error
    competition = _table(raw, "competition")
    market = _table(raw, "market")
    selection = _table(raw, "selection")
    staking = _table(raw, "staking")
    acceptance = _table(raw, "acceptance")
    _locked(competition, "league", "NBA")
    _locked(competition, "regular_season_only", True)
    _locked(market, "odds_format", "decimal")
    _locked(market, "include_overtime", True)
    _locked(market, "exclude_exchanges", True)
    _locked(selection, "margin_method", "proportional")
    _locked(selection, "consensus_method", "median")
    _locked(selection, "leave_one_out", True)
    _locked(selection, "maximum_candidates_per_game", 1)
    regions = _strings(market, "regions")
    if regions != ("eu",):
        raise BasketballConfigurationError("market.regions must be exactly ['eu']")
    settings = BasketballSettings(
        sport_key=_string(competition, "sport_key"),
        season_start_years=_integers(competition, "season_start_years"),
        development_seasons=_strings(competition, "development_seasons"),
        validation_seasons=_strings(competition, "validation_seasons"),
        holdout_seasons=_strings(competition, "holdout_seasons"),
        region=regions[0],
        market_key=_string(market, "market_key"),
        entry_minutes_before_tip=_positive_int(
            selection, "entry_minutes_before_tip"
        ),
        closing_minutes_before_tip=_positive_int(
            selection, "closing_minutes_before_tip"
        ),
        maximum_quote_age_minutes=_positive_int(
            selection, "maximum_quote_age_minutes"
        ),
        minimum_bookmakers=_positive_int(selection, "minimum_bookmakers"),
        primary_ev_threshold=_decimal(selection, "primary_ev_threshold"),
        development_thresholds=tuple(
            Decimal(str(value))
            for value in _list(selection, "development_thresholds")
        ),
        kelly_fraction=_decimal(staking, "kelly_fraction"),
        kelly_starting_equity=_decimal(staking, "kelly_starting_equity"),
        minimum_result_match_rate=_decimal(
            acceptance, "minimum_result_match_rate"
        ),
        minimum_entry_coverage_rate=_decimal(
            acceptance, "minimum_entry_coverage_rate"
        ),
        minimum_holdout_candidates=_positive_int(
            acceptance, "minimum_holdout_candidates"
        ),
    )
    if settings.entry_minutes_before_tip <= settings.closing_minutes_before_tip:
        raise BasketballConfigurationError(
            "entry must occur earlier than the closing benchmark"
        )
    locked_values = {
        "sport_key": (settings.sport_key, "basketball_nba"),
        "season_start_years": (
            settings.season_start_years,
            (2021, 2022, 2023, 2024, 2025),
        ),
        "development_seasons": (
            settings.development_seasons,
            ("2021-22", "2022-23", "2023-24"),
        ),
        "validation_seasons": (settings.validation_seasons, ("2024-25",)),
        "holdout_seasons": (settings.holdout_seasons, ("2025-26",)),
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
    for name, (actual, expected) in locked_values.items():
        if actual != expected:
            raise BasketballConfigurationError(
                f"{name} is locked at {expected!r}"
            )
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


def _decimal(table: dict[str, Any], key: str) -> Decimal:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BasketballConfigurationError(f"{key} must be numeric")
    return Decimal(str(value))
