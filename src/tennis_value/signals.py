"""Point-in-time market selection and value-signal evaluation."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from tennis_value.config import AppSettings
from tennis_value.consensus import (
    ConsensusEstimate,
    leave_one_out_median_consensus,
    sharp_bookmaker_consensus,
)
from tennis_value.data.domain import Match, OddsSnapshot, PlayerId
from tennis_value.pricing import (
    DeViggedMarket,
    expected_value,
    power_devig,
)


class QualityFlag(StrEnum):
    """Non-suppressing warnings attached to an offer evaluation."""

    LARGE_EDGE = "large_edge"
    SUSPICIOUS_OVERROUND = "suspicious_overround"
    WIDE_PEER_DISPERSION = "wide_peer_dispersion"


class ExclusionReason(StrEnum):
    """Why a snapshot did not become its bookmaker's selected quote."""

    WRONG_MATCH = "wrong_match"
    PARTICIPANT_MISMATCH = "participant_mismatch"
    FUTURE_QUOTE = "future_quote"
    SUPERSEDED_QUOTE = "superseded_quote"
    DUPLICATE_QUOTE = "duplicate_quote"


@dataclass(frozen=True, slots=True)
class SnapshotExclusion:
    """An observable reason for excluding one input snapshot."""

    snapshot_id: str
    bookmaker_id: str
    reason: ExclusionReason
    detail: str


@dataclass(frozen=True, slots=True)
class OfferEvaluation:
    """One bookmaker outcome evaluated against independent peer prices."""

    match_id: str
    bookmaker_id: str
    snapshot_id: str
    player_id: PlayerId
    offered_odds: Decimal
    target_overround: Decimal
    consensus_probability: Decimal
    consensus_fair_odds: Decimal
    expected_value: Decimal
    is_candidate: bool
    peer_count: int
    peer_snapshot_ids: tuple[str, ...]
    minimum_peer_probability: Decimal
    maximum_peer_probability: Decimal
    peer_probability_range: Decimal
    margin_method: str
    consensus_method: str
    decision_at: datetime
    calculated_at: datetime
    quality_flags: tuple[QualityFlag, ...]
    suspicious_overround_snapshot_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MarketEvaluationResult:
    """All eligible offer evaluations and observable snapshot exclusions."""

    match_id: str
    decision_at: datetime
    calculated_at: datetime
    eligible_bookmaker_count: int
    evaluations: tuple[OfferEvaluation, ...]
    exclusions: tuple[SnapshotExclusion, ...]


class MarketEvaluationError(ValueError):
    """Base exception for a market that cannot be evaluated safely."""


class ConflictingSnapshotsError(MarketEvaluationError):
    """Raised for different quotes from one bookmaker at the same timestamp."""


class InsufficientBookmakersError(MarketEvaluationError):
    """Raised when fewer than the configured number of books remain."""

    def __init__(
        self,
        *,
        eligible_bookmakers: int,
        required_bookmakers: int,
        exclusions: tuple[SnapshotExclusion, ...],
    ) -> None:
        self.eligible_bookmakers = eligible_bookmakers
        self.required_bookmakers = required_bookmakers
        self.exclusions = exclusions
        super().__init__(
            f"market has {eligible_bookmakers} eligible bookmakers; "
            f"{required_bookmakers} required"
        )


class MissingSharpBookmakerError(MarketEvaluationError):
    """Raised when the configured sharp reference has no eligible quote."""


