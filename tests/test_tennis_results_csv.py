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


def test_tennisdata_timestamp_date_column_is_supported(tmp_path: Path) -> None:
    atp = tmp_path / "atp.csv"
    wta = tmp_path / "wta.csv"
    atp.write_text(
        "date_timestamp,date_human,tournament,home_name,away_name,winner_code,status\n"
        "1767320100,02 Jan 2026,United Cup ATP,Munar J.,Baez S.,2,FINISHED\n",
        encoding="utf-8",
    )
    _write(wta, "")
    fixture = ResearchFixture(
        fixture_id="fixture-tennisdata",
        tournament_key="atp_united_cup",
        tour="ATP",
        surface="hard",
        tournament_id=1,
        tournament_name="United Cup ATP",
        player_one_id=11,
        player_two_id=22,
        player_one_name="Munar, Jaume",
        player_two_name="Baez, Sebastian",
        scheduled_start=datetime(2026, 1, 2, 12, tzinfo=UTC),
    )

    matching = match_result_csvs((fixture,), atp_path=atp, wta_path=wta)

    assert matching.results["fixture-tennisdata"].winner_player_id == 22
    assert matching.quarantines == ()


def test_repeated_abbreviated_identity_retains_both_sources_for_review(
    tmp_path: Path,
) -> None:
    atp = tmp_path / "atp.csv"
    wta = tmp_path / "wta.csv"
    _write(
        atp,
        "2026-03-10,Indian Wells,Smith A.,Sinner J.,1,completed\n"
        "2026-03-11,Indian Wells,Smith A.,Alcaraz C.,2,completed\n",
    )
    _write(wta, "")
    fixtures = (
        ResearchFixture(
            fixture_id="fixture-anna",
            tournament_key="atp_indian_wells",
            tour="ATP",
            surface="hard",
            tournament_id=1,
            tournament_name="Indian Wells Men Singles",
            player_one_id=11,
            player_two_id=22,
            player_one_name="Smith, Anna",
            player_two_name="Sinner, Jannik",
            scheduled_start=datetime(2026, 3, 10, 16, tzinfo=UTC),
        ),
        ResearchFixture(
            fixture_id="fixture-alice",
            tournament_key="atp_indian_wells",
            tour="ATP",
            surface="hard",
            tournament_id=1,
            tournament_name="Indian Wells Men Singles",
            player_one_id=33,
            player_two_id=44,
            player_one_name="Smith, Alice",
            player_two_name="Alcaraz, Carlos",
            scheduled_start=datetime(2026, 3, 11, 16, tzinfo=UTC),
        ),
    )

    matching = match_result_csvs(fixtures, atp_path=atp, wta_path=wta)

    assert set(matching.results) == {"fixture-anna", "fixture-alice"}
    oddspapi_reviews = [
        value
        for value in matching.identity_reviews
        if value.source == "oddspapi"
        and value.canonical_name in {"smith an.", "smith al."}
    ]
    assert {value.original_name for value in oddspapi_reviews} == {
        "Smith, Anna",
        "Smith, Alice",
    }
    assert {value.record_id for value in oddspapi_reviews} == {
        "fixture-anna",
        "fixture-alice",
    }
    assert all(
        value.tournament_name == "Indian Wells Men Singles"
        for value in oddspapi_reviews
    )
    tennisdata_reviews = [
        value
        for value in matching.identity_reviews
        if value.source == "tennisdata" and value.canonical_name == "smith a."
    ]
    assert len(tennisdata_reviews) == 2
    assert {value.match_date.day for value in tennisdata_reviews} == {10, 11}


