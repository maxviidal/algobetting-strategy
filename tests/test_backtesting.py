from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tennis_value.backtesting import (
    KellySettings,
    MatchResult,
    ResultStatus,
    SelectedCandidate,
    SettlementStatus,
    backtest_candidates,
    backtest_kelly_candidates,
    kelly_fraction,
    kelly_stake,
    select_best_candidates,
    select_one_candidate_per_match,
)
from tennis_value.signals import OfferEvaluation

DECISION_AT = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
SETTLED_AT = DECISION_AT + timedelta(hours=3)


def make_evaluation(
    *,
    match_id: str = "match-1",
    player_id: int = 1,
    bookmaker_id: str = "book-a",
    odds: str = "2.20",
    ev: str = "0.10",
    candidate: bool = True,
) -> OfferEvaluation:
    return OfferEvaluation(
        match_id=match_id,
        bookmaker_id=bookmaker_id,
        snapshot_id=f"snapshot-{bookmaker_id}",
        player_id=player_id,
        offered_odds=Decimal(odds),
        target_overround=Decimal("1.05"),
        consensus_probability=Decimal("0.5"),
        consensus_fair_odds=Decimal("2"),
        expected_value=Decimal(ev),
        is_candidate=candidate,
        peer_count=5,
        peer_snapshot_ids=("peer-1",),
        minimum_peer_probability=Decimal("0.48"),
        maximum_peer_probability=Decimal("0.52"),
        peer_probability_range=Decimal("0.04"),
        margin_method="power",
        consensus_method="median",
        decision_at=DECISION_AT,
        calculated_at=DECISION_AT,
        quality_flags=(),
        suspicious_overround_snapshot_ids=(),
    )


def test_select_best_candidates_keeps_highest_sportsbook_price() -> None:
    selected = select_best_candidates(
        (
            make_evaluation(bookmaker_id="book-a", odds="2.10", ev="0.05"),
            make_evaluation(bookmaker_id="book-b", odds="2.30", ev="0.15"),
            make_evaluation(bookmaker_id="matchbook", odds="2.50", ev="0.25"),
            make_evaluation(player_id=2, bookmaker_id="book-c", odds="3.00"),
            make_evaluation(bookmaker_id="book-d", candidate=False),
        )
    )

    selected_keys = [
        (candidate.player_id, candidate.bookmaker_id) for candidate in selected
    ]
    assert selected_keys == [
        (1, "book-b"),
        (2, "book-c"),
    ]


def test_backtest_reports_profit_hit_rate_and_drawdown() -> None:
    candidates = (
        SelectedCandidate(
            match_id="match-loss",
            player_id=1,
            bookmaker_id="book-a",
            snapshot_id="snapshot-loss",
            offered_odds=Decimal("2.00"),
            consensus_probability=Decimal("0.55"),
            expected_value=Decimal("0.10"),
            decision_at=DECISION_AT,
        ),
        SelectedCandidate(
            match_id="match-win",
            player_id=2,
            bookmaker_id="book-b",
            snapshot_id="snapshot-win",
            offered_odds=Decimal("3.00"),
            consensus_probability=Decimal("0.40"),
            expected_value=Decimal("0.10"),
            decision_at=DECISION_AT + timedelta(minutes=1),
        ),
    )
    results = {
        "match-loss": MatchResult(
            match_id="match-loss",
            status=ResultStatus.COMPLETED,
            winner_player_id=2,
            settled_at=SETTLED_AT,
            source="test",
        ),
        "match-win": MatchResult(
            match_id="match-win",
            status=ResultStatus.COMPLETED,
            winner_player_id=2,
            settled_at=SETTLED_AT,
            source="test",
        ),
    }

    report = backtest_candidates(candidates, results)

    assert report.selected_candidates == 2
    assert report.settled_bets == 2
    assert report.wins == 1
    assert report.losses == 1
    assert report.turnover == Decimal("2")
    assert report.profit == Decimal("1")
    assert report.roi == Decimal("0.5")
    assert report.yield_ == Decimal("0.5")
    assert report.hit_rate == Decimal("0.5")
    assert report.maximum_drawdown == Decimal("1")
    assert [bet.status for bet in report.bets] == [
        SettlementStatus.LOSS,
        SettlementStatus.WIN,
    ]


