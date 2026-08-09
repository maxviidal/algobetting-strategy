import csv
import gzip
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from betting_core.calibration import CalibrationObservation
from tennis_value.calibration_reporting import (
    assign_chronological_phases,
    export_tennis_calibration_report,
    fit_tour_calibrations,
)
from tennis_value.config import load_settings
from tennis_value.research_config import load_tennis_research_settings


def _observations(tour: str, *, inverted: bool) -> tuple[CalibrationObservation, ...]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for index in range(340):
        probability = Decimal("0.65") if index % 2 else Decimal("0.35")
        expected = probability > Decimal("0.5")
        rows.append(
            CalibrationObservation(
                game_id=f"{tour}-{index}",
                scheduled_start=start + timedelta(days=index),
                home_probability=probability,
                home_won=(not expected if inverted else expected),
            )
        )
    return tuple(rows)


def test_chronological_phases_do_not_cross_dates_or_future_boundaries() -> None:
    observations = _observations("ATP", inverted=False)[:10]
    phases = assign_chronological_phases(
        observations,
        training_fraction=Decimal("0.6"),
        validation_fraction=Decimal("0.2"),
    )

    assert len(phases["training"]) == 6
    assert len(phases["validation"]) == 2
    assert len(phases["test"]) == 2
    assert (
        phases["training"][-1].scheduled_start < phases["validation"][0].scheduled_start
    )
    assert phases["validation"][-1].scheduled_start < phases["test"][0].scheduled_start


def test_atp_and_wta_are_fit_as_separate_deterministic_models() -> None:
    settings = load_tennis_research_settings(
        Path("configs/tennis_calibration_2026.toml")
    )
    observations = {
        "ATP": _observations("ATP", inverted=False),
        "WTA": _observations("WTA", inverted=True),
    }

    first = fit_tour_calibrations(
        observations,
        match_rates={"ATP": Decimal(1), "WTA": Decimal(1)},
        settings=settings,
    )
    second = fit_tour_calibrations(
        observations,
        match_rates={"ATP": Decimal(1), "WTA": Decimal(1)},
        settings=settings,
    )

    assert first["ATP"].validation_model is not None
    assert first["WTA"].validation_model is not None
    assert first["ATP"].validation_model == second["ATP"].validation_model
    assert (
        first["ATP"].validation_model.point_model
        != first["WTA"].validation_model.point_model
    )
    assert first["ATP"].validation_model.training_observations >= 200
    assert (
        first["ATP"].validation_model.trained_through
        < first["ATP"].phases["validation"][0].scheduled_start
    )