def test_same_abbreviated_players_in_one_fixture_are_quarantined(
    tmp_path: Path,
) -> None:
    atp = tmp_path / "atp.csv"
    wta = tmp_path / "wta.csv"
    _write(atp, "2026-03-10,Indian Wells,Smith A.,Smith A.,1,completed\n")
    _write(wta, "")
    fixture = ResearchFixture(
        fixture_id="fixture-collision",
        tournament_key="atp_indian_wells",
        tour="ATP",
        surface="hard",
        tournament_id=1,
        tournament_name="Indian Wells Men Singles",
        player_one_id=11,
        player_two_id=22,
        player_one_name="Smith, Anna",
        player_two_name="Smith, Alice",
        scheduled_start=datetime(2026, 3, 10, 16, tzinfo=UTC),
    )

    matching = match_result_csvs((fixture,), atp_path=atp, wta_path=wta)

    assert matching.results == {}
    assert matching.quarantines[0].reason == "winner_identity_mismatch"
    assert "Smith, Anna" in matching.quarantines[0].detail
    assert "Smith, Alice" in matching.quarantines[0].detail


def test_extended_tennisdata_initial_uses_first_initial_key(tmp_path: Path) -> None:
    atp = tmp_path / "atp.csv"
    wta = tmp_path / "wta.csv"
    _write(atp, "")
    _write(
        wta,
        "2026-03-10,Indian Wells,Pliskova Ka.,Sierra S.,1,completed\n",
    )
    fixture = ResearchFixture(
        fixture_id="fixture-extended-initial",
        tournament_key="wta_indian_wells",
        tour="WTA",
        surface="hard",
        tournament_id=1,
        tournament_name="Indian Wells Women Singles",
        player_one_id=11,
        player_two_id=22,
        player_one_name="Pliskova, Karolina",
        player_two_name="Sierra, Solana",
        scheduled_start=datetime(2026, 3, 10, 16, tzinfo=UTC),
    )

    matching = match_result_csvs((fixture,), atp_path=atp, wta_path=wta)

    assert matching.results[fixture.fixture_id].winner_player_id == 11
    assert matching.quarantines == ()


def test_two_letter_datasource_initial_disambiguates_colliding_names(
    tmp_path: Path,
) -> None:
    atp = tmp_path / "atp.csv"
    wta = tmp_path / "wta.csv"
    _write(atp, "")
    _write(
        wta,
        "2026-03-10,Indian Wells,Pliskova Ka.,Sierra S.,1,completed\n"
        "2026-03-10,Indian Wells,Pliskova Kr.,Sierra S.,2,completed\n",
    )
    fixtures = (
        ResearchFixture(
            fixture_id="fixture-karolina",
            tournament_key="wta_indian_wells",
            tour="WTA",
            surface="hard",
            tournament_id=1,
            tournament_name="Indian Wells Women Singles",
            player_one_id=11,
            player_two_id=22,
            player_one_name="Pliskova, Karolina",
            player_two_name="Sierra, Solana",
            scheduled_start=datetime(2026, 3, 10, 16, tzinfo=UTC),
        ),
        ResearchFixture(
            fixture_id="fixture-kristyna",
            tournament_key="wta_indian_wells",
            tour="WTA",
            surface="hard",
            tournament_id=1,
            tournament_name="Indian Wells Women Singles",
            player_one_id=33,
            player_two_id=44,
            player_one_name="Pliskova, Kristyna",
            player_two_name="Sierra, Solana",
            scheduled_start=datetime(2026, 3, 10, 16, tzinfo=UTC),
        ),
    )

    matching = match_result_csvs(fixtures, atp_path=atp, wta_path=wta)

    assert set(matching.results) == {"fixture-karolina", "fixture-kristyna"}
    assert matching.results["fixture-karolina"].winner_player_id == 11
    assert matching.results["fixture-kristyna"].winner_player_id == 44
    assert matching.quarantines == ()
    reviews = {
        (value.source, value.canonical_name, value.original_name)
        for value in matching.identity_reviews
        if value.canonical_name.startswith("pliskova ")
    }
    assert ("oddspapi", "pliskova ka.", "Pliskova, Karolina") in reviews
    assert ("oddspapi", "pliskova kr.", "Pliskova, Kristyna") in reviews
    assert ("tennisdata", "pliskova ka.", "Pliskova Ka.") in reviews
    assert ("tennisdata", "pliskova kr.", "Pliskova Kr.") in reviews


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
