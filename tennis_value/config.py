"""Typed application configuration."""

import os
import re
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

type MarginMethod = Literal["proportional"]
type ConsensusMethod = Literal["median"]

_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigurationError(ValueError):
    """Raised when a configuration file is invalid or unsupported."""


@dataclass(frozen=True, slots=True)
class CollectionSettings:
    """Point-in-time quote eligibility settings."""

    minimum_bookmakers: int = 5
    maximum_quote_age_seconds: int = 300

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_bookmakers, bool)
            or self.minimum_bookmakers < 2
        ):
            raise ConfigurationError("minimum_bookmakers must be an integer >= 2")
        if (
            isinstance(self.maximum_quote_age_seconds, bool)
            or self.maximum_quote_age_seconds < 0
        ):
            raise ConfigurationError(
                "maximum_quote_age_seconds must be a non-negative integer"
            )


@dataclass(frozen=True, slots=True)
class PricingSettings:
    """De-vigging and consensus strategy settings supported by v1."""

    margin_method: MarginMethod = "proportional"
    consensus_method: ConsensusMethod = "median"
    leave_one_bookmaker_out: bool = True

    def __post_init__(self) -> None:
        if self.margin_method != "proportional":
            raise ConfigurationError(
                "margin_method must be 'proportional' in pricing model v1"
            )
        if self.consensus_method != "median":
            raise ConfigurationError(
                "consensus_method must be 'median' in pricing model v1"
            )
        if self.leave_one_bookmaker_out is not True:
            raise ConfigurationError(
                "leave_one_bookmaker_out must be true in pricing model v1"
            )


@dataclass(frozen=True, slots=True)
class SignalSettings:
    """Expected-value thresholds for research signals."""

    minimum_expected_value: Decimal = Decimal("0.05")

    def __post_init__(self) -> None:
        _require_probability_like(
            self.minimum_expected_value,
            "minimum_expected_value",
        )


