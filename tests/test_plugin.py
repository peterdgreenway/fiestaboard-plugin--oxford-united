"""Tests for the Oxford United plugin (v1.1 – HT/FT + scorers)."""

from unittest.mock import MagicMock, patch
import sys
from types import ModuleType


class _PluginResult:
    def __init__(self, available=True, data=None, error=None, formatted_lines=None):
        self.available = available
        self.data = data or {}
        self.error = error
        self.formatted_lines = formatted_lines


class _PluginBase:
    def __init__(self, manifest=None):
        self.config = {}
        self._manifest = manifest or {}

    def get_data(self):
        return self.fetch_data()


fake_base = ModuleType("src.plugins.base")
fake_base.PluginBase = _PluginBase
fake_base.PluginResult = _PluginResult
sys.modules["src"] = ModuleType("src")
sys.modules["src.plugins"] = ModuleType("src.plugins")
sys.modules["src.plugins.base"] = fake_base

# Import after stubs
import importlib.util
spec = importlib.util.spec_from_file_location(
    "oxford_united",
    "/home/workdir/artifacts/fiestaboard-plugin--oxford-united/__init__.py",
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
OxfordUnitedPlugin = mod.OxfordUnitedPlugin


class TestOxfordUnitedPlugin:
    def _make_plugin(self, config=None):
        plugin = OxfordUnitedPlugin(
            {"id": "oxford_united", "name": "Oxford United", "version": "1.1.0"}
        )
        plugin.config = config or {
            "api_key": "123",
            "refresh_seconds": 300,
            "show_form": True,
            "max_scorers": 4,
        }
        return plugin

    def test_plugin_id(self):
        assert self._make_plugin().plugin_id == "oxford_united"

    def test_parse_goals(self):
        plugin = self._make_plugin()
        timeline = [
            {
                "strTimeline": "Goal",
                "strTimelineDetail": "Normal Goal",
                "intTime": "10",
                "strPlayer": "Michael Cheek",
                "strTeam": "Bromley",
                "strHome": "Yes",
            },
            {
                "strTimeline": "Card",
                "strTimelineDetail": "Yellow Card",
                "intTime": "20",
                "strPlayer": "Someone",
                "strTeam": "Bromley",
                "strHome": "Yes",
            },
            {
                "strTimeline": "Goal",
                "strTimelineDetail": "Normal Goal",
                "intTime": "55",
                "strPlayer": "Away Player",
                "strTeam": "Peterborough United",
                "strHome": "No",
            },
        ]
        goals = plugin._parse_goals(timeline)
        assert len(goals) == 2
        assert goals[0]["minute"] == 10
        assert goals[0]["player_short"] == "Cheek"
        assert goals[0]["is_home"] is True
        assert goals[1]["minute"] == 55
        assert goals[1]["is_home"] is False

    def test_derive_ht_ft(self):
        plugin = self._make_plugin()
        last = {"intHomeScore": "2", "intAwayScore": "1"}
        goals = [
            {"minute": 12, "is_home": True, "is_own": False},
            {"minute": 40, "is_home": False, "is_own": False},
            {"minute": 70, "is_home": True, "is_own": False},
        ]
        ht_h, ht_a, ft_h, ft_a = plugin._derive_ht_ft(last, goals)
        assert (ht_h, ht_a) == (1, 1)
        assert (ft_h, ft_a) == (2, 1)

    def test_format_scorers_line(self):
        plugin = self._make_plugin()
        goals = [
            {"player_short": "Cheek", "minute": 10},
            {"player_short": "Jones", "minute": 67},
        ]
        line = plugin._format_scorers_line(goals)
        assert "Cheek 10'" in line
        assert "Jones 67'" in line

    @patch("requests.get")
    def test_fetch_with_timeline(self, mock_get):
        standing = {
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
        last = {
            "idEvent": "2500845",
            "strHomeTeam": "Oxford United",
            "strAwayTeam": "Bradford City",
            "intHomeScore": "1",
            "intAwayScore": "0",
            "dateEvent": "2026-10-24",
            "strStatus": "FT",
        }
        nxt = {
            "strHomeTeam": "Oxford United",
            "strAwayTeam": "Stockport County",
            "dateEvent": "2026-10-27",
            "strTime": "19:45:00",
        }
        timeline = [
            {
                "strTimeline": "Goal",
                "strTimelineDetail": "Normal Goal",
                "intTime": "67",
                "strPlayer": "Mark Harris",
                "strTeam": "Oxford United",
                "strHome": "Yes",
            }
        ]

        responses = [
            MagicMock(**{"json.return_value": {"table": [standing]}, "raise_for_status": MagicMock()}),
            MagicMock(**{"json.return_value": {"results": [last]}, "raise_for_status": MagicMock()}),
            MagicMock(**{"json.return_value": {"events": [nxt]}, "raise_for_status": MagicMock()}),
            MagicMock(**{"json.return_value": {"timeline": timeline}, "raise_for_status": MagicMock()}),
        ]
        for r in responses:
            r.status_code = 200
            r.raise_for_status = MagicMock()
        mock_get.side_effect = responses

        plugin = self._make_plugin()
        result = plugin.fetch_data()
        assert result.available is True
        d = result.data
        assert d["ft_score"] == "1-0"
        assert d["ht_score"] == "0-0"  # goal at 67' is second half
        assert d["last_result"] == "W"
        assert "Harris" in d["formatted_scorers"]
        assert "67" in d["formatted_scorers"]
        assert d["formatted_htft"].startswith("HT")
