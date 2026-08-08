import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import tennis_value.data.odds_api as odds_api_module
from tennis_value.cli import main
from tennis_value.data.ingestion import ingest_odds_api_json
from tennis_value.data.storage import SqliteOddsRepository


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
        row = connection.execute("SELECT raw_bytes FROM raw_odds_responses").fetchone()
    assert row == (raw_response,)


def test_local_commands_approve_normalize_and_evaluate_without_api_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "odds.sqlite3"
    bookmakers = []
    for bookmaker in ("a", "b", "c", "d", "pinnacle"):
        first_odds, second_odds = (3.0, 1.5) if bookmaker == "a" else (2.0, 2.0)
        bookmakers.append(
            {
                "key": bookmaker,
                "title": f"Bookmaker {bookmaker.upper()}",
                "last_update": "2026-08-01T09:59:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-08-01T09:59:00Z",
                        "outcomes": [
                            {"name": "Player One", "price": first_odds},
                            {"name": "Player Two", "price": second_odds},
                        ],
                    }
                ],
            }
        )
    raw_bytes = json.dumps(
        [
            {
                "id": "event-1",
                "sport_key": "tennis_atp_test",
                "sport_title": "ATP Test",
                "commence_time": "2026-08-01T12:00:00Z",
                "home_team": "Player One",
                "away_team": "Player Two",
                "bookmakers": bookmakers,
            }
        ]
    ).encode()
    response = ingest_odds_api_json(
        raw_bytes,
        collected_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        source="test",
    )
    with sqlite3.connect(database_path) as connection:
        SqliteOddsRepository(connection).save_raw_response(response)
    monkeypatch.delenv("ODDS_API_KEY", raising=False)

    assert main(("players", "pending", "--database", str(database_path))) == 0
    pending_output = capsys.readouterr()
    assert "Pending player names: 2" in pending_output.out
    assert "Player One\tunknown" in pending_output.out
    assert "Player Two\tunknown" in pending_output.out

    assert (
        main(
            (
                "players",
                "approve",
                "--all",
                "--database",
                str(database_path),
            )
        )
        == 0
    )
    approval_output = capsys.readouterr()
    assert "Approved player identities: 2" in approval_output.out

    assert main(("normalize", "--database", str(database_path))) == 0
    normalization_output = capsys.readouterr()
    assert "Matches saved: 1" in normalization_output.out
    assert "Bookmaker snapshots saved: 5" in normalization_output.out

    assert (
        main(
            (
                "evaluate",
                "--database",
                str(database_path),
                "--config",
                "configs/research.toml",
            )
        )
        == 0
    )
    evaluation_output = capsys.readouterr()
    assert "Matches evaluated: 1" in evaluation_output.out
    assert "Offers evaluated: 8" in evaluation_output.out
    assert "Candidates: 1" in evaluation_output.out
    assert "Player One vs Player Two" in evaluation_output.out
    assert "odds=3.0" in evaluation_output.out
    assert "ev=50.00%" in evaluation_output.out
    assert evaluation_output.err == ""
