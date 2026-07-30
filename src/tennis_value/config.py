"""Typed application configuration will live in this module."""
import os
def get_odds_api_key() -> str:
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        raise RuntimeError("ODDS_API_KEY environment variable is not set. Add it to your environment variables.")
    return api_key
