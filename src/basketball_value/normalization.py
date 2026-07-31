"""Provider normalization and conservative cross-provider game matching."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

from basketball_value.domain import (
    Game,
    GameResult,
    MoneylinePrice,
    MoneylineSnapshot,
    Team,
    stable_game_id,
)
from basketball_value.teams import resolve_provider_team, resolve_team
from betting_core import (
    QuoteRecord,
    SelectedMarket,
    select_latest_record,
    select_market_at,
)

_EXCHANGE_KEYS = frozenset({"betfair_ex_eu", "betfair_ex_uk", "matchbook"})


@dataclass(frozen=True, slots=True)
class ResultGame:
    game: Game
    result: GameResult


@dataclass(frozen=True, slots=True)
class NormalizedOdds:
    snapshots: tuple[MoneylineSnapshot, ...]
    excluded: tuple[str, ...]
    unmatched_event_ids: tuple[str, ...]
    matched_event_ids: tuple[str, ...]
    matched_game_ids: tuple[str, ...]


def normalize_balldontlie_games(payloads: tuple[object, ...]) -> tuple[ResultGame, ...]:
    """Normalize regular-season NBA games, scores, and postponement status."""

    normalized: list[ResultGame] = []
    seen: set[str] = set()
    for payload in payloads:
        for raw in _items(_dictionary(payload).get("data")):
            event_id = str(raw.get("id", ""))
            if not event_id or event_id in seen or raw.get("postseason") is True:
                continue
            home = _balldontlie_team(raw.get("home_team"))
            away = _balldontlie_team(raw.get("visitor_team"))
            scheduled = _utc(str(raw.get("datetime") or raw.get("date")))
            season_start = int(raw["season"])
            game_id = stable_game_id("balldontlie", event_id)
            game = Game(
                game_id=game_id,
                source="balldontlie",
                source_event_id=event_id,
                season=f"{season_start}-{str(season_start + 1)[-2:]}",
                home_team_id=home.team_id,
                away_team_id=away.team_id,
                scheduled_start=scheduled,
            )
            status = str(raw.get("status", "")).casefold()
            postponed = bool(raw.get("postponed")) or "postpon" in status
            final = status == "final" and not postponed
            result = GameResult(
                game_id=game_id,
                home_score=_optional_int(raw.get("home_team_score")),
                away_score=_optional_int(raw.get("visitor_team_score")),
                final=final,
                postponed=postponed,
            )
            normalized.append(ResultGame(game, result))
            seen.add(event_id)
    return tuple(sorted(normalized, key=lambda item: item.game.scheduled_start))


def match_odds_event(
    event: dict[str, Any],
    games: tuple[Game, ...],
    *,
    tolerance: timedelta = timedelta(hours=12),
) -> Game:
    """Require exactly one canonical home/away/time result match."""

    home = resolve_team(str(event["home_team"])).team_id
    away = resolve_team(str(event["away_team"])).team_id
    start = _utc(str(event["commence_time"]))
    candidates = tuple(
        game
        for game in games
        if game.home_team_id == home
        and game.away_team_id == away
        and abs(game.scheduled_start - start) <= tolerance
    )
    if len(candidates) != 1:
        raise ValueError(
            f"odds event {event.get('id')!r} matched {len(candidates)} results"
        )
    return candidates[0]


def normalize_odds_snapshot(
    payload: object,
    games: tuple[Game, ...],
    *,
    decision_at: datetime,
    maximum_age: timedelta,
) -> NormalizedOdds:
    """Normalize complete, active, fixed-odds h2h books at one decision time."""

    root = _dictionary(payload)
    events = _items(root.get("data"))
    snapshots: list[MoneylineSnapshot] = []
    excluded: list[str] = []
    unmatched: list[str] = []
    matched: list[str] = []
    matched_games: list[str] = []
    for event in events:
        event_id = str(event.get("id", ""))
        try:
            game = match_odds_event(event, games)
        except (KeyError, ValueError):
            unmatched.append(event_id)
            continue
        matched.append(event_id)
        matched_games.append(game.game_id)
        for book in _items(event.get("bookmakers")):
            bookmaker_id = str(book.get("key", ""))
            markets = _items(book.get("markets"))
            if (
                not bookmaker_id
                or bookmaker_id in _EXCHANGE_KEYS
                or "exchange" in bookmaker_id
                or any(market.get("key") == "h2h_lay" for market in markets)
            ):
                excluded.append(f"{event_id}:{bookmaker_id}:exchange")
                continue
            market = next(
                (value for value in markets if value.get("key") == "h2h"), None
            )
            selected, exclusion_reason = _selected_market(
                market,
                game,
                decision_at=decision_at,
                maximum_age=maximum_age,
            )
            if selected is None:
                excluded.append(
                    f"{event_id}:{bookmaker_id}:{exclusion_reason or 'ineligible'}"
                )
                continue
            snapshot_id = sha256(
                (
                    f"{event_id}|{bookmaker_id}|{selected.observed_at.isoformat()}|"
                    f"{selected.price_for(game.home_team_id)}|"
                    f"{selected.price_for(game.away_team_id)}"
                ).encode()
            ).hexdigest()
            snapshots.append(
                MoneylineSnapshot(
                    snapshot_id=snapshot_id,
                    game_id=game.game_id,
                    bookmaker_id=bookmaker_id,
                    observed_at=selected.observed_at,
                    prices=(
                        MoneylinePrice(
                            game.home_team_id,
                            selected.price_for(game.home_team_id),
                        ),
                        MoneylinePrice(
                            game.away_team_id,
                            selected.price_for(game.away_team_id),
                        ),
                    ),
                    source="the_odds_api",
                    source_event_id=event_id,
                )
            )
    return NormalizedOdds(
        tuple(snapshots),
        tuple(excluded),
        tuple(unmatched),
        tuple(matched),
        tuple(matched_games),
    )


def _selected_market(
    market: dict[str, Any] | None,
    game: Game,
    *,
    decision_at: datetime,
    maximum_age: timedelta,
) -> tuple[SelectedMarket | None, str | None]:
    if market is None:
        return None, "incomplete"
    market_updated = market.get("last_update")
    records: list[QuoteRecord] = []
    for outcome in _items(market.get("outcomes")):
        try:
            team_id = resolve_team(str(outcome["name"])).team_id
        except (KeyError, ValueError):
            continue
        if team_id not in {game.home_team_id, game.away_team_id}:
            continue
        updated = outcome.get("last_update") or market_updated
        if not updated:
            continue
        try:
            price = Decimal(str(outcome.get("price")))
        except InvalidOperation:
            price = None
        records.append(
            QuoteRecord(
                outcome_id=team_id,
                decimal_odds=price,
                observed_at=_utc(str(updated)),
                active=outcome.get("active", True) is True,
            )
        )
    latest = tuple(
        select_latest_record(
            tuple(record for record in records if record.outcome_id == outcome_id),
            decision_at=decision_at,
        )
        for outcome_id in (game.home_team_id, game.away_team_id)
    )
    if any(record is None for record in latest):
        reason = (
            "post_decision"
            if any(record.observed_at > decision_at for record in records)
            else "incomplete"
        )
        return None, reason
    if any(record is not None and not record.active for record in latest):
        return None, "suspended"
    if any(
        record is not None
        and (
            record.decimal_odds is None
            or not record.decimal_odds.is_finite()
            or record.decimal_odds <= 1
        )
        for record in latest
    ):
        return None, "incomplete"
    latest_observed = max(
        record.observed_at for record in latest if record is not None
    )
    if decision_at - latest_observed > maximum_age:
        return None, "stale"
    selected = select_market_at(
        tuple(records),
        outcome_ids=(game.home_team_id, game.away_team_id),
        decision_at=decision_at,
        maximum_age=maximum_age,
    )
    return selected, None


def _team_name(value: object) -> str:
    data = _dictionary(value)
    for key in ("full_name", "name", "abbreviation"):
        candidate = data.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    raise ValueError("team has no resolvable name")


def _balldontlie_team(value: object) -> Team:
    data = _dictionary(value)
    by_name = resolve_team(_team_name(value))
    provider_id = data.get("id")
    if provider_id is None:
        return by_name
    by_id = resolve_provider_team("balldontlie", str(provider_id))
    if by_id.team_id != by_name.team_id:
        raise ValueError("BALLDONTLIE team ID and name conflict")
    return by_id


def _dictionary(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if not isinstance(value, int | str):
        raise ValueError("score must be an integer")
    return int(value)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)
