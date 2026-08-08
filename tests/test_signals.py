from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tennis_value.config import (
    AppSettings,
    PricingSettings,
    QualitySettings,
    SignalSettings,
)
from tennis_value.data.domain import Match, MatchWinnerPrice, OddsSnapshot
from tennis_value.signals import (
    ConflictingSnapshotsError,
    ExclusionReason,
    InsufficientBookmakersError,
    MarketEvaluationError,
    MissingSharpBookmakerError,
    QualityFlag,
    evaluate_market,
)

DECISION_AT = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
CALCULATED_AT = datetime(2026, 7, 30, 12, 0, 1, tzinfo=UTC)
MATCH = Match(
    match_id="match-1",
    tournament_id="tournament-1",
    player_ids=(1, 2),
    scheduled_start=datetime(2026, 7, 30, 13, 0, tzinfo=UTC),
)


def make_snapshot(
    bookmaker_id: str,
    *,
    first_odds: str = "2.00",
    second_odds: str = "2.00",
    observed_at: datetime | None = None,
    snapshot_suffix: str = "",
    match_id: str = "match-1",
) -> OddsSnapshot:
    return OddsSnapshot(
        snapshot_id=f"snapshot-{bookmaker_id}{snapshot_suffix}",
        match_id=match_id,
        bookmaker_id=bookmaker_id,
        observed_at=observed_at or DECISION_AT,
        prices=(
            MatchWinnerPrice(1, Decimal(first_odds)),
            MatchWinnerPrice(2, Decimal(second_odds)),
        ),
        source="test",
        source_event_id="event-1",
    )


def five_snapshots() -> tuple[OddsSnapshot, ...]:
    return tuple(make_snapshot(bookmaker) for bookmaker in ("a", "b", "c", "d", "e"))


def test_five_books_produce_two_evaluations_each_with_four_peers() -> None:
    result = evaluate_market(
        MATCH,
        tuple(reversed(five_snapshots())),
        decision_at=DECISION_AT,
        calculated_at=CALCULATED_AT,
        settings=AppSettings(),
    )

    assert result.eligible_bookmaker_count == 5
    assert len(result.evaluations) == 10
    assert all(evaluation.peer_count == 4 for evaluation in result.evaluations)
    assert all(not evaluation.is_candidate for evaluation in result.evaluations)
    assert [
        (evaluation.bookmaker_id, evaluation.player_id)
        for evaluation in result.evaluations
    ] == [
        ("a", 1),
        ("a", 2),
        ("b", 1),
        ("b", 2),
        ("c", 1),
        ("c", 2),
        ("d", 1),
        ("d", 2),
        ("e", 1),
        ("e", 2),
    ]
    for evaluation in result.evaluations:
        assert evaluation.snapshot_id not in evaluation.peer_snapshot_ids


def test_allow_stale_quotes_keeps_latest_pre_decision_markets() -> None:
    stale_snapshots = tuple(
        make_snapshot(
            bookmaker,
            observed_at=DECISION_AT - timedelta(hours=4),
        )
        for bookmaker in ("a", "b", "c", "d", "e")
    )

    result = evaluate_market(
        MATCH,
        stale_snapshots,
        decision_at=DECISION_AT,
        calculated_at=CALCULATED_AT,
        settings=AppSettings(),
        allow_stale_quotes=True,
    )

    assert result.eligible_bookmaker_count == 5
    assert len(result.evaluations) == 10


def test_six_books_use_all_five_peers() -> None:
    snapshots = five_snapshots() + (make_snapshot("f"),)

    result = evaluate_market(
        MATCH,
        snapshots,
        decision_at=DECISION_AT,
        calculated_at=CALCULATED_AT,
        settings=AppSettings(),
    )

    assert len(result.evaluations) == 12
    assert all(evaluation.peer_count == 5 for evaluation in result.evaluations)


def test_four_books_raise_with_coverage_details() -> None:
    with pytest.raises(InsufficientBookmakersError) as captured:
        evaluate_market(
            MATCH,
            five_snapshots()[:4],
            decision_at=DECISION_AT,
            calculated_at=CALCULATED_AT,
            settings=AppSettings(),
        )

    assert captured.value.eligible_bookmakers == 4
    assert captured.value.required_bookmakers == 5


def test_latest_point_in_time_snapshot_is_selected_and_exclusions_are_visible() -> None:
    old = make_snapshot(
        "a",
        observed_at=DECISION_AT - timedelta(seconds=60),
        snapshot_suffix="-old",
    )
    stale = make_snapshot(
        "stale",
        observed_at=DECISION_AT - timedelta(seconds=301),
    )
    future = make_snapshot(
        "future",
        observed_at=DECISION_AT + timedelta(seconds=1),
    )
    snapshots = five_snapshots() + (old, stale, future)

    result = evaluate_market(
        MATCH,
        snapshots,
        decision_at=DECISION_AT,
        calculated_at=CALCULATED_AT,
        settings=AppSettings(),
    )

    reasons = {
        exclusion.snapshot_id: exclusion.reason for exclusion in result.exclusions
    }
    assert reasons["snapshot-a-old"] is ExclusionReason.SUPERSEDED_QUOTE
    assert reasons["snapshot-stale"] is ExclusionReason.STALE_QUOTE
    assert reasons["snapshot-future"] is ExclusionReason.FUTURE_QUOTE
    assert result.eligible_bookmaker_count == 5