def test_backtest_voids_non_completed_matches_and_tracks_missing_results() -> None:
    candidates = (
        SelectedCandidate(
            match_id="retired",
            player_id=1,
            bookmaker_id="book-a",
            snapshot_id="snapshot-retired",
            offered_odds=Decimal("2.00"),
            consensus_probability=Decimal("0.5"),
            expected_value=Decimal("0.1"),
            decision_at=DECISION_AT,
        ),
        SelectedCandidate(
            match_id="missing",
            player_id=1,
            bookmaker_id="book-a",
            snapshot_id="snapshot-missing",
            offered_odds=Decimal("2.00"),
            consensus_probability=Decimal("0.5"),
            expected_value=Decimal("0.1"),
            decision_at=DECISION_AT,
        ),
    )
    results = {
        "retired": MatchResult(
            match_id="retired",
            status=ResultStatus.RETIRED,
            settled_at=SETTLED_AT,
            source="test",
        )
    }

    report = backtest_candidates(candidates, results)

    assert report.settled_bets == 0
    assert report.void_bets == 1
    assert report.missing_results == 1
    assert report.turnover == Decimal(0)
    assert report.profit == Decimal(0)
    assert [bet.status for bet in report.bets] == [
        SettlementStatus.MISSING_RESULT,
        SettlementStatus.VOID,
    ]


def test_backtest_rejects_result_that_predates_signal() -> None:
    candidate = SelectedCandidate(
        match_id="match-1",
        player_id=1,
        bookmaker_id="book-a",
        snapshot_id="snapshot-1",
        offered_odds=Decimal("2.00"),
        consensus_probability=Decimal("0.5"),
        expected_value=Decimal("0.1"),
        decision_at=DECISION_AT,
    )
    result = MatchResult(
        match_id="match-1",
        status=ResultStatus.COMPLETED,
        winner_player_id=1,
        settled_at=DECISION_AT - timedelta(seconds=1),
        source="test",
    )

    with pytest.raises(ValueError, match="predates"):
        backtest_candidates((candidate,), {"match-1": result})


def test_completed_result_requires_winner_and_retirement_cannot_have_one() -> None:
    with pytest.raises(ValueError, match="winner_player_id"):
        MatchResult(
            match_id="match-1",
            status=ResultStatus.COMPLETED,
            settled_at=SETTLED_AT,
            source="test",
        )
    with pytest.raises(ValueError, match="must not"):
        MatchResult(
            match_id="match-1",
            status=ResultStatus.RETIRED,
            winner_player_id=1,
            settled_at=SETTLED_AT,
            source="test",
        )


def test_result_status_must_use_the_explicit_enum() -> None:
    with pytest.raises(ValueError, match="ResultStatus"):
        MatchResult(
            match_id="match-1",
            status="completed",  # type: ignore[arg-type]
            winner_player_id=1,
            settled_at=SETTLED_AT,
            source="test",
        )


def test_kelly_calculation_uses_the_configured_quarter_fraction() -> None:
    candidate = SelectedCandidate(
        match_id="match-1",
        player_id=1,
        bookmaker_id="book-a",
        snapshot_id="snapshot-1",
        offered_odds=Decimal("2.00"),
        consensus_probability=Decimal("0.60"),
        expected_value=Decimal("0.20"),
        decision_at=DECISION_AT,
    )

    assert kelly_fraction(candidate) == Decimal("0.20")
    assert kelly_stake(
        candidate,
        available_equity=Decimal("10000"),
        settings=KellySettings(),
    ) == Decimal("500.0000")