@dataclass(frozen=True, slots=True)
class QualitySettings:
    """Non-suppressing diagnostics for unusual market observations."""

    review_expected_value: Decimal = Decimal("0.20")
    minimum_normal_overround: Decimal = Decimal("0.98")
    maximum_normal_overround: Decimal = Decimal("1.15")
    maximum_peer_probability_range: Decimal = Decimal("0.10")

    def __post_init__(self) -> None:
        _require_probability_like(self.review_expected_value, "review_expected_value")
        _require_positive_decimal(
            self.minimum_normal_overround,
            "minimum_normal_overround",
        )
        _require_positive_decimal(
            self.maximum_normal_overround,
            "maximum_normal_overround",
        )
        if self.minimum_normal_overround > self.maximum_normal_overround:
            raise ConfigurationError(
                "minimum_normal_overround must not exceed maximum_normal_overround"
            )
        _require_probability_like(
            self.maximum_peer_probability_range,
            "maximum_peer_probability_range",
        )


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Complete settings for point-in-time market evaluation."""

    collection: CollectionSettings = field(default_factory=CollectionSettings)
    pricing: PricingSettings = field(default_factory=PricingSettings)
    signals: SignalSettings = field(default_factory=SignalSettings)
    quality: QualitySettings = field(default_factory=QualitySettings)


def load_settings(path: Path) -> AppSettings:
    """Load and strictly validate a TOML settings file."""

    try:
        with path.open("rb") as config_file:
            raw = tomllib.load(config_file, parse_float=Decimal)
    except (OSError, tomllib.TOMLDecodeError) as error:
        message = f"Could not load configuration from {path}: {error}"
        raise ConfigurationError(message) from error

    _reject_unknown_keys(raw, {"collection", "pricing", "signals", "quality"}, "root")
    collection = _required_table(raw, "collection")
    pricing = _required_table(raw, "pricing")
    signals = _required_table(raw, "signals")
    quality = _optional_table(raw, "quality")

    _reject_unknown_keys(
        collection,
        {"minimum_bookmakers", "maximum_quote_age_seconds"},
        "collection",
    )
    _reject_unknown_keys(
        pricing,
        {"margin_method", "consensus_method", "leave_one_bookmaker_out"},
        "pricing",
    )
    _reject_unknown_keys(signals, {"minimum_expected_value"}, "signals")
    _reject_unknown_keys(
        quality,
        {
            "review_expected_value",
            "minimum_normal_overround",
            "maximum_normal_overround",
            "maximum_peer_probability_range",
        },
        "quality",
    )

    try:
        return AppSettings(
            collection=CollectionSettings(
                minimum_bookmakers=_required_int(
                    collection,
                    "minimum_bookmakers",
                    "collection",
                ),
                maximum_quote_age_seconds=_required_int(
                    collection,
                    "maximum_quote_age_seconds",
                    "collection",
                ),
            ),
            pricing=PricingSettings(
                margin_method=cast(
                    MarginMethod,
                    _required_string(pricing, "margin_method", "pricing"),
                ),
                consensus_method=cast(
                    ConsensusMethod,
                    _required_string(pricing, "consensus_method", "pricing"),
                ),
                leave_one_bookmaker_out=_required_bool(
                    pricing,
                    "leave_one_bookmaker_out",
                    "pricing",
                ),
            ),
            signals=SignalSettings(
                minimum_expected_value=_required_decimal(
                    signals,
                    "minimum_expected_value",
                    "signals",
                )
            ),
            quality=QualitySettings(
                review_expected_value=_optional_decimal(
                    quality,
                    "review_expected_value",
                    Decimal("0.20"),
                    "quality",
                ),
                minimum_normal_overround=_optional_decimal(
                    quality,
                    "minimum_normal_overround",
                    Decimal("0.98"),
                    "quality",
                ),
                maximum_normal_overround=_optional_decimal(
                    quality,
                    "maximum_normal_overround",
                    Decimal("1.15"),
                    "quality",
                ),
                maximum_peer_probability_range=_optional_decimal(
                    quality,
                    "maximum_peer_probability_range",
                    Decimal("0.10"),
                    "quality",
                ),
            ),
        )
    except (KeyError, TypeError, ConfigurationError) as error:
        if isinstance(error, ConfigurationError):
            raise
        raise ConfigurationError(f"Invalid configuration in {path}: {error}") from error


def get_odds_api_key() -> str:
    """Return the Odds API key configured in the environment."""
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        message = (
            "ODDS_API_KEY environment variable is not set. "
            "Add it to your environment variables."
        )
        raise RuntimeError(message)
    return api_key


def load_env_file(path: Path, *, override: bool = False) -> bool:
    """Safely load simple KEY=VALUE entries without executing shell code."""

    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        message = f"Could not read environment file {path}: {error}"
        raise ConfigurationError(message) from error

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            raise ConfigurationError(
                f"{path}:{line_number} must use NAME=VALUE syntax"
            )
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if _ENVIRONMENT_NAME.fullmatch(name) is None:
            raise ConfigurationError(
                f"{path}:{line_number} has an invalid environment variable name"
            )
        value = _parse_env_value(raw_value.strip(), path, line_number)
        if override or name not in os.environ:
            os.environ[name] = value
    return True


def _required_table(
    raw: dict[str, object],
    key: str,
) -> dict[str, object]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigurationError(f"{key} must be a TOML table")
    return cast(dict[str, object], value)


def _optional_table(
    raw: dict[str, object],
    key: str,
) -> dict[str, object]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"{key} must be a TOML table")
    return cast(dict[str, object], value)


def _reject_unknown_keys(
    table: dict[str, object],
    allowed: set[str],
    context: str,
) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigurationError(
            f"Unknown configuration key(s) in {context}: {', '.join(unknown)}"
        )


def _required_int(table: dict[str, object], key: str, context: str) -> int:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{context}.{key} must be an integer")
    return value


def _required_bool(table: dict[str, object], key: str, context: str) -> bool:
    value = table.get(key)
    if not isinstance(value, bool):
        raise ConfigurationError(f"{context}.{key} must be a boolean")
    return value


def _required_string(table: dict[str, object], key: str, context: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{context}.{key} must be a non-empty string")
    return value


def _required_decimal(table: dict[str, object], key: str, context: str) -> Decimal:
    if key not in table:
        raise ConfigurationError(f"{context}.{key} is required")
    return _as_decimal(table[key], f"{context}.{key}")


def _optional_decimal(
    table: dict[str, object],
    key: str,
    default: Decimal,
    context: str,
) -> Decimal:
    if key not in table:
        return default
    return _as_decimal(table[key], f"{context}.{key}")


def _as_decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise ConfigurationError(f"{field_name} must be a TOML number")
    return Decimal(value)


def _require_positive_decimal(value: Decimal, field_name: str) -> None:
    if not value.is_finite() or value <= 0:
        raise ConfigurationError(f"{field_name} must be finite and greater than zero")


def _require_probability_like(value: Decimal, field_name: str) -> None:
    if not value.is_finite() or value < 0 or value > 1:
        raise ConfigurationError(f"{field_name} must be between 0 and 1")


def _parse_env_value(value: str, path: Path, line_number: int) -> str:
    if not value:
        return ""
    if value[0] in {'"', "'"}:
        quote = value[0]
        if len(value) < 2 or value[-1] != quote:
            raise ConfigurationError(
                f"{path}:{line_number} has an unterminated quoted value"
            )
        return value[1:-1]
    comment_start = value.find(" #")
    if comment_start >= 0:
        value = value[:comment_start]
    return value.strip()
