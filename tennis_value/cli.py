"""Command-line entry points for collection, normalization, and evaluation."""

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from tennis_value.config import (
    ConfigurationError,
    get_odds_api_key,
    get_odds_papi_base_url,
    get_odds_papi_key,
    load_env_file,
    load_settings,
)
from tennis_value.ingestion import IngestionError
from tennis_value.odds_api import OddsApiClient, OddsApiError, OddsApiQuota
from tennis_value.odds_papi import OddsPapiClient, OddsPapiError
from tennis_value.oddspapi_backtest import run_atp_wimbledon_backtest
from tennis_value.storage import SqliteOddsRepository, SqlitePlayerRegistry
from tennis_value.workflow import (
    PendingPlayersError,
    WorkflowError,
    approve_pending_players,
    evaluate_stored_response,
    normalize_stored_response,
    restore_ingested_response,
    scan_pending_players,
    select_raw_response,
)

_DEFAULT_DATABASE = "data/tennis_value.sqlite3"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the tennis-value command line interface."""

    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command in {"sports", "collect"}:
            client = _api_client(arguments)
            if arguments.command == "sports":
                return _list_tennis_sports(client)
            return _collect_current_odds(
                client,
                sport=str(arguments.sport),
                regions=_parse_regions(str(arguments.regions)),
                database=Path(arguments.database),
            )
        if arguments.command == "players":
            if arguments.players_command == "pending":
                return _list_pending_players(
                    database=_existing_database(arguments.database),
                    response_id=str(arguments.response_id),
                )
            return _approve_players(
                database=_existing_database(arguments.database),
                response_id=str(arguments.response_id),
                approve_all=bool(arguments.approve_all),
                names=tuple(arguments.names or ()),
            )
        if arguments.command == "normalize":
            return _normalize(
                database=_existing_database(arguments.database),
                response_id=str(arguments.response_id),
            )
        if arguments.command == "evaluate":
            return _evaluate(
                database=_existing_database(arguments.database),
                response_id=str(arguments.response_id),
                config_path=Path(arguments.config),
                decision_at=_optional_datetime(arguments.decision_at),
            )
        if arguments.command == "backtest-atp-wimbledon":
            return _backtest_atp_wimbledon(
                arguments,
                config_path=Path(arguments.config),
                cache_directory=Path(arguments.cache_directory),
            )
    except PendingPlayersError as error:
        print(f"error: {error}", file=sys.stderr)
        print(
            "Run `python -m tennis_value players pending`, then explicitly "
            "approve the names.",
            file=sys.stderr,
        )
        return 2
    except (
        ConfigurationError,
        IngestionError,
        OddsApiError,
        OddsPapiError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        ValueError,
        KeyError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    parser.error("a command is required")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tennis-value",
        description="Collect and evaluate point-in-time tennis odds.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="environment file containing provider API keys (default: .env)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "sports",
        help="list active tennis sport keys without using quota",
    )

    collect = commands.add_parser(
        "collect",
        help="fetch and preserve current h2h odds",
    )
    collect.add_argument(
        "--sport",
        required=True,
        help="active tournament key returned by the sports command",
    )
    collect.add_argument(
        "--regions",
        default="uk",
        help="comma-separated bookmaker regions (default: uk)",
    )
    _add_database_argument(collect)

    players = commands.add_parser(
        "players",
        help="review and approve provider player identities",
    )
    player_commands = players.add_subparsers(
        dest="players_command",
        required=True,
    )
    pending = player_commands.add_parser(
        "pending",
        help="list names in a stored response that require approval",
    )
    _add_database_argument(pending)
    _add_response_argument(pending)

    approve = player_commands.add_parser(
        "approve",
        help="create stable identities for explicitly approved names",
    )
    _add_database_argument(approve)
    _add_response_argument(approve)
    approval_scope = approve.add_mutually_exclusive_group(required=True)
    approval_scope.add_argument(
        "--all",
        dest="approve_all",
        action="store_true",
        help="explicitly approve every unknown name in the response",
    )
    approval_scope.add_argument(
        "--name",
        dest="names",
        action="append",
        help="approve one exact pending name; repeat for multiple names",
    )

    normalize = commands.add_parser(
        "normalize",
        help="normalize an approved raw response into matches and snapshots",
    )
    _add_database_argument(normalize)
    _add_response_argument(normalize)

    evaluate = commands.add_parser(
        "evaluate",
        help="run the consensus and EV model over normalized stored odds",
    )
    _add_database_argument(evaluate)
    _add_response_argument(evaluate)
    evaluate.add_argument(
        "--config",
        default="configs/research.toml",
        help="typed model configuration path",
    )
    evaluate.add_argument(
        "--decision-at",
        help=(
            "optional ISO 8601 UTC decision time; defaults to the response "
            "collection time"
        ),
    )
    backtest = commands.add_parser(
        "backtest-atp-wimbledon",
        help="run the OddsPapi ATP Wimbledon 2026 60-minute backtest",
    )
    backtest.add_argument(
        "--config",
        default="configs/research.toml",
        help="typed model configuration path",
    )
    backtest.add_argument(
        "--cache-directory",
        default="data/oddspapi/wimbledon_atp_2026",
        help="raw response cache for safe resume",
    )
    return parser


def _add_database_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database",
        default=_DEFAULT_DATABASE,
        help=f"SQLite database path (default: {_DEFAULT_DATABASE})",
    )


def _add_response_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--response-id",
        default="latest",
        help="stored raw response ID or 'latest' (default: latest)",
    )


def _api_client(arguments: argparse.Namespace) -> OddsApiClient:
    load_env_file(Path(arguments.env_file))
    return OddsApiClient(
        get_odds_api_key(),
        timeout_seconds=float(arguments.timeout),
    )


def _odds_papi_client(arguments: argparse.Namespace) -> OddsPapiClient:
    load_env_file(Path(arguments.env_file))
    return OddsPapiClient(
        get_odds_papi_key(),
        base_url=get_odds_papi_base_url(),
        timeout_seconds=float(arguments.timeout),
    )


def _list_tennis_sports(client: OddsApiClient) -> int:
    sports = tuple(
        sport
        for sport in client.list_sports()
        if sport.active
        and (
            "tennis" in sport.group.casefold()
            or "tennis" in sport.key.casefold()
        )
    )
    if not sports:
        print("No active tennis tournaments were returned.")
        return 0
    for sport in sports:
        print(f"{sport.key}\t{sport.title}")
    return 0


def _collect_current_odds(
    client: OddsApiClient,
    *,
    sport: str,
    regions: tuple[str, ...],
    database: Path,
) -> int:
    result = client.fetch_current_odds(sport, regions=regions, market="h2h")
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        repository = SqliteOddsRepository(connection)
        response_id = repository.save_raw_response(result.response)

    print(f"Saved raw response: {response_id}")
    print(f"Database: {database.resolve()}")
    print(f"Events returned: {len(result.response.events)}")
    _print_quota(result.quota)
    print(
        "Raw collection is complete. Player identities must be approved "
        "before normalization."
    )
    return 0


def _list_pending_players(*, database: Path, response_id: str) -> int:
    with sqlite3.connect(database) as connection:
        repository = SqliteOddsRepository(connection)
        registry = SqlitePlayerRegistry(connection)
        stored = select_raw_response(repository, response_id)
        pending = scan_pending_players(
            registry,
            restore_ingested_response(stored),
        )
    if not pending:
        print("No player names are pending approval.")
        return 0
    print(f"Pending player names: {len(pending)}")
    for item in pending:
        print(f"{item.raw_name}\t{item.reason}")
    return 0


def _approve_players(
    *,
    database: Path,
    response_id: str,
    approve_all: bool,
    names: tuple[str, ...],
) -> int:
    with sqlite3.connect(database) as connection:
        repository = SqliteOddsRepository(connection)
        registry = SqlitePlayerRegistry(connection)
        stored = select_raw_response(repository, response_id)
        pending = scan_pending_players(
            registry,
            restore_ingested_response(stored),
        )
        selected_names = (
            tuple(item.raw_name for item in pending)
            if approve_all
            else names
        )
        approved = approve_pending_players(
            registry,
            pending,
            names=selected_names,
        )
    if not approved:
        print("No player identities required approval.")
        return 0
    for player in approved:
        print(f"Approved player {player.player_id}: {player.display_name}")
    print(f"Approved player identities: {len(approved)}")
    return 0


def _normalize(*, database: Path, response_id: str) -> int:
    with sqlite3.connect(database) as connection:
        summary = normalize_stored_response(
            connection,
            response_id=response_id,
        )
    print(f"Normalized response: {summary.response_id}")
    print(f"Matches saved: {summary.match_count}")
    print(f"Bookmaker snapshots saved: {summary.snapshot_count}")
    return 0


def _evaluate(
    *,
    database: Path,
    response_id: str,
    config_path: Path,
    decision_at: datetime | None,
) -> int:
    settings = load_settings(config_path)
    with sqlite3.connect(database) as connection:
        registry = SqlitePlayerRegistry(connection)
        batch = evaluate_stored_response(
            connection,
            settings=settings,
            response_id=response_id,
            decision_at=decision_at,
        )
        evaluated_offers = sum(
            len(item.result.evaluations) for item in batch.evaluated
        )
        candidates = [
            (item.match, evaluation)
            for item in batch.evaluated
            for evaluation in item.result.evaluations
            if evaluation.is_candidate
        ]
        candidates.sort(
            key=lambda item: (
                item[0].scheduled_start,
                item[0].match_id,
                -item[1].expected_value,
                item[1].bookmaker_id,
                item[1].player_id,
            )
        )
        print(f"Response: {batch.response_id}")
        print(f"Decision time: {batch.decision_at.isoformat()}")
        print(f"Matches evaluated: {len(batch.evaluated)}")
        print(f"Matches skipped: {len(batch.skipped)}")
        print(f"Offers evaluated: {evaluated_offers}")
        print(f"Candidates: {len(candidates)}")
        for match, evaluation in candidates:
            player = registry.get_player(evaluation.player_id)
            opponent_id = next(
                player_id
                for player_id in match.player_ids
                if player_id != evaluation.player_id
            )
            opponent = registry.get_player(opponent_id)
            flags = ",".join(flag.value for flag in evaluation.quality_flags)
            if not flags:
                flags = "none"
            print(
                " | ".join(
                    (
                        f"{player.display_name} vs {opponent.display_name}",
                        evaluation.bookmaker_id,
                        f"odds={evaluation.offered_odds}",
                        f"consensus={_percent(evaluation.consensus_probability)}",
                        f"ev={_percent(evaluation.expected_value)}",
                        f"peers={evaluation.peer_count}",
                        f"flags={flags}",
                    )
                )
            )
        for skipped in batch.skipped:
            print(
                f"Skipped {skipped.match.match_id}: {skipped.reason}",
                file=sys.stderr,
            )
    return 0


def _backtest_atp_wimbledon(
    arguments: argparse.Namespace,
    *,
    config_path: Path,
    cache_directory: Path,
) -> int:
    run = run_atp_wimbledon_backtest(
        _odds_papi_client(arguments),
        model_settings=load_settings(config_path),
        cache_directory=cache_directory,
    )
    report = run.report
    print("Tournament: ATP Wimbledon Men Singles 2026")
    print("Decision time: 60 minutes before scheduled start")
    print("Bookmakers: 9")
    print("Kelly fraction: 25%")
    print(f"Initial equity: {report.settings.initial_equity}")
    print(f"Fixtures: {run.fixture_count}")
    print(f"Matches evaluated: {run.evaluated_matches}")
    print(f"Matches skipped: {run.skipped_matches}")
    print(f"Offers evaluated: {run.offer_evaluations}")
    print(f"Candidates selected: {report.selected_candidates}")
    print(f"Settled bets: {report.settled_bets}")
    print(f"Wins: {report.wins}")
    print(f"Losses: {report.losses}")
    print(f"Voids: {report.void_bets}")
    print(f"Turnover: {report.turnover}")
    print(f"Profit: {report.profit}")
    print(f"Final equity: {report.final_equity}")
    print(f"ROI: {_percent(report.roi)}")
    print(f"Hit rate: {_percent(report.hit_rate)}")
    print(f"Maximum drawdown: {report.maximum_drawdown}")
    print(f"Raw cache: {cache_directory.resolve()}")
    return 0


def _print_quota(quota: OddsApiQuota) -> None:
    if quota.last_request_cost is not None:
        print(f"Request cost: {quota.last_request_cost}")
    if quota.remaining is not None:
        print(f"Requests remaining: {quota.remaining}")
    if quota.used is not None:
        print(f"Requests used: {quota.used}")


def _parse_regions(value: str) -> tuple[str, ...]:
    regions = tuple(
        region.strip() for region in value.split(",") if region.strip()
    )
    if not regions:
        raise ValueError("at least one region is required")
    return regions


def _existing_database(value: object) -> Path:
    path = Path(str(value))
    if not path.is_file():
        raise WorkflowError(f"database does not exist: {path}")
    return path


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    raw_value = str(value)
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as error:
        raise WorkflowError("--decision-at must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorkflowError("--decision-at must be timezone-aware")
    return parsed.astimezone(UTC)


def _percent(value: Decimal) -> str:
    return f"{(value * Decimal(100)).quantize(Decimal('0.01'))}%"
