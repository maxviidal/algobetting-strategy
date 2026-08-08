"""Command-line interface for the NBA consensus pilot."""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from basketball_value.config import (
    BasketballConfigurationError,
    load_basketball_settings,
)
from basketball_value.providers import (
    BallDontLieClient,
    BasketballProviderError,
    TheOddsApiHistoricalClient,
)
from basketball_value.reporting import (
    build_summary,
    export_reports,
    run_backtest,
)
from basketball_value.workflow import (
    PreflightReport,
    fetch_nba_history,
    load_cached_dataset,
    preflight_nba_history,
)

_DEFAULT_CONFIG = "configs/basketball_research.toml"
_DEFAULT_CACHE = "data/nba_moneyline"


def main(argv: Sequence[str] | None = None) -> int:
    """Run one basketball research command."""

    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        settings = load_basketball_settings(Path(arguments.config))
        cache_directory = Path(arguments.cache_directory)
        if arguments.command == "preflight-nba-moneyline":
            _load_env_file(Path(arguments.env_file))
            odds_key = os.environ.get("ODDS_API_KEY", "")
            odds_client = (
                TheOddsApiHistoricalClient(
                    odds_key,
                    timeout=float(arguments.timeout),
                )
                if odds_key
                else None
            )
            report = preflight_nba_history(
                settings=settings,
                cache_directory=cache_directory,
                reports_directory=Path(arguments.output_directory),
                results_key_present=bool(os.environ.get("BALLDONTLIE_API_KEY", "")),
                odds_client=odds_client,
            )
            _print_preflight(report, settings.profile)
            return 0 if report.status != "NOT_READY" else 2
        if arguments.command == "fetch-nba-history":
            _load_env_file(Path(arguments.env_file))
            results_key = os.environ.get("BALLDONTLIE_API_KEY", "")
            results_client = BallDontLieClient(
                results_key, timeout=float(arguments.timeout)
            )
            confirmed = arguments.confirm_quota_cost
            odds_client = (
                TheOddsApiHistoricalClient(
                    os.environ.get("ODDS_API_KEY", ""),
                    timeout=float(arguments.timeout),
                )
                if confirmed is not None
                else None
            )
            manifest = fetch_nba_history(
                settings=settings,
                cache_directory=cache_directory,
                results_client=results_client,
                odds_client=odds_client,
                confirmed_credits=confirmed,
                progress=_progress,
            )
            print(f"quota manifest: {manifest.path}")
            print(f"distinct timestamps: {manifest.distinct_timestamps}")
            print(
                f"total maximum historical credits: {manifest.total_expected_credits}"
            )
            print(f"maximum additional credits: {manifest.expected_credits}")
            print(f"cached requests: {manifest.cached_requests}")
            print(f"requests not completed: {manifest.missing_requests}")
            if confirmed is None:
                print(
                    "No paid odds requests were made. Review the manifest, then "
                    "rerun with --confirm-quota-cost "
                    f"{manifest.expected_credits}."
                )
            return 0
        dataset = load_cached_dataset(
            settings=settings, cache_directory=cache_directory
        )
        run = run_backtest(dataset, settings)
        if arguments.command == "backtest-nba-moneyline":
            summary = build_summary(dataset, run, settings)
            output = Path(arguments.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n"
            )
            print(f"backtest summary: {output}")
            print(f"conclusion: {summary['acceptance']['conclusion']}")
            return 0
        paths = export_reports(
            output_directory=Path(arguments.output_directory),
            dataset=dataset,
            run=run,
            settings=settings,
        )
        for path in paths:
            print(path)
        return 0
    except (
        BasketballConfigurationError,
        BasketballProviderError,
        OSError,
        ValueError,
        KeyError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="basketball-value",
        description="Collect and backtest NBA moneyline consensus value.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser(
        "preflight-nba-moneyline",
        help="validate data, quota, and output readiness without paid requests",
    )
    _common(preflight)
    preflight.add_argument("--env-file", default=".env")
    preflight.add_argument("--timeout", type=float, default=30)
    preflight.add_argument("--output-directory", default="reports/nba_2023_24")
    fetch = commands.add_parser(
        "fetch-nba-history",
        help="cache NBA schedules and prepare or execute approved odds requests",
    )
    _common(fetch)
    fetch.add_argument("--env-file", default=".env")
    fetch.add_argument("--timeout", type=float, default=30)
    fetch.add_argument(
        "--confirm-quota-cost",
        type=int,
        help="exact manifest credit total required before paid requests",
    )
    backtest = commands.add_parser(
        "backtest-nba-moneyline", help="run the locked strategy from cache"
    )
    _common(backtest)
    backtest.add_argument("--output", default="reports/nba/nba_moneyline_backtest.json")
    export = commands.add_parser(
        "export-nba-report", help="export cached game, offer, and summary reports"
    )
    _common(export)
    export.add_argument("--output-directory", default="reports/nba")
    return parser


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=_DEFAULT_CONFIG)
    parser.add_argument("--cache-directory", default=_DEFAULT_CACHE)


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _progress(completed: int, total: int, status: str) -> None:
    if completed == 1 or completed % 25 == 0 or completed == total:
        print(f"historical odds: {completed}/{total} ({status})", flush=True)


def _print_preflight(report: PreflightReport, profile: str) -> None:
    manifest = report.manifest
    print("NBA MONEYLINE PILOT PREFLIGHT")
    print(f"profile: {profile}")
    print(f"regular-season games: {report.games}")
    print(f"result quarantines: {report.result_quarantines}")
    print(f"historical timestamps: {manifest.distinct_timestamps}")
    print(f"already cached: {manifest.cached_requests}")
    print(f"still required: {manifest.missing_requests}")
    print(f"maximum additional credits: {manifest.expected_credits}")
    print(
        "Odds API credits available: "
        + (
            str(report.available_credits)
            if report.available_credits is not None
            else "unknown"
        )
    )
    print(
        "credit headroom: "
        + (
            str(report.credit_headroom)
            if report.credit_headroom is not None
            else "unknown"
        )
    )
    print(f"BALLDONTLIE key: {'present' if report.results_key_present else 'missing'}")
    print(f"The Odds API key: {'present' if report.odds_key_present else 'missing'}")
    print(f"cache directory: {'writable' if report.cache_writable else 'not writable'}")
    print(
        f"report directory: {'writable' if report.reports_writable else 'not writable'}"
    )
    for reason in report.reasons:
        print(f"reason: {reason}")
    print(f"PREFLIGHT RESULT: {report.status}")
    print("No paid historical requests were made.")


if __name__ == "__main__":
    raise SystemExit(main())
