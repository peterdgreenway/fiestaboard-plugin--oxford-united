"""Oxford United (English League One) plugin for FiestaBoard.

Fetches standings, live/in-progress match (minute, HT/FT, scorers),
and next fixture for Oxford United from TheSportsDB.

When a match is live, refresh_seconds (default 120) controls polling.
Use run_refresh_now + Save for an immediate update.
When no match is in progress, exposes a Note-friendly next-opponent block.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from src.plugins.base import PluginBase, PluginResult

logger = logging.getLogger(__name__)

# Fixed IDs for Oxford United / English League One
TEAM_ID = "134361"
LEAGUE_ID = "4396"
TEAM_NAME = "Oxford United"
TEAM_SHORT = "OXF"

BASE_URL = "https://www.thesportsdb.com/api/v1/json/{api_key}"

# Timeline event types that count as goals
GOAL_TYPES = {"goal", "penalty", "own goal", "penalty goal"}


class OxfordUnitedPlugin(PluginBase):
    """Oxford United League One standings + fixtures + match detail."""

    @property
    def plugin_id(self) -> str:
        return "oxford_united"

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        refresh = config.get("refresh_seconds", 120)
        if not isinstance(refresh, int) or refresh < 60:
            errors.append("refresh_seconds must be an integer >= 60 (default 120 while live)")
        if "run_refresh_now" in config and not isinstance(config["run_refresh_now"], bool):
            errors.append("run_refresh_now must be a boolean")
        max_scorers = config.get("max_scorers", 4)
        if max_scorers is not None and (
            not isinstance(max_scorers, int) or max_scorers < 1 or max_scorers > 8
        ):
            errors.append("max_scorers must be an integer 1-8")
        return errors

    def on_config_change(self, old_config: Dict[str, Any], new_config: Dict[str, Any]) -> None:
        """Clear cache on Save — enables Run refresh now for live scores/scorers."""
        try:
            self.clear_cache()
            logger.info("Oxford United: cache cleared (refresh now / settings save)")
        except Exception as e:
            logger.warning("Oxford United: clear_cache failed: %s", e)

    def fetch_data(self) -> PluginResult:
        api_key = (
            self.config.get("api_key")
            or os.getenv("OXFORD_UNITED_API_KEY")
            or "123"
        )
        show_form = self.config.get("show_form", True)
        max_scorers = int(self.config.get("max_scorers", 4))
        force = bool(self.config.get("run_refresh_now", False))

        try:
            standing = self._fetch_standing(api_key)
            last = self._fetch_last_result(api_key)
            nxt = self._fetch_next_fixture(api_key)
            live = self._find_live_event(api_key, last, nxt)

            # Prefer live event for scores/scorers/minute
            match_event = live or last
            timeline: List[Dict] = []
            if match_event and match_event.get("idEvent"):
                timeline = self._fetch_timeline(api_key, str(match_event["idEvent"]))

            if not standing and not match_event and not nxt:
                return PluginResult(
                    available=False,
                    error="No data returned from TheSportsDB",
                )

            data = self._build_data(
                standing,
                match_event,
                nxt,
                timeline,
                show_form,
                max_scorers,
                live_event=live,
                force_refresh=force,
            )
            return PluginResult(available=True, data=data)

        except requests.RequestException as e:
            logger.error("Network error fetching Oxford United data: %s", e)
            return PluginResult(available=False, error=f"Network error: {e}")
        except Exception as e:
            logger.exception("Unexpected error in Oxford United plugin")
            return PluginResult(available=False, error=str(e))

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _get(self, api_key: str, endpoint: str, params: Optional[Dict] = None) -> Dict:
        url = f"{BASE_URL.format(api_key=api_key)}/{endpoint}"
        resp = requests.get(url, params=params or {}, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _fetch_standing(self, api_key: str) -> Optional[Dict]:
        data = self._get(api_key, "lookuptable.php", {"l": LEAGUE_ID})
        table = data.get("table") or []
        for row in table:
            if str(row.get("idTeam")) == TEAM_ID or row.get("strTeam") == TEAM_NAME:
                return row
        return None

    def _fetch_last_result(self, api_key: str) -> Optional[Dict]:
        data = self._get(api_key, "eventslast.php", {"id": TEAM_ID})
        results = data.get("results") or []
        for event in results:
            if event.get("intHomeScore") is not None and event.get("intAwayScore") is not None:
                return event
        return results[0] if results else None

    def _fetch_next_fixture(self, api_key: str) -> Optional[Dict]:
        data = self._get(api_key, "eventsnext.php", {"id": TEAM_ID})
        events = data.get("events") or []
        return events[0] if events else None

    def _fetch_timeline(self, api_key: str, event_id: str) -> List[Dict]:
        """Match timeline (goals, cards, subs). Free tier works."""
        try:
            data = self._get(api_key, "lookuptimeline.php", {"id": event_id})
            return data.get("timeline") or []
        except Exception as e:
            logger.warning("Could not fetch timeline for event %s: %s", event_id, e)
            return []

    def _status_is_live(self, event: Optional[Dict]) -> bool:
        if not event:
            return False
        status = (event.get("strStatus") or event.get("strProgress") or "").lower()
        if any(
            x in status
            for x in (
                "not started",
                "ns",
                "finish",
                "ft",
                "aet",
                "pen",
                "postpon",
                "cancel",
                "abandoned",
            )
        ):
            # Finished / not started — unless progress shows a minute
            if "finish" in status or status in ("ft", "aet", "pen") or "not started" in status:
                return False
        if any(
            x in status
            for x in ("progress", "1h", "2h", "ht", "half", "live", "1st", "2nd")
        ):
            return True
        # strProgress often "67'" during live
        progress = (event.get("strProgress") or "").strip()
        if progress.endswith("'") or progress.isdigit():
            return True
        return False

    def _event_minute(self, event: Optional[Dict]) -> str:
        if not event:
            return ""
        progress = (event.get("strProgress") or "").strip()
        if progress:
            # Normalise 67' / 45+2 / HT
            return progress.replace(" ", "")[:8]
        status = (event.get("strStatus") or "").upper()
        if "HT" in status or "HALF" in status:
            return "HT"
        return ""

    def _find_live_event(
        self,
        api_key: str,
        last: Optional[Dict],
        nxt: Optional[Dict],
    ) -> Optional[Dict]:
        """Return the in-progress Oxford match if any (from last/next payloads)."""
        for ev in (last, nxt):
            if ev and self._status_is_live(ev):
                return ev
        return None

    def _truncate(self, text: str, max_len: int) -> str:
        text = (text or "").strip()
        if len(text) <= max_len:
            return text
        cut = text[: max_len + 1]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        return cut[:max_len].strip()

    # ------------------------------------------------------------------
    # Goal / HT helpers
    # ------------------------------------------------------------------

    def _parse_goals(self, timeline: List[Dict]) -> List[Dict[str, Any]]:
        """Extract ordered goal events with minute, scorer, team side."""
        goals: List[Dict[str, Any]] = []
        for t in timeline:
            kind = (t.get("strTimeline") or "").strip().lower()
            detail = (t.get("strTimelineDetail") or "").strip().lower()
            if kind not in GOAL_TYPES and "goal" not in detail and kind != "goal":
                # Accept "Goal" + "Normal Goal" / "Penalty" etc.
                if kind != "goal" and "goal" not in kind:
                    continue

            try:
                minute = int(t.get("intTime") or 0)
            except (TypeError, ValueError):
                minute = 0

            player = (t.get("strPlayer") or "Unknown").strip()
            team = (t.get("strTeam") or "").strip()
            is_home = (t.get("strHome") or "").strip().lower() in ("yes", "1", "true")
            is_own = "own" in kind or "own goal" in detail

            goals.append(
                {
                    "minute": minute,
                    "player": player,
                    "player_short": self._short_name(player),
                    "team": team,
                    "is_home": is_home,
                    "is_own": is_own,
                    "detail": t.get("strTimelineDetail") or kind,
                }
            )

        goals.sort(key=lambda g: g["minute"])
        return goals

    def _derive_ht_ft(
        self, last: Dict, goals: List[Dict]
    ) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
        """
        Returns (ht_home, ht_away, ft_home, ft_away).
        FT comes from the event; HT is derived from goals with minute <= 45
        (or 45+ stoppage counted as first half if period not set).
        """
        ft_h = self._to_int(last.get("intHomeScore"))
        ft_a = self._to_int(last.get("intAwayScore"))

        ht_h = 0
        ht_a = 0
        for g in goals:
            # First-half goals: minute <= 45 (includes 45+ injury time still
            # reported as 45 by many feeds). Own goals credit the opposing side.
            if g["minute"] > 45:
                continue
            if g["is_own"]:
                # Own goal: opposite team scores
                if g["is_home"]:
                    ht_a += 1
                else:
                    ht_h += 1
            else:
                if g["is_home"]:
                    ht_h += 1
                else:
                    ht_a += 1

        # If we have no goals at all but FT is known, HT stays 0-0
        # (common for 0-0 matches). If FT is None, leave HT as None too.
        if ft_h is None and ft_a is None and not goals:
            return None, None, None, None

        return ht_h, ht_a, ft_h, ft_a

    @staticmethod
    def _to_int(val: Any) -> Optional[int]:
        if val is None or val == "":
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    def _short_name(self, full: str, max_len: int = 10) -> str:
        """Surname-focused short form for scorers on a 22-char board."""
        if not full:
            return "?"
        parts = full.strip().split()
        if len(parts) == 1:
            return parts[0][:max_len]
        # Prefer last name; if still long, truncate
        surname = parts[-1]
        if len(surname) <= max_len:
            return surname
        return surname[:max_len]

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def _abbr(self, name: str, max_len: int = 8) -> str:
        if not name:
            return "?"
        replacements = {
            "Oxford United": "OXF",
            "Bradford City": "BRA",
            "Stockport County": "STK",
            "Huddersfield Town": "HUD",
            "Peterborough United": "PET",
            "Burton Albion": "BUR",
            "AFC Wimbledon": "WIM",
            "Mansfield Town": "MAN",
            "Doncaster Rovers": "DON",
            "Blackpool": "BPL",
            "Milton Keynes Dons": "MKD",
            "Luton Town": "LUT",
            "Barnsley": "BAR",
            "Stevenage": "STE",
            "Wigan Athletic": "WIG",
            "Reading": "REA",
            "Leyton Orient": "LEY",
            "Lincoln City": "LIN",
            "Northampton Town": "NOR",
            "Port Vale": "PVA",
            "Rotherham United": "ROT",
            "Wycombe Wanderers": "WYC",
            "Plymouth Argyle": "PLY",
            "Cardiff City": "CAR",
            "Exeter City": "EXE",
            "Bolton Wanderers": "BOL",
            "Bromley": "BRO",
        }
        short = replacements.get(name, name)
        if len(short) > max_len:
            parts = short.split()
            if len(parts) > 1:
                short = "".join(p[0] for p in parts).upper()
            short = short[:max_len]
        return short.upper()

    def _format_scorers_line(self, goals: List[Dict], max_items: int = 4) -> str:
        """Compact scorer string: 'Cheek 10, X 69' (max ~22 chars)."""
        if not goals:
            return ""
        bits = []
        for g in goals[:max_items]:
            bits.append(f"{g['player_short']} {g['minute']}'")
        line = ", ".join(bits)
        if len(goals) > max_items:
            line = line[:19] + "..."
        return line[:22]

    def _format_scorer_rows(self, goals: List[Dict], max_rows: int = 3) -> List[str]:
        """One scorer per row for multi-line display."""
        rows = []
        for g in goals[:max_rows]:
            side = "H" if g["is_home"] else "A"
            own = " (OG)" if g["is_own"] else ""
            rows.append(f"{g['minute']:2d}' {g['player_short']}{own}"[:22])
        return rows

    # ------------------------------------------------------------------
    # Data shaping
    # ------------------------------------------------------------------

    def _build_data(
        self,
        standing: Optional[Dict],
        last: Optional[Dict],
        nxt: Optional[Dict],
        timeline: List[Dict],
        show_form: bool,
        max_scorers: int,
        live_event: Optional[Dict] = None,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        in_progress = live_event is not None

        data: Dict[str, Any] = {
            "team": TEAM_SHORT,
            "match_in_progress": "YES" if in_progress else "NO",
            "is_live": "YES" if in_progress else "NO",
            "current_minute": self._event_minute(live_event) if in_progress else "",
            "run_refresh_now": "YES" if force_refresh else "NO",
            "refresh_seconds": str(int(self.config.get("refresh_seconds", 120) or 120)),
            "position": "",
            "points": "",
            "played": "",
            "won": "",
            "drawn": "",
            "lost": "",
            "gf": "",
            "ga": "",
            "gd": "",
            "form": "",
            # Last match identity
            "last_home": "",
            "last_away": "",
            "last_date": "",
            "last_status": "",
            "last_result": "",
            # Scores
            "ht_home": "",
            "ht_away": "",
            "ht_score": "",
            "ft_home": "",
            "ft_away": "",
            "ft_score": "",
            "last_score": "",  # alias of ft_score for backward compat
            # Scorers
            "scorers": "",
            "scorer_count": "0",
            "scorer_1": "",
            "scorer_2": "",
            "scorer_3": "",
            "scorer_4": "",
            # Formatted lines
            "formatted_table": "",
            "formatted_last": "",
            "formatted_htft": "",
            "formatted_scorers": "",
            "formatted_next": "",
            "next_home": "",
            "next_away": "",
            "next_date": "",
            "next_time": "",
            "next_opponent": "",
            "last_updated": now,
        }

        # ---- Standing ----
        if standing:
            pos = standing.get("intRank", "")
            pts = standing.get("intPoints", "")
            data.update(
                {
                    "position": str(pos),
                    "points": str(pts),
                    "played": str(standing.get("intPlayed", "")),
                    "won": str(standing.get("intWin", "")),
                    "drawn": str(standing.get("intDraw", "")),
                    "lost": str(standing.get("intLoss", "")),
                    "gf": str(standing.get("intGoalsFor", "")),
                    "ga": str(standing.get("intGoalsAgainst", "")),
                    "gd": str(standing.get("intGoalDifference", "") or ""),
                    "form": (standing.get("strForm") or "") if show_form else "",
                }
            )
            suffix = f"{pos}th" if pos else "?"
            data["formatted_table"] = f"{TEAM_SHORT}  {suffix}  {pts}pts".ljust(22)[:22]

        # ---- Last result + HT/FT + scorers ----
        if last:
            home = last.get("strHomeTeam", "")
            away = last.get("strAwayTeam", "")
            date = last.get("dateEvent", "")
            status = last.get("strStatus") or ""

            data["last_home"] = self._abbr(home)
            data["last_away"] = self._abbr(away)
            data["last_date"] = date
            data["last_status"] = status

            goals = self._parse_goals(timeline)
            ht_h, ht_a, ft_h, ft_a = self._derive_ht_ft(last, goals)

            if ft_h is not None and ft_a is not None:
                data["ft_home"] = str(ft_h)
                data["ft_away"] = str(ft_a)
                data["ft_score"] = f"{ft_h}-{ft_a}"
                data["last_score"] = data["ft_score"]

                oxford_home = home == TEAM_NAME
                ox_score = ft_h if oxford_home else ft_a
                opp_score = ft_a if oxford_home else ft_h
                if ox_score > opp_score:
                    data["last_result"] = "W"
                elif ox_score < opp_score:
                    data["last_result"] = "L"
                else:
                    data["last_result"] = "D"

                h_abbr = self._abbr(home, 6)
                a_abbr = self._abbr(away, 6)
                data["formatted_last"] = f"{h_abbr} {ft_h}-{ft_a} {a_abbr}".ljust(22)[:22]

            if ht_h is not None and ht_a is not None:
                data["ht_home"] = str(ht_h)
                data["ht_away"] = str(ht_a)
                data["ht_score"] = f"{ht_h}-{ht_a}"
                # e.g. "HT 0-0  FT 1-0"
                ft_part = data["ft_score"] or "?-?"
                data["formatted_htft"] = f"HT {data['ht_score']}  FT {ft_part}".ljust(22)[:22]

            # Scorers
            data["scorer_count"] = str(len(goals))
            data["formatted_scorers"] = self._format_scorers_line(goals, max_scorers)
            data["scorers"] = data["formatted_scorers"]

            scorer_rows = self._format_scorer_rows(goals, max_rows=4)
            for i, row in enumerate(scorer_rows, start=1):
                data[f"scorer_{i}"] = row

            # Also expose structured list for advanced templates
            data["goals"] = [
                {
                    "minute": g["minute"],
                    "player": g["player_short"],
                    "player_full": g["player"],
                    "team": self._abbr(g["team"], 6),
                    "is_home": "Y" if g["is_home"] else "N",
                    "is_own": "Y" if g["is_own"] else "N",
                    "formatted": f"{g['minute']}' {g['player_short']}",
                }
                for g in goals
            ]

        # ---- Live match extras ----
        if in_progress and last:
            data["live_status"] = (last.get("strStatus") or last.get("strProgress") or "LIVE")[:20]
            # During live, last_* / ft_* hold current score; ht_* from timeline
            if data.get("current_minute"):
                data["formatted_minute"] = f"MIN {data['current_minute']}"[:15]
            else:
                data["formatted_minute"] = "LIVE"[:15]
            data["formatted_live"] = (
                f"{data.get('last_score', '')} {data.get('current_minute', '')}".strip()
            )[:22]
        else:
            data["live_status"] = ""
            data["formatted_minute"] = ""
            data["formatted_live"] = ""

        # ---- Next fixture (+ Note block when not live) ----
        if nxt:
            home = nxt.get("strHomeTeam", "")
            away = nxt.get("strAwayTeam", "")
            date = nxt.get("dateEvent", "")
            time_str = (nxt.get("strTime") or "")[:5]

            data["next_home"] = self._abbr(home)
            data["next_away"] = self._abbr(away)
            data["next_date"] = date
            data["next_time"] = time_str

            if home == TEAM_NAME:
                data["next_opponent"] = self._abbr(away)
                data["next_opponent_full"] = away[:40]
                data["next_home_away"] = "HOME"
                data["next_ha"] = "H"
                data["formatted_next"] = f"NEXT {TEAM_SHORT} vs {self._abbr(away)}"[:22]
            else:
                data["next_opponent"] = self._abbr(home)
                data["next_opponent_full"] = home[:40]
                data["next_home_away"] = "AWAY"
                data["next_ha"] = "A"
                data["formatted_next"] = f"NEXT {self._abbr(home)} vs {TEAM_SHORT}"[:22]

            # Note 15×3 block when no match in progress
            # Row1: NEXT H/A   Row2: opponent   Row3: date time
            ha = data["next_ha"]
            opp = self._truncate(data["next_opponent_full"].upper(), 15)
            # Prefer short date e.g. 01 NOV 15:00
            date_disp = date
            try:
                if date and len(date) >= 10:
                    dt = datetime.strptime(date[:10], "%Y-%m-%d")
                    date_disp = dt.strftime("%d %b").upper()
            except ValueError:
                date_disp = date[:9]
            when = f"{date_disp} {time_str}".strip()[:15]
            r1 = f"NEXT {ha}".ljust(15)[:15]
            r2 = opp.ljust(15)[:15]
            r3 = when.ljust(15)[:15]
            data["next_row1"] = r1
            data["next_row2"] = r2
            data["next_row3"] = r3
            data["next_match_block"] = f"{r1}\n{r2}\n{r3}"
            data["next_match_line"] = self._truncate(
                f"NEXT {ha} {data['next_opponent']} {when}", 45
            )
        else:
            data["next_home_away"] = ""
            data["next_ha"] = ""
            data["next_opponent_full"] = ""
            data["next_row1"] = ""
            data["next_row2"] = ""
            data["next_row3"] = ""
            data["next_match_block"] = ""
            data["next_match_line"] = ""

        # Primary board block: live scores vs next match
        if in_progress:
            data["match_block"] = data.get("formatted_live") or data.get("formatted_last") or ""
            data["display_mode"] = "LIVE"
        else:
            data["match_block"] = data.get("next_match_line") or data.get("formatted_next") or ""
            data["display_mode"] = "NEXT"

        return data

    def get_formatted_display(self) -> Optional[List[str]]:
        """Default 6-line board layout including HT/FT and scorers."""
        result = self.get_data()
        if not result.available or not result.data:
            return None

        d = result.data
        lines = [
            "   OXFORD UNITED    ",
            d.get("formatted_table", "")[:22],
            d.get("formatted_last", "")[:22],
            d.get("formatted_htft", "")[:22]
            or d.get("formatted_next", "")[:22],
            d.get("formatted_scorers", "")[:22]
            or f"{d.get('next_date', '')} {d.get('next_time', '')}".strip()[:22],
            (f"Form {d.get('form', '')}".strip() if d.get("form") else d.get("formatted_next", ""))[:22],
        ]
        while len(lines) < 6:
            lines.append("")
        return [line.ljust(22)[:22] for line in lines[:6]]
