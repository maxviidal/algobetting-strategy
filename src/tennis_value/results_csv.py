"""Conservative matching of local ATP/WTA result CSVs to OddsPapi fixtures."""

import csv
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
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
    detail: str = ""


@dataclass(frozen=True, slots=True)
class IdentityReview:
    source: str
    record_id: str
    tour: str
    canonical_name: str
    original_name: str
    opponent_name: str
    match_date: date
    tournament_name: str
    result_status: str


@dataclass(frozen=True, slots=True)
class _NameParts:
    surname: str
    given: str
    abbreviated: bool


@dataclass(frozen=True, slots=True)
class ResultMatching:
    results: dict[str, MatchedResult]
    quarantines: tuple[ResultQuarantine, ...]
    source_checksums: dict[str, str]
    identity_reviews: tuple[IdentityReview, ...]


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
    indexed: dict[tuple[str, frozenset[tuple[str, str]]], list[ExternalResult]] = {}
    for tour, values in rows.items():
        for value in values:
            key = (
                tour,
                frozenset(
                    (
                        _abbreviated_name_key(value.player_one_name),
                        _abbreviated_name_key(value.player_two_name),
                    )
                ),
            )
            indexed.setdefault(key, []).append(value)
    unavailable_index: dict[
        tuple[str, frozenset[tuple[str, str]]], list[ExternalResult]
    ] = {}
    for tour, values in unavailable_rows.items():
        for value in values:
            key = (
                tour,
                frozenset(
                    (
                        _abbreviated_name_key(value.player_one_name),
                        _abbreviated_name_key(value.player_two_name),
                    )
                ),
            )
            unavailable_index.setdefault(key, []).append(value)
    matched: dict[str, MatchedResult] = {}
    quarantines: list[ResultQuarantine] = []
    for fixture in fixtures:
        player_one_key = _abbreviated_name_key(fixture.player_one_name)
        player_two_key = _abbreviated_name_key(fixture.player_two_name)
        if player_one_key == player_two_key and _two_letter_name_key(
            fixture.player_one_name
        ) == _two_letter_name_key(fixture.player_two_name):
            quarantines.append(
                ResultQuarantine(
                    fixture.fixture_id,
                    fixture.tour,
                    "ambiguous_fixture_identity",
                    _fixture_detail(fixture),
                )
            )
            continue
        key = (
            fixture.tour,
            frozenset((player_one_key, player_two_key)),
        )
        candidates = [
            value
            for value in indexed.get(key, [])
            if abs((value.match_date - fixture.scheduled_start.date()).days) <= 1
            and _tournament_matches(fixture.tournament_key, value.tournament_name)
            and _matchup_names_match(fixture, value)
        ]
        if len(candidates) != 1:
            unavailable = [
                value
                for value in unavailable_index.get(key, [])
                if abs((value.match_date - fixture.scheduled_start.date()).days) <= 1
                and _tournament_matches(fixture.tournament_key, value.tournament_name)
                and _matchup_names_match(fixture, value)
            ]
            if candidates:
                reason = "ambiguous_result_match"
            elif unavailable:
                reason = "non_completed_result"
            else:
                reason = "result_not_found"
            quarantines.append(
                ResultQuarantine(
                    fixture.fixture_id,
                    fixture.tour,
                    reason,
                    _candidate_detail(fixture, candidates or unavailable),
                )
            )
            continue
        result = candidates[0]
        winner_name = (
            result.player_one_name
            if result.winner_code == 1
            else result.player_two_name
        )
        winner_matches_one = _names_match(fixture.player_one_name, winner_name)
        winner_matches_two = _names_match(fixture.player_two_name, winner_name)
        if winner_matches_one and not winner_matches_two:
            winner_id = fixture.player_one_id
        elif winner_matches_two and not winner_matches_one:
            winner_id = fixture.player_two_id
        else:
            quarantines.append(
                ResultQuarantine(
                    fixture.fixture_id,
                    fixture.tour,
                    "winner_identity_mismatch",
                    _candidate_detail(fixture, candidates),
                )
            )
            continue
        matched[fixture.fixture_id] = MatchedResult(
            fixture.fixture_id,
            winner_id,
            result.source_row,
            checksums[fixture.tour],
        )
    reviews = _identity_reviews(fixtures, rows, unavailable_rows)
    return ResultMatching(matched, tuple(quarantines), checksums, reviews)


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
        "date": (
            "date_timestamp",
            "date_human",
            "date",
            "match_date",
            "start_date",
            "start_time",
            "event_date",
        ),
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


