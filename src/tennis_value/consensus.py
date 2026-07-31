"""Robust consensus calculations over de-vigged bookmaker markets."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tennis_value.data.domain import PlayerId
from tennis_value.pricing import DeViggedMarket, fair_odds

MEDIAN_CONSENSUS_METHOD = "median"


@dataclass(frozen=True, slots=True)
class ConsensusEstimate:
    """A leave-one-bookmaker-out probability estimate with peer provenance."""

    target_bookmaker_id: str
    player_id: PlayerId
    probability: Decimal
    fair_odds: Decimal
    peer_count: int
    peer_snapshot_ids: tuple[str, ...]
    minimum_peer_probability: Decimal
    maximum_peer_probability: Decimal
    peer_probability_range: Decimal
    method: str
    calculated_at: datetime


def median_probability(probabilities: tuple[Decimal, ...]) -> Decimal:
    """Return a deterministic median, averaging the middle pair when even."""

    if not probabilities:
        raise ValueError("at least one peer probability is required")
    ordered = sorted(probabilities)
    for probability in ordered:
        if not probability.is_finite() or probability < 0 or probability > 1:
            raise ValueError("peer probabilities must be finite and between 0 and 1")
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal(2)


def leave_one_out_median_consensus(
    markets: tuple[DeViggedMarket, ...],
    *,
    target_bookmaker_id: str,
    player_id: PlayerId,
    calculated_at: datetime,
) -> ConsensusEstimate:
    """Build a median consensus excluding the bookmaker being evaluated."""

    peers = tuple(
        sorted(
            (
                market
                for market in markets
                if market.bookmaker_id != target_bookmaker_id
            ),
            key=lambda market: (market.bookmaker_id, market.snapshot_id),
        )
    )
    if not peers:
        raise ValueError("leave-one-out consensus requires at least one peer market")
    probabilities = tuple(market.probability_for(player_id) for market in peers)
    probability = median_probability(probabilities)
    minimum = min(probabilities)
    maximum = max(probabilities)
    return ConsensusEstimate(
        target_bookmaker_id=target_bookmaker_id,
        player_id=player_id,
        probability=probability,
        fair_odds=fair_odds(probability),
        peer_count=len(peers),
        peer_snapshot_ids=tuple(market.snapshot_id for market in peers),
        minimum_peer_probability=minimum,
        maximum_peer_probability=maximum,
        peer_probability_range=maximum - minimum,
        method=MEDIAN_CONSENSUS_METHOD,
        calculated_at=calculated_at,
    )
