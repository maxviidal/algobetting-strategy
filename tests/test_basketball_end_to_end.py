import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from basketball_value.config import load_basketball_settings
from basketball_value.domain import (
    Game,
    GameResult,
    MoneylinePrice,
    MoneylineSnapshot,
    stable_game_id,
)
from basketball_value.reporting import export_reports, run_backtest
from basketball_value.workflow import BasketballDataset


def _snapshot(
    game: Game,
    bookmaker: str,
    home: str,
    away: str,
    minute: int,
) -> MoneylineSnapshot:
    return MoneylineSnapshot(
        snapshot_id=f"{bookmaker}-{minute}",
        game_id=game.game_id,
        bookmaker_id=bookmaker,
        observed_at=datetime(2026, 1, 2, 2, minute, tzinfo=UTC),
        prices=(
            MoneylinePrice("LAL", Decimal(home)),
            MoneylinePrice("BOS", Decimal(away)),
        ),
        source="test",
        source_event_id="odds-1",
    )


def test_synthetic_five_book_pipeline_selects_settles_and_exports(
    tmp_path: Path,
) -> None:
    game_id = stable_game_id("balldontlie", "1")
    game = Game(
        game_id=game_id,
        source="balldontlie",
        source_event_id="1",
        season="2025-26",
        home_team_id="LAL",
        away_team_id="BOS",
        scheduled_start=datetime(2026, 1, 2, 3, tzinfo=UTC),
    )
    entry = tuple(
        _snapshot(
            game,
            bookmaker,
            "3.0" if bookmaker == "a" else "2.0",
            "1.5" if bookmaker == "a" else "2.0",
            0,
        )
        for bookmaker in ("a", "b", "c", "d", "e")
    )
    closing = tuple(
        _snapshot(game, bookmaker, "2.0", "2.0", 55)
        for bookmaker in ("a", "b", "c", "d", "e")
    )
    dataset = BasketballDataset(
        games=(game,),
        results={
            game_id: GameResult(
                game_id=game_id,
                home_score=121,
                away_score=119,
                final=True,
                postponed=False,
            )
        },
        entry_snapshots=entry,
        closing_snapshots=closing,
        requested_timestamps=2,
        completed_timestamps=2,
        unmatched_events=0,
        matched_events=2,
        matched_games=1,
        quarantined_event_ids=(),
        market_exclusions=(),
        result_quarantines=(),
    )
    settings = load_basketball_settings(
        Path("configs/basketball_research.toml")
    )

    run = run_backtest(dataset, settings)
    paths = export_reports(
        output_directory=tmp_path,
        dataset=dataset,
        run=run,
        settings=settings,
    )

    assert len(run.offers) == 10
    assert len(run.candidates) == 1
    assert run.candidates[0].offer.team_id == "LAL"
    assert run.candidates[0].closing_value == Decimal("0.5")
    assert run.settled[0].won is True
    assert run.settled[0].flat_profit == Decimal("2.0")
    assert all(path.exists() for path in paths)
    summary = json.loads(paths[2].read_text())
    assert summary["holdout_2025_26"]["candidates"] == 1
    assert summary["acceptance"]["conclusion"] == "inconclusive"