def evaluate_market(
    match: Match,
    snapshots: tuple[OddsSnapshot, ...],
    *,
    decision_at: datetime,
    settings: AppSettings,
    calculated_at: datetime | None = None,
) -> MarketEvaluationResult:
    """Evaluate every eligible offer using point-in-time peer consensus.

    Quotes observed after ``decision_at`` remain strictly prohibited. Quote age
    is intentionally diagnostic-only and never suppresses an older quote.
    """

    _require_utc(decision_at, "decision_at")
    calculation_time = calculated_at or datetime.now(UTC)
    _require_utc(calculation_time, "calculated_at")
    if decision_at >= match.scheduled_start:
        raise MarketEvaluationError(
            "decision_at must be earlier than the match scheduled start"
        )

    selected, exclusions = _select_latest_eligible_snapshots(
        match,
        snapshots,
        decision_at=decision_at,
    )
    if len(selected) < settings.collection.minimum_bookmakers:
        raise InsufficientBookmakersError(
            eligible_bookmakers=len(selected),
            required_bookmakers=settings.collection.minimum_bookmakers,
            exclusions=exclusions,
        )

    markets = tuple(
        power_devig(snapshot, calculated_at=calculation_time) for snapshot in selected
    )
    if (
        settings.pricing.consensus_method == "pinnacle"
        and not any(market.bookmaker_id == "pinnacle" for market in markets)
    ):
        raise MissingSharpBookmakerError(
            "Pinnacle has no eligible point-in-time h2h quote for this market"
        )
    evaluations = tuple(
        _evaluate_offer(
            market,
            player_id=price.player_id,
            offered_odds=price.decimal_odds,
            markets=markets,
            decision_at=decision_at,
            calculated_at=calculation_time,
            settings=settings,
        )
        for market in markets
        if not (
            settings.pricing.consensus_method == "pinnacle"
            and market.bookmaker_id == "pinnacle"
        )
        for price in market.prices
    )
    return MarketEvaluationResult(
        match_id=match.match_id,
        decision_at=decision_at,
        calculated_at=calculation_time,
        eligible_bookmaker_count=len(markets),
        evaluations=evaluations,
        exclusions=exclusions,
    )


def _select_latest_eligible_snapshots(
    match: Match,
    snapshots: tuple[OddsSnapshot, ...],
    *,
    decision_at: datetime,
) -> tuple[tuple[OddsSnapshot, ...], tuple[SnapshotExclusion, ...]]:
    eligible_by_bookmaker: defaultdict[str, list[OddsSnapshot]] = defaultdict(list)
    exclusions: list[SnapshotExclusion] = []
    participants = set(match.player_ids)

    for snapshot in sorted(
        snapshots,
        key=lambda item: (
            item.bookmaker_id,
            item.observed_at,
            item.snapshot_id,
        ),
    ):
        reason: ExclusionReason | None = None
        detail = ""
        if snapshot.match_id != match.match_id:
            reason = ExclusionReason.WRONG_MATCH
            detail = "snapshot match_id does not match the evaluated match"
        elif {price.player_id for price in snapshot.prices} != participants:
            reason = ExclusionReason.PARTICIPANT_MISMATCH
            detail = "snapshot outcomes do not match the evaluated participants"
        elif snapshot.observed_at > decision_at:
            reason = ExclusionReason.FUTURE_QUOTE
            detail = "snapshot was observed after decision_at"
        if reason is not None:
            exclusions.append(
                SnapshotExclusion(
                    snapshot_id=snapshot.snapshot_id,
                    bookmaker_id=snapshot.bookmaker_id,
                    reason=reason,
                    detail=detail,
                )
            )
            continue
        eligible_by_bookmaker[snapshot.bookmaker_id].append(snapshot)

    selected: list[OddsSnapshot] = []
    for bookmaker_id, bookmaker_snapshots in sorted(eligible_by_bookmaker.items()):
        latest_at = max(snapshot.observed_at for snapshot in bookmaker_snapshots)
        latest = [
            snapshot
            for snapshot in bookmaker_snapshots
            if snapshot.observed_at == latest_at
        ]
        _reject_conflicting_latest_quotes(bookmaker_id, latest_at, latest)
        chosen = min(latest, key=lambda snapshot: snapshot.snapshot_id)
        selected.append(chosen)
        for snapshot in bookmaker_snapshots:
            if snapshot is chosen:
                continue
            reason = (
                ExclusionReason.DUPLICATE_QUOTE
                if snapshot.observed_at == latest_at
                else ExclusionReason.SUPERSEDED_QUOTE
            )
            exclusions.append(
                SnapshotExclusion(
                    snapshot_id=snapshot.snapshot_id,
                    bookmaker_id=snapshot.bookmaker_id,
                    reason=reason,
                    detail=(
                        "an equivalent quote at the same timestamp was selected"
                        if reason is ExclusionReason.DUPLICATE_QUOTE
                        else "a newer eligible quote was selected"
                    ),
                )
            )

    return (
        tuple(sorted(selected, key=lambda snapshot: snapshot.bookmaker_id)),
        tuple(
            sorted(
                exclusions,
                key=lambda item: (
                    item.bookmaker_id,
                    item.snapshot_id,
                    item.reason,
                ),
            )
        ),
    )


