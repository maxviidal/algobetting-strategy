"""Application configuration."""

import os


def get_odds_api_key() -> str:
    """Return the Odds API key configured in the environment."""
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        message = (
            "ODDS_API_KEY environment variable is not set. "
            "Add it to your environment variables."
        )
        raise RuntimeError(message)
    return api_key
