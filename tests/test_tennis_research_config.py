from decimal import Decimal
from pathlib import Path

from tennis_value.research_config import load_tennis_research_settings


def test_locked_tennis_calibration_profile_loads() -> None:
    settings = load_tennis_research_settings(
        Path("configs/tennis_calibration_2026.toml")
    )

    assert settings.entry_minutes_before_start == 60
    assert settings.closing_minutes_before_start == 5
    assert settings.bootstrap_samples == 200
    assert settings.block_days == 7
    assert settings.minimum_training_matches == 200
    assert settings.lower_quantile == Decimal("0.05")
    assert settings.regularization == Decimal("10")
    assert settings.ev_threshold == Decimal("0.04")
    assert len(settings.bookmakers) == 15
    assert settings.bookmakers[0] == "pinnacle"
    assert len(settings.tournaments) == 11
    assert {value.tour for value in settings.tournaments} == {"ATP", "WTA"}
    assert {value.surface for value in settings.tournaments} == {"hard", "clay"}


def test_main_tour_profile_includes_250_level_events_and_grass() -> None:
    settings = load_tennis_research_settings(
        Path("configs/tennis_main_tour_2026.toml")
    )

    assert settings.profile == "tennis_main_tour_2026"
    assert len(settings.bookmakers) == 15
    assert settings.bookmakers[0] == "pinnacle"
    assert len(settings.tournaments) == 81
    assert {value.tour for value in settings.tournaments} == {"ATP", "WTA"}
    assert {value.surface for value in settings.tournaments} == {
        "hard",
        "clay",
        "grass",
    }
    keys = {value.key for value in settings.tournaments}
    assert {"atp_houston", "atp_kitzbuhel", "wta_bogota", "wta_iasi"} <= keys
