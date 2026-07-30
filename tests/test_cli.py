import sqlite3
from pathlib import Path

import pytest
import tennis_value.odds_api as odds_api_module
from tennis_value.cli import main


def test_sports_command_lists_only_active_tennis_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("ODDS_API_KEY=secret-key\n")
    monkeypatch.delenv("ODDS_API_KEY", raising=False)

    def fake_http_get(
        url: str,
        *,
        timeout_seconds: float,
    ) -> tuple[bytes, dict[str, str]]:
        return (
            b"""
[
  {"key":"tennis_atp_test","group":"Tennis","title":"ATP Test","active":true},
  {"key":"tennis_wta_old","group":"Tennis","title":"WTA Old","active":false},
  {"key":"basketball_nba","group":"Basketball","title":"NBA","active":true}
]
""",
            {},
        )

    monkeypatch.setattr(odds_api_module, "_http_get", fake_http_get)

    exit_code = main(("--env-file", str(env_path), "sports"))
    output = capsys.readouterr()

    assert exit_code == 0
    assert output.out == "tennis_atp_test\tATP Test\n"
    assert "secret-key" not in output.out
    assert output.err == ""


def test_collect_command_preserves_raw_response_and_reports_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("ODDS_API_KEY=secret-key\n")
    database_path = tmp_path / "data" / "odds.sqlite3"
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    raw_response = (
        b'[{"id":"event-1","sport_key":"tennis_atp_test",'
        b'"sport_title":"ATP Test","commence_time":"2026-08-01T12:00:00Z",'
        b'"home_team":"Player One","away_team":"Player Two",'
        b'"bookmakers":[]}]'
    )

    def fake_http_get(
        url: str,
        *,
        timeout_seconds: float,
    ) -> tuple[bytes, dict[str, str]]:
        assert "secret-key" in url
        return (
            raw_response,
            {
                "x-requests-remaining": "99",
                "x-requests-used": "1",
                "x-requests-last": "1",
            },
        )

    monkeypatch.setattr(odds_api_module, "_http_get", fake_http_get)

    exit_code = main(
        (
            "--env-file",
            str(env_path),
            "collect",
            "--sport",
            "tennis_atp_test",
            "--database",
            str(database_path),
        )
    )
    output = capsys.readouterr()

    assert exit_code == 0
    assert database_path.exists()
    assert "Events returned: 1" in output.out
    assert "Request cost: 1" in output.out
    assert "Requests remaining: 99" in output.out
    assert "secret-key" not in output.out
    assert output.err == ""
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT raw_bytes FROM raw_odds_responses"
        ).fetchone()
    assert row == (raw_response,)