def test_quote_at_freshness_boundary_is_eligible() -> None:
    snapshots = tuple(
        make_snapshot(
            bookmaker,
            observed_at=DECISION_AT - timedelta(seconds=300),
        )
        for bookmaker in ("a", "b", "c", "d", "e")
    )

    result = evaluate_market(
        MATCH,
        snapshots,
        decision_at=DECISION_AT,
        calculated_at=CALCULATED_AT,
        settings=AppSettings(),
    )

    assert result.eligible_bookmaker_count == 5


def test_conflicting_latest_snapshots_are_rejected() -> None:
    conflicting = make_snapshot(
        "a",
        first_odds="2.10",
        second_odds="1.90",
        snapshot_suffix="-conflict",
    )

    with pytest.raises(ConflictingSnapshotsError, match="bookmaker 'a'"):
        evaluate_market(
            MATCH,
            five_snapshots() + (conflicting,),
            decision_at=DECISION_AT,
            calculated_at=CALCULATED_AT,
            settings=AppSettings(),
        )


def test_candidate_threshold_is_inclusive_and_non_candidates_are_retained() -> None:
    settings = replace(
        AppSettings(),
        signals=SignalSettings(minimum_expected_value=Decimal("0.05")),
    )
    snapshots = (
        make_snapshot("a", first_odds="2.10", second_odds="1.91"),
        *(make_snapshot(bookmaker) for bookmaker in ("b", "c", "d", "e")),
    )

    result = evaluate_market(
        MATCH,
        snapshots,
        decision_at=DECISION_AT,
        calculated_at=CALCULATED_AT,
        settings=settings,
    )
    target = next(
        evaluation
        for evaluation in result.evaluations
        if evaluation.bookmaker_id == "a" and evaluation.player_id == 1
    )

    assert target.expected_value == Decimal("0.050")
    assert target.is_candidate
    assert any(not evaluation.is_candidate for evaluation in result.evaluations)


def test_pinnacle_consensus_devigs_sharp_quote_and_evaluates_other_books() -> None:
    settings = replace(
        AppSettings(),
        pricing=PricingSettings(consensus_method="pinnacle"),
    )
    snapshots = (
        make_snapshot("pinnacle", first_odds="1.80", second_odds="2.20"),
        make_snapshot("a", first_odds="2.10", second_odds="1.80"),
        make_snapshot("b"),
        make_snapshot("c"),
        make_snapshot("d"),
    )

    result = evaluate_market(
        MATCH,
        snapshots,
        decision_at=DECISION_AT,
        calculated_at=CALCULATED_AT,
        settings=settings,
    )
    target = next(
        evaluation
        for evaluation in result.evaluations
        if evaluation.bookmaker_id == "a" and evaluation.player_id == 1
    )

    assert len(result.evaluations) == 8
    assert all(item.bookmaker_id != "pinnacle" for item in result.evaluations)
    assert target.consensus_method == "pinnacle"
    assert target.peer_snapshot_ids == ("snapshot-pinnacle",)
    assert target.peer_count == 1
    assert target.expected_value > 0


def test_pinnacle_consensus_fails_closed_when_sharp_quote_is_missing() -> None:
    settings = replace(
        AppSettings(),
        pricing=PricingSettings(consensus_method="pinnacle"),
    )

    with pytest.raises(MissingSharpBookmakerError, match="Pinnacle"):
        evaluate_market(
            MATCH,
            five_snapshots(),
            decision_at=DECISION_AT,
            calculated_at=CALCULATED_AT,
            settings=settings,
        )


def test_quality_flags_do_not_suppress_candidates_or_coverage() -> None:
    settings = replace(
        AppSettings(),
        quality=QualitySettings(
            review_expected_value=Decimal("0.20"),
            minimum_normal_overround=Decimal("0.98"),
            maximum_normal_overround=Decimal("1.15"),
            maximum_peer_probability_range=Decimal("0.10"),
        ),
    )
    snapshots = (
        make_snapshot("a", first_odds="3.00", second_odds="1.50"),
        make_snapshot("b", first_odds="1.10", second_odds="1.10"),
        make_snapshot("c", first_odds="1.50", second_odds="3.00"),
        make_snapshot("d", first_odds="2.00", second_odds="2.00"),
        make_snapshot("e", first_odds="3.00", second_odds="1.50"),
    )

    result = evaluate_market(
        MATCH,
        snapshots,
        decision_at=DECISION_AT,
        calculated_at=CALCULATED_AT,
        settings=settings,
    )
    target = next(
        evaluation
        for evaluation in result.evaluations
        if evaluation.bookmaker_id == "a" and evaluation.player_id == 1
    )

    assert result.eligible_bookmaker_count == 5
    assert target.is_candidate
    assert QualityFlag.LARGE_EDGE in target.quality_flags
    assert QualityFlag.SUSPICIOUS_OVERROUND in target.quality_flags
    assert QualityFlag.WIDE_PEER_DISPERSION in target.quality_flags
    assert "snapshot-b" in target.suspicious_overround_snapshot_ids