def _abbreviated_name_key(value: str) -> tuple[str, str]:
    """Return surname plus given-name initial without replacing the source name."""

    parts = _name_parts(value)
    return parts.surname, parts.given[0]


def _two_letter_name_key(value: str) -> tuple[str, str]:
    parts = _name_parts(value)
    return parts.surname, _compact(parts.given)[:2]


def _name_parts(value: str) -> _NameParts:
    if "," in value:
        surname_raw, given_raw = value.split(",", maxsplit=1)
        surname = _normalize_name(surname_raw)
        given = _normalize_name(given_raw)
        if surname and given:
            return _NameParts(surname, given, False)
    raw_tokens = value.strip().split()
    if len(raw_tokens) >= 2 and raw_tokens[-1].endswith("."):
        surname = _normalize_name(" ".join(raw_tokens[:-1]))
        given_abbreviation = _normalize_name(raw_tokens[-1])
        if surname and given_abbreviation:
            return _NameParts(surname, _compact(given_abbreviation), True)
    tokens = _normalize_name(value).split()
    if len(tokens) < 2:
        raise ValueError(f"player name cannot be abbreviated safely: {value!r}")
    if len(tokens[-1]) == 1:
        return _NameParts(" ".join(tokens[:-1]), tokens[-1], True)
    return _NameParts(tokens[-1], " ".join(tokens[:-1]), False)


def _compact(value: str) -> str:
    return value.replace(" ", "")


def _names_match(fixture_name: str, result_name: str) -> bool:
    fixture = _name_parts(fixture_name)
    result = _name_parts(result_name)
    if fixture.surname != result.surname:
        return False
    fixture_given = _compact(fixture.given)
    result_given = _compact(result.given)
    if fixture.abbreviated and result.abbreviated:
        return fixture_given.startswith(result_given) or result_given.startswith(
            fixture_given
        )
    if fixture.abbreviated:
        return _abbreviation_matches(result.given, fixture_given)
    if result.abbreviated:
        return _abbreviation_matches(fixture.given, result_given)
    return fixture_given == result_given


def _abbreviation_matches(full_given: str, abbreviation: str) -> bool:
    compact_full = _compact(full_given)
    initials = "".join(token[0] for token in full_given.split())
    return compact_full.startswith(abbreviation) or initials.startswith(abbreviation)


def _matchup_names_match(fixture: ResearchFixture, result: ExternalResult) -> bool:
    direct = _names_match(
        fixture.player_one_name, result.player_one_name
    ) and _names_match(fixture.player_two_name, result.player_two_name)
    reversed_order = _names_match(
        fixture.player_one_name, result.player_two_name
    ) and _names_match(fixture.player_two_name, result.player_one_name)
    return direct or reversed_order


def _display_name_key(value: tuple[str, str]) -> str:
    surname, initial = value
    return f"{surname} {initial}."


def _fixture_detail(fixture: ResearchFixture) -> str:
    return (
        f"OddsPapi fixture {fixture.fixture_id}: {fixture.player_one_name} vs "
        f"{fixture.player_two_name}; {fixture.scheduled_start.date().isoformat()}; "
        f"{fixture.tournament_name}"
    )


def _candidate_detail(
    fixture: ResearchFixture, candidates: Sequence[ExternalResult]
) -> str:
    fixture_value = _fixture_detail(fixture)
    if not candidates:
        return fixture_value
    result_values = " | ".join(
        f"TennisData row {value.source_row}: {value.player_one_name} vs "
        f"{value.player_two_name}; {value.match_date.isoformat()}; "
        f"{value.tournament_name}"
        for value in candidates
    )
    return f"{fixture_value} || candidates: {result_values}"