def test_complete_synthetic_atp_wta_export_workflow(tmp_path: Path) -> None:
    locked = load_tennis_research_settings(Path("configs/tennis_calibration_2026.toml"))
    settings = replace(
        locked,
        minimum_training_matches=2,
        bootstrap_samples=5,
    )
    cache = tmp_path / "cache"
    atp_spec = settings.tournaments[0]
    wta_spec = next(value for value in settings.tournaments if value.key == "wta_doha")
    fixture_rows: dict[str, list[dict[str, object]]] = {
        value.key: [] for value in settings.tournaments
    }
    result_rows: dict[str, list[dict[str, object]]] = {"ATP": [], "WTA": []}
    for tour, specification in (("ATP", atp_spec), ("WTA", wta_spec)):
        for index in range(6):
            fixture_id = f"{tour.lower()}-{index}"
            scheduled = specification.from_time + timedelta(days=index + 1, hours=16)
            first_name = f"{tour} First {index}"
            second_name = f"{tour} Second {index}"
            fixture_rows[specification.key].append(
                {
                    "fixtureId": fixture_id,
                    "statusId": 2,
                    "sportId": settings.sport_id,
                    "startTime": scheduled.isoformat(),
                    "tournamentId": 100 if tour == "ATP" else 200,
                    "tournamentName": " ".join(specification.name_tokens),
                    "participant1Id": 1000 + index + (0 if tour == "ATP" else 100),
                    "participant2Id": 2000 + index + (0 if tour == "ATP" else 100),
                    "participant1Name": first_name,
                    "participant2Name": second_name,
                }
            )
            result_rows[tour].append(
                {
                    "date": scheduled.date().isoformat(),
                    "tournament_name": " ".join(specification.name_tokens),
                    "home_name": first_name,
                    "away_name": second_name,
                    "winner_code": "1" if tour == "ATP" else "2",
                    "status": "completed",
                }
            )
            groups = tuple(
                settings.bookmakers[start : start + 3]
                for start in range(0, len(settings.bookmakers), 3)
            )
            for group_index, group in enumerate(groups):
                _write_gzip_json(
                    cache / "historical" / f"{fixture_id}_{group_index}.json.gz",
                    _history_payload(fixture_id, group, scheduled),
                )
    for specification in settings.tournaments:
        _write_gzip_json(
            cache / "fixtures" / f"{specification.key}.json.gz",
            fixture_rows[specification.key],
        )
    atp_csv = tmp_path / "atp.csv"
    wta_csv = tmp_path / "wta.csv"
    _write_results(atp_csv, result_rows["ATP"])
    _write_results(wta_csv, result_rows["WTA"])

    exported = export_tennis_calibration_report(
        research_settings=settings,
        model_settings=load_settings(Path("configs/research.toml")),
        cache_directory=cache,
        atp_results_csv=atp_csv,
        wta_results_csv=wta_csv,
        artifact_directory=tmp_path / "artifacts",
        output_directory=tmp_path / "reports",
    )

    assert exported.fixture_count == 12
    assert exported.matched_results == 12
    assert exported.offer_count == 336
    assert exported.conclusion == "exploratory_only"
    assert (exported.artifact_directory / "atp.json").is_file()
    assert (exported.artifact_directory / "wta.json").is_file()
    summary = json.loads(exported.summary_json_path.read_text())
    assert summary["overall_conclusion"] == "exploratory_only"
    assert set(summary["phase_metrics"]) == {"validation", "test"}
    with exported.offers_path.open(newline="", encoding="utf-8") as handle:
        offers = list(csv.DictReader(handle))
    training = [value for value in offers if value["phase"] == "training"]
    out_of_sample = [
        value for value in offers if value["phase"] in {"validation", "test"}
    ]
    assert training and training[0]["p_safe"] == ""
    assert out_of_sample and out_of_sample[0]["p_safe"] != ""
    assert all(value["freshness_enforced"] == "False" for value in offers)
    assert all(
        value["safe_expected_value"] != value["raw_expected_value"]
        for value in out_of_sample
    )


def _write_gzip_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(payload).encode(), mtime=0))


def _write_results(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "date",
                "tournament_name",
                "home_name",
                "away_name",
                "winner_code",
                "status",
            ),
        )
        writer.writeheader()
        writer.writerows(rows)


def _history_payload(
    fixture_id: str,
    bookmakers: tuple[str, ...],
    scheduled: datetime,
) -> dict[str, object]:
    values: dict[str, object] = {}
    for bookmaker in bookmakers:
        first_odds = "2.02" if bookmaker == "pinnacle" else "3.00"
        second_odds = "2.02" if bookmaker == "pinnacle" else "1.50"
        values[bookmaker] = {
            "markets": {
                "121": {
                    "outcomes": {
                        "121": {
                            "players": {"0": _price_history(first_odds, scheduled)}
                        },
                        "122": {
                            "players": {"0": _price_history(second_odds, scheduled)}
                        },
                    }
                }
            }
        }
    return {"fixtureId": fixture_id, "bookmakers": values}


def _price_history(odds: str, scheduled: datetime) -> list[dict[str, object]]:
    return [
        {
            "createdAt": (scheduled - timedelta(minutes=120)).isoformat(),
            "price": odds,
            "active": True,
        },
        {
            "createdAt": (scheduled - timedelta(minutes=10)).isoformat(),
            "price": odds,
            "active": True,
        },
    ]