def _reject_conflicting_latest_quotes(
    bookmaker_id: str,
    observed_at: datetime,
    snapshots: list[OddsSnapshot],
) -> None:
    prices = {
        tuple(
            sorted(
                (
                    price.player_id,
                    price.decimal_odds,
                )
                for price in snapshot.prices
            )
        )
        for snapshot in snapshots
    }
    if len(prices) > 1:
        snapshot_ids = sorted(snapshot.snapshot_id for snapshot in snapshots)
        raise ConflictingSnapshotsError(
            f"bookmaker {bookmaker_id!r} has conflicting snapshots at "
            f"{observed_at.isoformat()}: {snapshot_ids!r}"
        )


def _evaluate_offer(
    market: DeViggedMarket,
    *,
    player_id: PlayerId,
    offered_odds: Decimal,
    markets: tuple[DeViggedMarket, ...],
    decision_at: datetime,
    calculated_at: datetime,
    settings: AppSettings,
) -> OfferEvaluation:
    if settings.pricing.consensus_method == "pinnacle":
        consensus = sharp_bookmaker_consensus(
            markets,
            target_bookmaker_id=market.bookmaker_id,
            player_id=player_id,
            sharp_bookmaker_id="pinnacle",
            calculated_at=calculated_at,
        )
    else:
        consensus = leave_one_out_median_consensus(
            markets,
            target_bookmaker_id=market.bookmaker_id,
            player_id=player_id,
            calculated_at=calculated_at,
        )
    edge = expected_value(offered_odds, consensus.probability)
    suspicious_ids = _suspicious_overround_snapshot_ids(markets, settings)
    flags = _quality_flags(
        edge=edge,
        consensus=consensus,
        suspicious_overround_snapshot_ids=suspicious_ids,
        settings=settings,
    )
    return OfferEvaluation(
        match_id=market.match_id,
        bookmaker_id=market.bookmaker_id,
        snapshot_id=market.snapshot_id,
        player_id=player_id,
        offered_odds=offered_odds,
        target_overround=market.overround,
        consensus_probability=consensus.probability,
        consensus_fair_odds=consensus.fair_odds,
        expected_value=edge,
        is_candidate=edge >= settings.signals.minimum_expected_value,
        peer_count=consensus.peer_count,
        peer_snapshot_ids=consensus.peer_snapshot_ids,
        minimum_peer_probability=consensus.minimum_peer_probability,
        maximum_peer_probability=consensus.maximum_peer_probability,
        peer_probability_range=consensus.peer_probability_range,
        margin_method=market.method,
        consensus_method=consensus.method,
        decision_at=decision_at,
        calculated_at=calculated_at,
        quality_flags=flags,
        suspicious_overround_snapshot_ids=suspicious_ids,
    )


def _suspicious_overround_snapshot_ids(
    markets: tuple[DeViggedMarket, ...],
    settings: AppSettings,
) -> tuple[str, ...]:
    return tuple(
        market.snapshot_id
        for market in markets
        if (
            market.overround < settings.quality.minimum_normal_overround
            or market.overround > settings.quality.maximum_normal_overround
        )
    )


def _quality_flags(
    *,
    edge: Decimal,
    consensus: ConsensusEstimate,
    suspicious_overround_snapshot_ids: tuple[str, ...],
    settings: AppSettings,
) -> tuple[QualityFlag, ...]:
    flags: list[QualityFlag] = []
    if edge >= settings.quality.review_expected_value:
        flags.append(QualityFlag.LARGE_EDGE)
    if suspicious_overround_snapshot_ids:
        flags.append(QualityFlag.SUSPICIOUS_OVERROUND)
    if (
        consensus.peer_probability_range
        > settings.quality.maximum_peer_probability_range
    ):
        flags.append(QualityFlag.WIDE_PEER_DISPERSION)
    return tuple(flags)


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketEvaluationError(f"{field_name} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise MarketEvaluationError(f"{field_name} must be in UTC")
