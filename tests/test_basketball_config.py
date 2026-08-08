from decimal import Decimal
from pathlib import Path

from basketball_value.config import load_basketball_settings


def test_development_pilot_locks_only_2023_24() -> None:
    settings = load_basketball_settings(Path("configs/basketball_pilot_2023_24.toml"))

    assert settings.profile == "development_pilot_2023_24"
    assert settings.season_start_years == (2023,)
    assert settings.development_seasons == ("2023-24",)
    assert settings.validation_seasons == ()
    assert settings.holdout_seasons == ()
    assert settings.calibration_enabled is True
    assert settings.calibration_training_fraction == Decimal("0.6")
    assert settings.calibration_validation_fraction == Decimal("0.2")


def test_five_season_profile_remains_unchanged() -> None:
    settings = load_basketball_settings(Path("configs/basketball_research.toml"))

    assert settings.profile == "five_season_research"
    assert settings.season_start_years == (2021, 2022, 2023, 2024, 2025)
    assert settings.calibration_enabled is False
