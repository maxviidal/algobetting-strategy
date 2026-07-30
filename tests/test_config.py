import pytest
from tennis_value.config import get_odds_api_key


def test_get_odds_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODDS_API_KEY", "test-key")

    assert get_odds_api_key() == "test-key"


def test_get_odds_api_key_requires_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ODDS_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="ODDS_API_KEY"):
        get_odds_api_key()