def _identity_reviews(
    fixtures: tuple[ResearchFixture, ...],
    rows: dict[str, tuple[ExternalResult, ...]],
    unavailable_rows: dict[str, tuple[ExternalResult, ...]],
) -> tuple[IdentityReview, ...]:
    collision_bases = _oddspapi_collision_bases(fixtures)
    observations: list[tuple[IdentityReview, tuple[str, str]]] = []
    for fixture in fixtures:
        for player_name, opponent_name in (
            (fixture.player_one_name, fixture.player_two_name),
            (fixture.player_two_name, fixture.player_one_name),
        ):
            base_key = _abbreviated_name_key(player_name)
            display_key = (
                _two_letter_name_key(player_name)
                if (fixture.tour, *base_key) in collision_bases
                else base_key
            )
            observations.append(
                (
                    IdentityReview(
                        source="oddspapi",
                        record_id=fixture.fixture_id,
                        tour=fixture.tour,
                        canonical_name=_display_name_key(display_key),
                        original_name=player_name,
                        opponent_name=opponent_name,
                        match_date=fixture.scheduled_start.date(),
                        tournament_name=fixture.tournament_name,
                        result_status="fixture",
                    ),
                    base_key,
                )
            )
    for status, values_by_tour in (
        ("completed", rows),
        ("non_completed", unavailable_rows),
    ):
        for tour, result_rows in values_by_tour.items():
            for result_row in result_rows:
                for player_name, opponent_name in (
                    (result_row.player_one_name, result_row.player_two_name),
                    (result_row.player_two_name, result_row.player_one_name),
                ):
                    base_key = _abbreviated_name_key(player_name)
                    display_key = (
                        _two_letter_name_key(player_name)
                        if (tour, *base_key) in collision_bases
                        else base_key
                    )
                    observations.append(
                        (
                            IdentityReview(
                                source="tennisdata",
                                record_id=f"row:{result_row.source_row}",
                                tour=tour,
                                canonical_name=_display_name_key(display_key),
                                original_name=player_name,
                                opponent_name=opponent_name,
                                match_date=result_row.match_date,
                                tournament_name=result_row.tournament_name,
                                result_status=status,
                            ),
                            base_key,
                        )
                    )
    counts: dict[tuple[str, str, str, str], int] = {}
    for review, base_key in observations:
        review_key = (review.source, review.tour, *base_key)
        counts[review_key] = counts.get(review_key, 0) + 1
    return tuple(
        sorted(
            (
                review
                for review, base_key in observations
                if counts[(review.source, review.tour, *base_key)] > 1
                or (review.tour, *base_key) in collision_bases
            ),
            key=lambda value: (
                value.source,
                value.tour,
                value.canonical_name,
                value.match_date,
                value.record_id,
                value.original_name,
            ),
        )
    )


def _oddspapi_collision_bases(
    fixtures: tuple[ResearchFixture, ...],
) -> set[tuple[str, str, str]]:
    full_given_names: dict[tuple[str, str, str], set[str]] = {}
    for fixture in fixtures:
        for player_name in (fixture.player_one_name, fixture.player_two_name):
            parts = _name_parts(player_name)
            base_key = (fixture.tour, parts.surname, parts.given[0])
            full_given_names.setdefault(base_key, set()).add(_compact(parts.given))
    return {base_key for base_key, names in full_given_names.items() if len(names) > 1}


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
        for pattern in ("%d %b %Y", "%d/%m/%Y", "%m/%d/%Y", "%Y%m%d"):
            try:
                return datetime.strptime(cleaned, pattern).date()
            except ValueError:
                continue
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", cleaned):
        try:
            return datetime.fromtimestamp(float(cleaned), tz=UTC).date()
        except (OSError, OverflowError, ValueError):
            pass
    raise ValueError("unsupported match date")
