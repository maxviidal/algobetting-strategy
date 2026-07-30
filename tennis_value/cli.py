"""Command-line entry points for safe odds collection."""

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from tennis_value.config import ConfigurationError, get_odds_api_key, load_env_file
from tennis_value.ingestion import IngestionError
from tennis_value.odds_api import OddsApiClient, OddsApiError, OddsApiQuota
from tennis_value.storage import SqliteOddsRepository


def main(argv: Sequence[str] | None = None) -> int:
    """Run the tennis-value command line interface."""

    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        env_file = Path(arguments.env_file)
        load_env_file(env_file)
        client = OddsApiClient(
            get_odds_api_key(),
            timeout_seconds=float(arguments.timeout),
        )
        if arguments.command == "sports":
            return _list_tennis_sports(client)
        if arguments.command == "collect":
            return _collect_current_odds(
                client,
                sport=str(arguments.sport),
                regions=_parse_regions(str(arguments.regions)),
                database=Path(arguments.database),
            )
    except (
        ConfigurationError,
        IngestionError,
        OddsApiError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    parser.error("a command is required")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tennis-value",
        description="Collect point-in-time tennis odds safely.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="environment file containing ODDS_API_KEY (default: .env)",
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
        help="fetch and preserve current pre-match h2h odds",
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
    collect.add_argument(
        "--database",
        default="data/tennis_value.sqlite3",
        help="SQLite database path",
    )
    return parser


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
