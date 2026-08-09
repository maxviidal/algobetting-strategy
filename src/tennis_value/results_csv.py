"""Conservative matching of local ATP/WTA result CSVs to OddsPapi fixtures."""

import csv
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path

from tennis_value.oddspapi_research import ResearchFixture

_UNFINISHED_MARKERS = (
    "walkover",
    "retired",
    "retirement",
    "cancelled",
    "canceled",
    "unfinished",
    "abandoned",
    "w/o",
    " wo ",
    " ret ",
)


@dataclass(frozen=True, slots=True)
class ExternalResult:
    source_row: int
    tour: str
    match_date: date
    player_one_name: str
    player_two_name: str
    winner_code: int
    tournament_name: str


@dataclass(frozen=True, slots=True)
class MatchedResult:
    fixture_id: str
    winner_player_id: int
    source_row: int
    source_sha256: str


@dataclass(frozen=True, slots=True)
class ResultQuarantine:
    fixture_id: str
    tour: str
    reason: str


@dataclass(frozen=True, slots=True)
class ResultMatching:
    results: dict[str, MatchedResult]
    quarantines: tuple[ResultQuarantine, ...]
    source_checksums: dict[str, str]


def match_result_csvs(
    fixtures: tuple[ResearchFixture, ...],
    *,
    atp_path: Path,
    wta_path: Path,
) -> ResultMatching:
    """Match two local season files without guessing ambiguous identities."""

    paths = {"ATP": atp_path, "WTA": wta_path}
    checksums = {
        tour: sha256(path.read_bytes()).hexdigest() for tour, path in paths.items()
    }
    parsed = {tour: _read_results(path, tour=tour) for tour, path in paths.items()}
    rows = {tour: value[0] for tour, value in parsed.items()}
    unavailable_rows = {tour: value[1] for tour, value in parsed.items()}
    indexed: dict[tuple[str, frozenset[str]], list[ExternalResult]] = {}
    for tour, values in rows.items():
        for value in values:
            key = (
                tour,
                frozenset(
                    (
                        _normalize_name(value.player_one_name),
                        _normalize_name(value.player_two_name),
                    )
                ),
            )
            indexed.setdefault(key, []).append(value)
    unavailable_index: dict[tuple[str, frozenset[str]], list[ExternalResult]] = {}
    for tour, values in unavailable_rows.items():
        for value in values:
            key = (
                tour,
                frozenset(
                    (
                        _normalize_name(value.player_one_name),
                        _normalize_name(value.player_two_name),
                    )
                ),
            )
            unavailable_index.setdefault(key, []).append(value)
    matched: dict[str, MatchedResult] = {}
    quarantines: list[ResultQuarantine] = []
    for fixture in fixtures:
        key = (
            fixture.tour,
            frozenset(
                (
                    _normalize_name(fixture.player_one_name),
                    _normalize_name(fixture.player_two_name),
                )
            ),
        )
        candidates = [
            value
            for value in indexed.get(key, [])
            if abs((value.match_date - fixture.scheduled_start.date()).days) <= 1
            and _tournament_matches(fixture.tournament_key, value.tournament_name)
        ]
        if len(candidates) != 1:
            unavailable = [
                value
                for value in unavailable_index.get(key, [])
                if abs((value.match_date - fixture.scheduled_start.date()).days) <= 1
                and _tournament_matches(fixture.tournament_key, value.tournament_name)
            ]
            if candidates:
                reason = "ambiguous_result_match"
            elif unavailable:
                reason = "non_completed_result"
            else:
                reason = "result_not_found"
            quarantines.append(
                ResultQuarantine(fixture.fixture_id, fixture.tour, reason)
            )
            continue
        result = candidates[0]
        winner_name = (
            result.player_one_name
            if result.winner_code == 1
            else result.player_two_name
        )
        winner_id = (
            fixture.player_one_id
            if _normalize_name(winner_name) == _normalize_name(fixture.player_one_name)
            else fixture.player_two_id
        )
        matched[fixture.fixture_id] = MatchedResult(
            fixture.fixture_id,
            winner_id,
            result.source_row,
            checksums[fixture.tour],
        )
    return ResultMatching(matched, tuple(quarantines), checksums)


def _read_results(
    path: Path,
    *,
    tour: str,
) -> tuple[tuple[ExternalResult, ...], tuple[ExternalResult, ...]]:
    if not path.is_file():
        raise FileNotFoundError(f"result CSV does not exist: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"result CSV has no header: {path}")
        aliases = _resolve_columns(reader.fieldnames)
        results: list[ExternalResult] = []
        unavailable: list[ExternalResult] = []
        for source_row, row in enumerate(reader, start=2):
            combined = " ".join(str(value or "") for value in row.values()).casefold()
            winner_raw = str(row[aliases["winner_code"]] or "").strip()
            try:
                match_date = _parse_date(str(row[aliases["date"]] or ""))
            except ValueError:
                continue
            value = ExternalResult(
                source_row=source_row,
                tour=tour,
                match_date=match_date,
                player_one_name=str(row[aliases["home"]] or "").strip(),
                player_two_name=str(row[aliases["away"]] or "").strip(),
                winner_code=int(winner_raw) if winner_raw in {"1", "2"} else 0,
                tournament_name=(
                    str(row[aliases["tournament"]] or "").strip()
                    if aliases["tournament"] is not None
                    else ""
                ),
            )
            is_unfinished = any(
                marker in f" {combined} " for marker in _UNFINISHED_MARKERS
            )
            if is_unfinished or winner_raw not in {"1", "2"}:
                unavailable.append(value)
            else:
                results.append(value)
    return tuple(results), tuple(unavailable)


def _resolve_columns(fieldnames: Sequence[str]) -> dict[str, str | None]:
    normalized = {_column_key(value): value for value in fieldnames}
    candidates = {
        "home": ("home_name", "home_player_name", "home_player", "player_1", "player1"),
        "away": ("away_name", "away_player_name", "away_player", "player_2", "player2"),
        "winner_code": ("winner_code", "winner", "winner_side"),
        "date": ("date", "match_date", "start_date", "start_time", "event_date"),
        "tournament": ("tournament_name", "tournament", "event_name"),
    }
    result: dict[str, str | None] = {}
    for role, names in candidates.items():
        result[role] = next(
            (normalized[name] for name in names if name in normalized), None
        )
        if result[role] is None and role != "tournament":
            raise ValueError(f"result CSV is missing a supported {role} column")
    return result


def _column_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")


def _normalize_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_like = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_like.casefold()).split())


def _tournament_matches(tournament_key: str, result_name: str) -> bool:
    if not result_name.strip():
        return True
    tokens = tournament_key.split("_")[1:]
    normalized = _normalize_name(result_name)
    return all(token in normalized.split() for token in tokens)


def _parse_date(value: str) -> date:
    cleaned = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned).date()
    except ValueError:
        for pattern in ("%d/%m/%Y", "%m/%d/%Y", "%Y%m%d"):
            try:
                return datetime.strptime(cleaned, pattern).date()
            except ValueError:
                continue
    raise ValueError("unsupported match date")
