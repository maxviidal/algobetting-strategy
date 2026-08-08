"""Canonical 30-team NBA catalog and provider-name aliases."""

import re

from basketball_value.domain import Team


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


_TEAM_DATA = (
    ("ATL", "Atlanta Hawks"),
    ("BOS", "Boston Celtics"),
    ("BKN", "Brooklyn Nets"),
    ("CHA", "Charlotte Hornets"),
    ("CHI", "Chicago Bulls"),
    ("CLE", "Cleveland Cavaliers"),
    ("DAL", "Dallas Mavericks"),
    ("DEN", "Denver Nuggets"),
    ("DET", "Detroit Pistons"),
    ("GSW", "Golden State Warriors"),
    ("HOU", "Houston Rockets"),
    ("IND", "Indiana Pacers"),
    ("LAC", "Los Angeles Clippers"),
    ("LAL", "Los Angeles Lakers"),
    ("MEM", "Memphis Grizzlies"),
    ("MIA", "Miami Heat"),
    ("MIL", "Milwaukee Bucks"),
    ("MIN", "Minnesota Timberwolves"),
    ("NOP", "New Orleans Pelicans"),
    ("NYK", "New York Knicks"),
    ("OKC", "Oklahoma City Thunder"),
    ("ORL", "Orlando Magic"),
    ("PHI", "Philadelphia 76ers"),
    ("PHX", "Phoenix Suns"),
    ("POR", "Portland Trail Blazers"),
    ("SAC", "Sacramento Kings"),
    ("SAS", "San Antonio Spurs"),
    ("TOR", "Toronto Raptors"),
    ("UTA", "Utah Jazz"),
    ("WAS", "Washington Wizards"),
)

NBA_TEAMS = tuple(
    Team(code, code, name, (("balldontlie", str(index)),))
    for index, (code, name) in enumerate(_TEAM_DATA, start=1)
)

_ALIASES = {_key(value): code for code, name in _TEAM_DATA for value in (code, name)}
_ALIASES.update(
    {
        _key("LA Clippers"): "LAC",
        _key("LA Lakers"): "LAL",
        _key("NY Knicks"): "NYK",
        _key("Golden State"): "GSW",
        _key("New Orleans"): "NOP",
        _key("Oklahoma City"): "OKC",
        _key("Portland Trailblazers"): "POR",
        _key("San Antonio"): "SAS",
        _key("Philadelphia Sixers"): "PHI",
    }
)


def resolve_team(value: str) -> Team:
    """Resolve a provider name or abbreviation to a canonical team."""

    team_id = _ALIASES.get(_key(value))
    if team_id is None:
        raise KeyError(f"unknown NBA team alias: {value!r}")
    return next(team for team in NBA_TEAMS if team.team_id == team_id)


def resolve_provider_team(source: str, provider_id: str) -> Team:
    """Resolve one explicit provider team ID."""

    matches = tuple(
        team for team in NBA_TEAMS if (source, provider_id) in team.provider_ids
    )
    if len(matches) != 1:
        raise KeyError(f"unknown {source} team ID: {provider_id!r}")
    return matches[0]
