from datetime import UTC, datetime
from pathlib import Path

from tennis_value.oddspapi_research import ResearchFixture
from tennis_value.results_csv import match_result_csvs


def _fixture(fixture_id: str = "fixture-1") -> ResearchFixture:
    return ResearchFixture(
        fixture_id=fixture_id,
        tournament_key="atp_indian_wells",
        tour="ATP",
        surface="hard",
        tournament_id=1,
        tournament_name="Indian Wells Men Singles",
        player_one_id=11,
        player_two_id=22,
        player_one_name="Carlos Alcaraz",
        player_two_name="Jannik Sinner",
        scheduled_start=datetime(2026, 3, 10, 16, tzinfo=UTC),
    )


def _write(path: Path, body: str) -> None:
    path.write_text(
        "date,tournament_name,home_name,away_name,winner_code,status\n" + body,
        encoding="utf-8",
    )


def test_result_matching_is_unordered_and_maps_winner_code(tmp_path: Path) -> None:
    atp = tmp_path / "atp.csv"
    wta = tmp_path / "wta.csv"
    _write(atp, "2026-03-10,Indian Wells,Jannik Sinner,Carlos Alcaraz,2,completed\n")
    _write(wta, "")

    matching = match_result_csvs((_fixture(),), atp_path=atp, wta_path=wta)

    assert matching.results["fixture-1"].winner_player_id == 11
    assert matching.quarantines == ()
    assert len(matching.source_checksums["ATP"]) == 64


def test_ambiguous_and_retired_results_are_quarantined(tmp_path: Path) -> None:
    atp = tmp_path / "atp.csv"
    wta = tmp_path / "wta.csv"
    duplicate = "2026-03-10,Indian Wells,Carlos Alcaraz,Jannik Sinner,1,completed\n"
    _write(atp, duplicate + duplicate)
    _write(wta, "")

    ambiguous = match_result_csvs((_fixture(),), atp_path=atp, wta_path=wta)
    assert ambiguous.quarantines[0].reason == "ambiguous_result_match"

    _write(
        atp,
        "2026-03-10,Indian Wells,Carlos Alcaraz,Jannik Sinner,1,retired\n",
    )
    retired = match_result_csvs((_fixture(),), atp_path=atp, wta_path=wta)
    assert retired.quarantines[0].reason == "non_completed_result"
