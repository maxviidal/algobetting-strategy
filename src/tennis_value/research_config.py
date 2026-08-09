"""Strict configuration for the 2026 ATP/WTA 1000 calibration study."""

import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tennis_value.config import ConfigurationError


@dataclass(frozen=True, slots=True)
class TournamentSpec:
    key: str
    tour: str
    surface: str
    category_slug: str
    name_tokens: tuple[str, ...]
    from_time: datetime
    to_time: datetime


@dataclass(frozen=True, slots=True)
class TennisResearchSettings:
    profile: str
    sport_id: int
    entry_minutes_before_start: int
    closing_minutes_before_start: int
    bookmakers: tuple[str, ...]
    minimum_bookmakers: int
    quota_reserve: int
    historical_cooldown_seconds: float
    fixture_cooldown_seconds: float
    training_fraction: Decimal
    validation_fraction: Decimal
    minimum_training_matches: int
    bootstrap_samples: int
    block_days: int
    lower_quantile: Decimal
    regularization: Decimal
    random_seed: int
    ev_threshold: Decimal
    kelly_fraction: Decimal
    starting_equity: Decimal
    tournaments: tuple[TournamentSpec, ...]


def load_tennis_research_settings(path: Path) -> TennisResearchSettings:
    """Load and strictly validate one reproducible tennis research profile."""

    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle, parse_float=Decimal)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(f"Could not load {path}: {error}") from error
    _keys(raw, {"dataset", "calibration", "signals", "staking", "tournaments"}, "root")
    dataset = _table(raw, "dataset")
    calibration = _table(raw, "calibration")
    signals = _table(raw, "signals")
    staking = _table(raw, "staking")
    tournament_rows = raw.get("tournaments")
    if not isinstance(tournament_rows, list) or not tournament_rows:
        raise ConfigurationError("tournaments must be a non-empty array of tables")
    _keys(
        dataset,
        {
            "profile",
            "sport_id",
            "entry_minutes_before_start",
            "closing_minutes_before_start",
            "bookmakers",
            "minimum_bookmakers",
            "quota_reserve",
            "historical_cooldown_seconds",
            "fixture_cooldown_seconds",
        },
        "dataset",
    )
    _keys(
        calibration,
        {
            "training_fraction",
            "validation_fraction",
            "minimum_training_matches",
            "bootstrap_samples",
            "block_days",
            "lower_quantile",
            "regularization",
            "random_seed",
        },
        "calibration",
    )
    _keys(signals, {"minimum_expected_value"}, "signals")
    _keys(staking, {"kelly_fraction", "starting_equity"}, "staking")
    tournaments = tuple(
        _tournament(row, index) for index, row in enumerate(tournament_rows)
    )
    bookmakers = _strings(dataset, "bookmakers")
    settings = TennisResearchSettings(
        profile=_string(dataset, "profile"),
        sport_id=_int(dataset, "sport_id"),
        entry_minutes_before_start=_int(dataset, "entry_minutes_before_start"),
        closing_minutes_before_start=_int(dataset, "closing_minutes_before_start"),
        bookmakers=bookmakers,
        minimum_bookmakers=_int(dataset, "minimum_bookmakers"),
        quota_reserve=_int(dataset, "quota_reserve"),
        historical_cooldown_seconds=float(
            _decimal(dataset, "historical_cooldown_seconds")
        ),
        fixture_cooldown_seconds=float(_decimal(dataset, "fixture_cooldown_seconds")),
        training_fraction=_decimal(calibration, "training_fraction"),
        validation_fraction=_decimal(calibration, "validation_fraction"),
        minimum_training_matches=_int(calibration, "minimum_training_matches"),
        bootstrap_samples=_int(calibration, "bootstrap_samples"),
        block_days=_int(calibration, "block_days"),
        lower_quantile=_decimal(calibration, "lower_quantile"),
        regularization=_decimal(calibration, "regularization"),
        random_seed=_int(calibration, "random_seed"),
        ev_threshold=_decimal(signals, "minimum_expected_value"),
        kelly_fraction=_decimal(staking, "kelly_fraction"),
        starting_equity=_decimal(staking, "starting_equity"),
        tournaments=tournaments,
    )
    _validate(settings)
    return settings


def _tournament(value: object, index: int) -> TournamentSpec:
    context = f"tournaments[{index}]"
    if not isinstance(value, dict):
        raise ConfigurationError(f"{context} must be a table")
    _keys(
        value,
        {"key", "tour", "surface", "category_slug", "name_tokens", "from", "to"},
        context,
    )
    tour = _string(value, "tour").upper()
    if tour not in {"ATP", "WTA"}:
        raise ConfigurationError(f"{context}.tour must be ATP or WTA")
    surface = _string(value, "surface").lower()
    if surface not in {"hard", "clay", "grass"}:
        raise ConfigurationError(f"{context}.surface must be hard, clay or grass")
    from_time = _datetime(value, "from")
    to_time = _datetime(value, "to")
    if from_time >= to_time:
        raise ConfigurationError(f"{context}.from must be before to")
    return TournamentSpec(
        key=_string(value, "key"),
        tour=tour,
        surface=surface,
        category_slug=_string(value, "category_slug"),
        name_tokens=_strings(value, "name_tokens"),
        from_time=from_time,
        to_time=to_time,
    )


