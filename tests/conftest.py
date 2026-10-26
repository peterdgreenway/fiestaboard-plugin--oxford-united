"""Shared fixtures for Oxford United plugin tests."""

import pytest


@pytest.fixture
def sample_config():
    return {
        "api_key": "123",
        "refresh_seconds": 300,
        "show_form": True,
    }


@pytest.fixture
def sample_standing():
    return {
        "idTeam": "134361",
        "strTeam": "Oxford United",
        "intRank": "8",
        "intPoints": "18",
        "intPlayed": "12",
        "intWin": "5",
        "intDraw": "3",
        "intLoss": "4",
        "intGoalsFor": "15",
        "intGoalsAgainst": "13",
        "intGoalDifference": "2",
        "strForm": "WWDLW",
    }


@pytest.fixture
def sample_last():
    return {
        "strHomeTeam": "Oxford United",
        "strAwayTeam": "Bradford City",
        "intHomeScore": "1",
        "intAwayScore": "0",
        "dateEvent": "2026-10-24",
    }


@pytest.fixture
def sample_next():
    return {
        "strHomeTeam": "Oxford United",
        "strAwayTeam": "Stockport County",
        "dateEvent": "2026-10-27",
        "strTime": "19:45:00",
    }