def test_kelly_backtest_uses_one_selection_per_match_and_updates_equity() -> None:
    candidates = (
        SelectedCandidate(
            match_id="match-1",
            player_id=1,
            bookmaker_id="book-a",
            snapshot_id="snapshot-a",
            offered_odds=Decimal("2.00"),
            consensus_probability=Decimal("0.60"),
            expected_value=Decimal("0.20"),
            decision_at=DECISION_AT,
        ),
        SelectedCandidate(
            match_id="match-1",
            player_id=2,
            bookmaker_id="book-b",
            snapshot_id="snapshot-b",
            offered_odds=Decimal("3.00"),
            consensus_probability=Decimal("0.40"),
            expected_value=Decimal("0.10"),
            decision_at=DECISION_AT,
        ),
        SelectedCandidate(
            match_id="match-2",
            player_id=3,
            bookmaker_id="book-a",
            snapshot_id="snapshot-c",
            offered_odds=Decimal("2.00"),
            consensus_probability=Decimal("0.60"),
            expected_value=Decimal("0.20"),
            decision_at=SETTLED_AT + timedelta(minutes=1),
        ),
    )
    results = {
        "match-1": MatchResult(
            match_id="match-1",
            status=ResultStatus.COMPLETED,
            winner_player_id=1,
            settled_at=SETTLED_AT,
            source="test",
        ),
        "match-2": MatchResult(
            match_id="match-2",
            status=ResultStatus.COMPLETED,
            winner_player_id=4,
            settled_at=SETTLED_AT + timedelta(hours=3),
            source="test",
        ),
    }

    report = backtest_kelly_candidates(candidates, results)

    assert report.selected_candidates == 2
    assert report.wins == 1
    assert report.losses == 1
    assert report.turnover == Decimal("1025.000000")
    assert report.final_equity == Decimal("9975.000000")
    assert report.profit == Decimal("-25.000000")
    assert [bet.stake for bet in report.bets] == [
        Decimal("500.0000"),
        Decimal("525.000000"),
    ]


def test_kelly_backtest_does_not_reuse_cash_for_overlapping_bets() -> None:
    candidates = tuple(
        SelectedCandidate(
            match_id=f"match-{player_id}",
            player_id=player_id,
            bookmaker_id="book-a",
            snapshot_id=f"snapshot-{player_id}",
            offered_odds=Decimal("2.00"),
            consensus_probability=Decimal("1.00"),
            expected_value=Decimal("1.00"),
            decision_at=DECISION_AT + timedelta(minutes=player_id),
        )
        for player_id in (1, 2)
    )
    results = {
        candidate.match_id: MatchResult(
            match_id=candidate.match_id,
            status=ResultStatus.COMPLETED,
            winner_player_id=999,
            settled_at=SETTLED_AT,
            source="test",
        )
        for candidate in candidates
    }

    report = backtest_kelly_candidates(
        candidates,
        results,
        settings=KellySettings(kelly_fraction=Decimal("1")),
    )

    assert report.turnover == Decimal("10000")
    assert report.final_equity == Decimal("0")


def test_select_one_candidate_per_match_prefers_higher_ev() -> None:
    candidates = (
        SelectedCandidate(
            match_id="match-1",
            player_id=1,
            bookmaker_id="book-a",
            snapshot_id="snapshot-a",
            offered_odds=Decimal("2"),
            consensus_probability=Decimal("0.55"),
            expected_value=Decimal("0.10"),
            decision_at=DECISION_AT,
        ),
        SelectedCandidate(
            match_id="match-1",
            player_id=2,
            bookmaker_id="book-b",
            snapshot_id="snapshot-b",
            offered_odds=Decimal("3"),
            consensus_probability=Decimal("0.4"),
            expected_value=Decimal("0.20"),
            decision_at=DECISION_AT,
        ),
    )

    assert select_one_candidate_per_match(candidates) == (candidates[1],)