def test_large_edge_flag_is_inclusive_at_review_threshold() -> None:
    snapshots = (
        make_snapshot("a", first_odds="2.40", second_odds="1.72"),
        *(make_snapshot(bookmaker) for bookmaker in ("b", "c", "d", "e")),
    )

    result = evaluate_market(
        MATCH,
        snapshots,
        decision_at=DECISION_AT,
        calculated_at=CALCULATED_AT,
        settings=AppSettings(),
    )
    target = next(
        evaluation
        for evaluation in result.evaluations
        if evaluation.bookmaker_id == "a" and evaluation.player_id == 1
    )

    assert target.expected_value == Decimal("0.200")
    assert QualityFlag.LARGE_EDGE in target.quality_flags


def test_quality_range_boundaries_are_inclusive_and_dispersion_is_strict() -> None:
    snapshots = (
        make_snapshot("a", first_odds="2.00", second_odds="2.00"),
        make_snapshot("b", first_odds="3.00", second_odds="1.50"),
        make_snapshot("c", first_odds="2.00", second_odds="2.00"),
        make_snapshot("d", first_odds="2.00", second_odds="2.00"),
        make_snapshot("e", first_odds="2.00", second_odds="2.00"),
    )
    initial = evaluate_market(
        MATCH,
        snapshots,
        decision_at=DECISION_AT,
        calculated_at=CALCULATED_AT,
        settings=AppSettings(),
    )
    target = next(
        evaluation
        for evaluation in initial.evaluations
        if evaluation.bookmaker_id == "a" and evaluation.player_id == 1
    )
    boundary_settings = replace(
        AppSettings(),
        quality=QualitySettings(
            minimum_normal_overround=Decimal(1),
            maximum_normal_overround=Decimal(1),
            maximum_peer_probability_range=target.peer_probability_range,
        ),
    )

    result = evaluate_market(
        MATCH,
        snapshots,
        decision_at=DECISION_AT,
        calculated_at=CALCULATED_AT,
        settings=boundary_settings,
    )
    boundary_target = next(
        evaluation
        for evaluation in result.evaluations
        if evaluation.bookmaker_id == "a" and evaluation.player_id == 1
    )

    assert QualityFlag.SUSPICIOUS_OVERROUND not in boundary_target.quality_flags
    assert QualityFlag.WIDE_PEER_DISPERSION not in boundary_target.quality_flags


def test_wrong_match_and_participants_have_explicit_exclusion_reasons() -> None:
    wrong_match = make_snapshot("wrong-match", match_id="match-2")
    wrong_participants = OddsSnapshot(
        snapshot_id="snapshot-wrong-players",
        match_id=MATCH.match_id,
        bookmaker_id="wrong-players",
        observed_at=DECISION_AT,
        prices=(
            MatchWinnerPrice(3, Decimal("2.00")),
            MatchWinnerPrice(4, Decimal("2.00")),
        ),
        source="test",
        source_event_id="event-1",
    )

    result = evaluate_market(
        MATCH,
        five_snapshots() + (wrong_match, wrong_participants),
        decision_at=DECISION_AT,
        calculated_at=CALCULATED_AT,
        settings=AppSettings(),
    )

    reasons = {exclusion.reason for exclusion in result.exclusions}
    assert ExclusionReason.WRONG_MATCH in reasons
    assert ExclusionReason.PARTICIPANT_MISMATCH in reasons


@pytest.mark.parametrize(
    "decision_at",
    [
        datetime(2026, 7, 30, 12, 0),
        datetime.fromisoformat("2026-07-30T14:00:00+02:00"),
    ],
)
def test_decision_time_must_be_utc(decision_at: datetime) -> None:
    with pytest.raises(MarketEvaluationError, match="decision_at"):
        evaluate_market(
            MATCH,
            five_snapshots(),
            decision_at=decision_at,
            calculated_at=CALCULATED_AT,
            settings=AppSettings(),
        )


def test_decision_time_must_be_before_match_start() -> None:
    with pytest.raises(MarketEvaluationError, match="scheduled start"):
        evaluate_market(
            MATCH,
            five_snapshots(),
            decision_at=MATCH.scheduled_start,
            calculated_at=CALCULATED_AT,
            settings=AppSettings(),
        )
