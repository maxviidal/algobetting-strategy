import os
from decimal import Decimal
from pathlib import Path

import pytest
from tennis_value.config import (
    ConfigurationError,
    get_odds_api_key,
    load_env_file,
    load_settings,
)


def test_get_odds_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODDS_API_KEY", "test-key")

    assert get_odds_api_key() == "test-key"


def test_get_odds_api_key_requires_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ODDS_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="ODDS_API_KEY"):
        get_odds_api_key()


def test_load_env_file_reads_key_without_overwriting_existing_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        """
# local secret
ODDS_API_KEY="file-key"
SECOND_VALUE=value # comment
"""
    )
    monkeypatch.setenv("ODDS_API_KEY", "existing-key")
    monkeypatch.delenv("SECOND_VALUE", raising=False)

    assert load_env_file(env_path)
    assert get_odds_api_key() == "existing-key"
    assert os.environ["SECOND_VALUE"] == "value"


def test_load_env_file_rejects_shell_syntax(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("not a shell command\n")

    with pytest.raises(ConfigurationError, match="NAME=VALUE"):
        load_env_file(env_path)


@pytest.mark.parametrize(
    ("filename", "minimum_ev", "maximum_age"),
    [
        ("development.toml", "0.03", 300),
        ("research.toml", "0.05", 180),
    ],
)
def test_project_configuration_loads_with_typed_values(
    filename: str,
    minimum_ev: str,
    maximum_age: int,
) -> None:
    settings = load_settings(Path("configs") / filename)

    assert settings.collection.minimum_bookmakers == 5
    assert settings.collection.maximum_quote_age_seconds == maximum_age
    assert settings.pricing.margin_method == "proportional"
    assert settings.pricing.consensus_method == "median"
    assert settings.pricing.leave_one_bookmaker_out
    assert settings.signals.minimum_expected_value == Decimal(minimum_ev)
    assert settings.quality.review_expected_value == Decimal("0.20")
    assert settings.quality.minimum_normal_overround == Decimal("0.98")
    assert settings.quality.maximum_normal_overround == Decimal("1.15")
    assert settings.quality.maximum_peer_probability_range == Decimal("0.10")


def test_configuration_rejects_unknown_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "invalid.toml"
    config_path.write_text(
        """
[collection]
minimum_bookmakers = 5
maximum_quote_age_seconds = 300
unknown = true

[pricing]
margin_method = "proportional"
consensus_method = "median"
leave_one_bookmaker_out = true

[signals]
minimum_expected_value = 0.05
"""
    )

    with pytest.raises(ConfigurationError, match="unknown"):
        load_settings(config_path)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("margin_method", '"power"', "margin_method"),
        ("consensus_method", '"mean"', "consensus_method"),
        ("leave_one_bookmaker_out", "false", "leave_one_bookmaker_out"),
    ],
)
def test_configuration_rejects_unsupported_v1_pricing_options(
    tmp_path: Path,
    field: str,
    replacement: str,
    message: str,
) -> None:
    margin_method = '"proportional"'
    consensus_method = '"median"'
    leave_one_out = "true"
    if field == "margin_method":
        margin_method = replacement
    elif field == "consensus_method":
        consensus_method = replacement
    else:
        leave_one_out = replacement

    config_text = f"""
[collection]
minimum_bookmakers = 5
maximum_quote_age_seconds = 300

[pricing]
margin_method = {margin_method}
consensus_method = {consensus_method}
leave_one_bookmaker_out = {leave_one_out}

[signals]
minimum_expected_value = 0.05
"""
    config_path = tmp_path / "unsupported.toml"
    config_path.write_text(config_text)

    with pytest.raises(ConfigurationError, match=message):
        load_settings(config_path)