def _validate(settings: TennisResearchSettings) -> None:
    if settings.profile != "tennis_1000_2026":
        raise ConfigurationError("dataset.profile must be tennis_1000_2026")
    if settings.sport_id <= 0:
        raise ConfigurationError("sport_id must be positive")
    if settings.entry_minutes_before_start != 60:
        raise ConfigurationError("entry_minutes_before_start is locked at 60")
    if settings.closing_minutes_before_start != 5:
        raise ConfigurationError("closing_minutes_before_start is locked at 5")
    if settings.bookmakers != (
        "pinnacle",
        "bet365",
        "betano",
        "bwin",
        "unibet",
        "betway",
        "coral",
        "ladbrokes",
        "leovegas",
        "williamhill",
        "paddypower",
        "betfred",
        "888sport",
        "betsson",
        "skybet",
    ):
        raise ConfigurationError("bookmakers must match the locked 15-book profile")
    if len(settings.bookmakers) < settings.minimum_bookmakers:
        raise ConfigurationError(
            "bookmaker whitelist is smaller than minimum_bookmakers"
        )
    if len(set(settings.bookmakers)) != len(settings.bookmakers):
        raise ConfigurationError("bookmakers must be unique")
    if "pinnacle" not in settings.bookmakers:
        raise ConfigurationError("bookmakers must include pinnacle")
    if settings.minimum_bookmakers < 5:
        raise ConfigurationError("minimum_bookmakers must be at least five")
    if settings.quota_reserve < 1:
        raise ConfigurationError("quota_reserve must be positive")
    if settings.historical_cooldown_seconds < 5.0:
        raise ConfigurationError("historical cooldown must be at least 5 seconds")
    if settings.fixture_cooldown_seconds < 2.0:
        raise ConfigurationError("fixture cooldown must be at least 2 seconds")
    if not Decimal(0) < settings.training_fraction < Decimal(1):
        raise ConfigurationError("training_fraction must be between zero and one")
    if not Decimal(0) < settings.validation_fraction < Decimal(1):
        raise ConfigurationError("validation_fraction must be between zero and one")
    if settings.training_fraction + settings.validation_fraction >= Decimal(1):
        raise ConfigurationError("calibration fractions must leave a test phase")
    if settings.training_fraction != Decimal("0.60"):
        raise ConfigurationError("training_fraction is locked at 0.60")
    if settings.validation_fraction != Decimal("0.20"):
        raise ConfigurationError("validation_fraction is locked at 0.20")
    if settings.minimum_training_matches <= 0 or settings.bootstrap_samples <= 0:
        raise ConfigurationError("training and bootstrap counts must be positive")
    if settings.block_days <= 0:
        raise ConfigurationError("block_days must be positive")
    if settings.minimum_training_matches != 200:
        raise ConfigurationError("minimum_training_matches is locked at 200")
    if settings.bootstrap_samples != 200:
        raise ConfigurationError("bootstrap_samples is locked at 200")
    if settings.block_days != 7:
        raise ConfigurationError("block_days is locked at 7")
    if not Decimal(0) < settings.lower_quantile < Decimal("0.5"):
        raise ConfigurationError("lower_quantile must be between zero and 0.5")
    if settings.regularization <= 0:
        raise ConfigurationError("regularization must be positive")
    if settings.lower_quantile != Decimal("0.05"):
        raise ConfigurationError("lower_quantile is locked at 0.05")
    if settings.regularization != Decimal("10"):
        raise ConfigurationError("regularization is locked at 10")
    if settings.ev_threshold != Decimal("0.04"):
        raise ConfigurationError("minimum_expected_value is locked at 0.04")
    if not Decimal(0) < settings.kelly_fraction <= Decimal(1):
        raise ConfigurationError("kelly_fraction must be in (0, 1]")
    if settings.starting_equity <= 0:
        raise ConfigurationError("starting_equity must be positive")
    keys = [value.key for value in settings.tournaments]
    if len(set(keys)) != len(keys):
        raise ConfigurationError("tournament keys must be unique")


def _keys(value: dict[str, Any], expected: set[str], context: str) -> None:
    unknown = sorted(set(value) - expected)
    if unknown:
        raise ConfigurationError(
            f"{context} contains unknown keys: {', '.join(unknown)}"
        )


def _table(value: dict[str, Any], key: str) -> dict[str, Any]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise ConfigurationError(f"{key} must be a table")
    return result


def _string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ConfigurationError(f"{key} must be a non-empty string")
    return result.strip()


def _strings(value: dict[str, Any], key: str) -> tuple[str, ...]:
    result = value.get(key)
    if not isinstance(result, list) or not result:
        raise ConfigurationError(f"{key} must be a non-empty string array")
    cleaned = tuple(
        item.strip() for item in result if isinstance(item, str) and item.strip()
    )
    if len(cleaned) != len(result):
        raise ConfigurationError(f"{key} must contain only non-empty strings")
    return cleaned


def _int(value: dict[str, Any], key: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int):
        raise ConfigurationError(f"{key} must be an integer")
    return result


def _decimal(value: dict[str, Any], key: str) -> Decimal:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, Decimal | int):
        raise ConfigurationError(f"{key} must be numeric")
    return Decimal(result)


def _datetime(value: dict[str, Any], key: str) -> datetime:
    result = value.get(key)
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ConfigurationError(f"{key} must be a timezone-aware datetime")
    return result.astimezone(UTC)
