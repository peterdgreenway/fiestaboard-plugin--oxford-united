"""Oxford United (English League One) plugin for FiestaBoard.

Data sources (no TheSportsDB / sportsapi):
  - Primary: Fixture Download JSON feed (EFL League One season schedule & scores)
  - Standings & form: computed from finished matches in that feed

Provides last game, match-in-progress, next game, league summary blocks.
"""

from __future__ import annotations

import logging
import time
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from src.plugins.base import PluginBase, PluginResult

logger = logging.getLogger(__name__)

TEAM_NAME = "Oxford United"
TEAM_SHORT = "OXF"
TEAM_ALIASES = ("oxford united", "oxford utd", "oxford")

# Season feed — no API key (not TheSportsDB)
FIXTURE_FEED_URLS = (
    "https://fixturedownload.com/feed/json/efl-league-one-2026",
    "https://fixturedownload.com/feed/json/efl-league-one-2025",
)

# Timeline event types that count as goals
GOAL_TYPES = {"goal", "penalty", "own goal", "penalty goal", "score", "g"}

_SCORER_CACHE: Dict[str, List[Dict[str, Any]]] = {}
# Rate limit / response cache (TheSportsDB free tier is easily exhausted)
_HTTP_CACHE: Dict[str, Any] = {}  # key -> {at, data}
_API_BUDGET: Dict[str, Any] = {"minute_key": "", "minute_count": 0, "day_key": "", "day_count": 0}
_RATE_LIMIT: Dict[str, Any] = {"suspended_until": 0.0, "message": ""}
_LAST_GOOD: Dict[str, Any] = {"data": None, "at": 0.0}
_LAST_NETWORK_AT: float = 0.0
HTTP_CACHE_TTL = 120  # seconds for identical GETs within a poll window
HTTP_CACHE_TTL_TABLE = 900  # standings change slowly
HTTP_CACHE_TTL_LAST = 180  # last results
MIN_POLL_GAP = 90  # soft minimum between full network refreshes unless force
DAILY_BUDGET = 80  # soft daily cap (free key is tight)
MINUTE_BUDGET = 8  # max calls per rolling minute
BACKOFF_429 = 900  # 15 min default suspend on 429

GREEN = "{66}"


def _s(val: Any, default: str = "") -> str:
    """Coerce None/non-str to a safe string for concatenation and slicing."""
    if val is None:
        return default
    if isinstance(val, str):
        return val
    try:
        return str(val)
    except Exception:
        return default

YELLOW = "{65}"
RED = "{63}"



def _fmt_countdown(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h}h{m:02d}m"
    if seconds >= 60:
        m, s = divmod(seconds, 60)
        return f"{m}m{s:02d}s"
    return f"{seconds}s"


def build_oxford_api_dashboard() -> Dict[str, str]:
    """API usage % and countdown to next allowed network call."""
    global _LAST_NETWORK_AT
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.gmtime())
    if _API_BUDGET.get("day_key") != day:
        calls = 0
    else:
        calls = int(_API_BUDGET.get("day_count") or 0)
    limit = max(1, DAILY_BUDGET)
    remaining = max(0, limit - calls)
    pct = min(100, int(round(100.0 * calls / limit)))

    suspended_until = float(_RATE_LIMIT.get("suspended_until") or 0)
    suspended = now < suspended_until

    # Next call: suspension end, or min gap after last network, or next minute if minute-capped
    candidates = []
    if suspended:
        candidates.append(suspended_until)
    if _LAST_NETWORK_AT:
        candidates.append(float(_LAST_NETWORK_AT) + MIN_POLL_GAP)
    minute_count = int(_API_BUDGET.get("minute_count") or 0)
    if minute_count >= MINUTE_BUDGET:
        # next UTC minute boundary
        gm = time.gmtime(now)
        secs_into = gm.tm_sec
        candidates.append(now + (60 - secs_into))
    if not candidates:
        # ready now (or only cache hits)
        next_at = now
    else:
        next_at = max(candidates)

    wait = max(0, int(next_at - now))
    countdown = _fmt_countdown(wait)
    ready = wait <= 0 and not suspended and remaining > 0

    if suspended or pct >= 90:
        flap, flap_name, status = "{63}", "red", "SUSPENDED" if suspended else "HIGH"
    elif pct >= 70:
        flap, flap_name, status = "{65}", "yellow", "HIGH"
    else:
        flap, flap_name, status = "{66}", "green", "OK"

    r1 = f"{flap} API {pct}%"
    while len(r1.replace("{63}", " ").replace("{65}", " ").replace("{66}", " ")) > 15 and "API" in r1:
        r1 = r1[:-1]
    r2 = f"{calls}/{limit}".ljust(15)[:15]
    r3 = (f"NEXT {countdown}" if wait > 0 else ("READY" if ready else f"{remaining} LEFT")).ljust(15)[:15]
    block = f"{r1}\n{r2}\n{r3}"

    return {
        "api_usage_percent": str(pct),
        "api_usage_status": status,
        "api_usage_indicator": flap,
        "api_usage_indicator_name": flap_name,
        "api_calls_today": str(calls),
        "api_budget_limit": str(limit),
        "api_budget_remaining": str(remaining),
        "api_next_call_seconds": str(wait),
        "api_next_call_countdown": countdown,
        "api_next_call_ready": "YES" if ready else "NO",
        "api_next_call_at": time.strftime("%H:%M:%S UTC", time.gmtime(next_at)) if wait > 0 else "now",
        "rate_limit_suspended": "YES" if suspended else "NO",
        "api_usage_note_row1": r1[:22],
        "api_usage_note_row2": r2,
        "api_usage_note_row3": r3,
        "api_usage_note_block": block,
        "api_usage_line": f"API {pct}% {countdown}".strip()[:22],
    }


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
        idle = config.get("idle_refresh_seconds", 1800)
        if not isinstance(idle, int) or idle < 300:
            errors.append("idle_refresh_seconds must be an integer >= 300 (default 1800)")
        if "run_refresh_now" in config and not isinstance(config["run_refresh_now"], bool):
            errors.append("run_refresh_now must be a boolean")
        max_scorers = config.get("max_scorers", 4)
        if max_scorers is not None and (
            not isinstance(max_scorers, int) or max_scorers < 1 or max_scorers > 8
        ):
            errors.append("max_scorers must be an integer 1-8")
        fg = config.get("form_games", 5)
        if fg not in (3, 5) and fg not in ("3", "5"):
            errors.append("form_games must be 3 or 5")
        if "form_show_letters" in config and not isinstance(config["form_show_letters"], bool):
            errors.append("form_show_letters must be a boolean")
        if "scheduled_refresh_enabled" in config and not isinstance(
            config["scheduled_refresh_enabled"], bool
        ):
            errors.append("scheduled_refresh_enabled must be a boolean")
        style = config.get("form_style", "last_5_letters")
        allowed = {
            "last_5_letters", "last_3_letters", "last_5_tiles", "last_3_tiles",
            "", None,
        }
        if style not in allowed and str(style).lower() not in allowed:
            errors.append(
                "form_style must be last_3_tiles, last_5_tiles, last_3_letters, or last_5_letters"
            )
        return errors

    def on_config_change(self, old_config: Dict[str, Any], new_config: Dict[str, Any]) -> None:
        """Clear cache on Save — enables Run refresh now for live scores/scorers."""
        try:
            self.clear_cache()
            logger.info("Oxford United: cache cleared (refresh now / settings save)")
        except Exception as e:
            logger.warning("Oxford United: clear_cache failed: %s", e)

    def fetch_data(self) -> PluginResult:
        # api_key kept for config compatibility; SportsDB is no longer used
        api_key = (
            self.config.get("api_key")
            or os.getenv("OXFORD_UNITED_API_KEY")
            or ""
        )
        show_form = self.config.get("show_form", True)
        max_scorers = int(self.config.get("max_scorers", 4))
        force = bool(self.config.get("run_refresh_now", False))
        if force:
            self._clear_all_caches()

        try:
            # If suspended, prefer last good board data
            if time.time() < float(_RATE_LIMIT.get("suspended_until") or 0):
                if _LAST_GOOD.get("data"):
                    data = dict(_LAST_GOOD["data"])
                    data["api_warning"] = _RATE_LIMIT.get("message") or "rate limited"
                    data["data_stale"] = "YES"
                    data.update(build_oxford_api_dashboard())
                    return PluginResult(available=True, data=data)

            if force:
                self._clear_all_caches()

            # --- Non-SportsDB harvest (Fixture Download + computed table) ---
            bundle = self._harvest_fixture_feed(force=force)
            standing = bundle.get("standing")
            last_finished = bundle.get("last_finished")
            live = bundle.get("live")
            nxt = bundle.get("next")
            match_event = live or last_finished
            timeline: List[Dict] = []  # feed has no per-goal timeline

            if not standing and not match_event and not nxt and not last_finished:
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
                last_finished=last_finished,
            )
            data.update(build_oxford_api_dashboard())
            _LAST_GOOD["data"] = dict(data)
            _LAST_GOOD["at"] = time.time()
            return PluginResult(available=True, data=data)

        except requests.RequestException as e:
            logger.error("Network error fetching Oxford United data: %s", e)
            if _LAST_GOOD.get("data"):
                data = dict(_LAST_GOOD["data"])
                data["api_warning"] = str(e)[:120]
                data["data_stale"] = "YES"
                data.update(build_oxford_api_dashboard())
                return PluginResult(available=True, data=data)
            return PluginResult(available=False, error=f"Network error: {e}")
        except Exception as e:
            logger.exception("Unexpected error in Oxford United plugin")
            err = str(e)
            if "429" in err or "budget" in err.lower() or "rate limit" in err.lower():
                self._suspend(BACKOFF_429, err[:200])
            if _LAST_GOOD.get("data"):
                data = dict(_LAST_GOOD["data"])
                data["api_warning"] = err[:120]
                data["data_stale"] = "YES"
                data.update(build_oxford_api_dashboard())
                return PluginResult(available=True, data=data)
            return PluginResult(available=False, error=err)

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _clear_all_caches(self) -> None:
        """Clear HTTP, scorer, last-good, and plugin base caches (Refresh now)."""
        global _SCORER_CACHE, _HTTP_CACHE, _LAST_GOOD
        _SCORER_CACHE.clear()
        _HTTP_CACHE.clear()
        # Keep last-good snapshot for resilience unless explicit full wipe
        try:
            self.clear_cache()
        except Exception as e:
            logger.warning("clear_cache: %s", e)
        logger.info("Oxford United: caches cleared (refresh now)")

    def _is_oxford_name(self, name: str) -> bool:
        n = _s(name).strip().lower()
        return any(a in n for a in TEAM_ALIASES)

    def _match_to_event(self, m: Dict[str, Any]) -> Dict[str, Any]:
        """Map Fixture Download row to internal event shape (SportsDB-like)."""
        home = _s(m.get("HomeTeam"))
        away = _s(m.get("AwayTeam"))
        hs = m.get("HomeTeamScore")
        as_ = m.get("AwayTeamScore")
        date_utc = _s(m.get("DateUtc"))
        date_part = date_utc[:10] if date_utc else ""
        time_part = ""
        if "T" in date_utc.replace(" ", "T"):
            time_part = date_utc.replace(" ", "T").split("T")[-1][:5]
        elif len(date_utc) >= 16:
            time_part = date_utc[11:16]
        finished = hs is not None and as_ is not None
        # Live heuristic: same UK calendar day, kickoff passed, not finished
        status = "Match Finished" if finished else "Not Started"
        progress = "FT" if finished else ""
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo("Europe/London"))
        except Exception:
            now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if not finished and date_part == today and time_part:
            try:
                hh, mm = int(time_part[:2]), int(time_part[3:5] or 0)
                kick_mins = hh * 60 + mm
                now_mins = now.hour * 60 + now.minute
                # 0–130 minutes after KO → treat as in progress
                if 0 <= (now_mins - kick_mins) <= 130:
                    status = "In Play"
                    elapsed = max(1, now_mins - kick_mins)
                    progress = f"{min(elapsed, 90)}'"
            except ValueError:
                pass
        eid = _s(m.get("MatchNumber") or f"{date_part}-{home}-{away}")
        return {
            "idEvent": eid,
            "strHomeTeam": home,
            "strAwayTeam": away,
            "intHomeScore": hs if hs is not None else None,
            "intAwayScore": as_ if as_ is not None else None,
            "dateEvent": date_part,
            "strTime": time_part,
            "strStatus": status,
            "strProgress": progress,
            "strHomeGoalDetails": "",
            "strAwayGoalDetails": "",
            "idHomeTeam": "OXF" if self._is_oxford_name(home) else home,
            "idAwayTeam": "OXF" if self._is_oxford_name(away) else away,
        }

    def _compute_standing_and_form(self, matches: List[Dict]) -> Optional[Dict]:
        """Build Oxford table row + form from finished league matches."""
        stats: Dict[str, Dict[str, Any]] = {}

        def team_stats(name: str) -> Dict[str, Any]:
            if name not in stats:
                stats[name] = {
                    "name": name, "played": 0, "won": 0, "drawn": 0, "lost": 0,
                    "gf": 0, "ga": 0, "pts": 0, "results": [],
                }
            return stats[name]

        finished = [
            m for m in matches
            if m.get("HomeTeamScore") is not None and m.get("AwayTeamScore") is not None
        ]
        finished.sort(key=lambda m: _s(m.get("DateUtc")))
        for m in finished:
            home, away = _s(m.get("HomeTeam")), _s(m.get("AwayTeam"))
            try:
                hs, as_ = int(m["HomeTeamScore"]), int(m["AwayTeamScore"])
            except (TypeError, ValueError):
                continue
            for team, gf, ga, home_side in (
                (home, hs, as_, True),
                (away, as_, hs, False),
            ):
                st = team_stats(team)
                st["played"] += 1
                st["gf"] += gf
                st["ga"] += ga
                if gf > ga:
                    st["won"] += 1
                    st["pts"] += 3
                    st["results"].append("W")
                elif gf < ga:
                    st["lost"] += 1
                    st["results"].append("L")
                else:
                    st["drawn"] += 1
                    st["pts"] += 1
                    st["results"].append("D")

        if not stats:
            return None
        ordered = sorted(
            stats.values(),
            key=lambda s: (s["pts"], s["gf"] - s["ga"], s["gf"]),
            reverse=True,
        )
        ox = None
        rank = 0
        for i, row in enumerate(ordered, start=1):
            if self._is_oxford_name(row["name"]):
                ox = row
                rank = i
                break
        if not ox:
            return None
        form = "".join(ox["results"][-5:][::-1])  # most recent first for display
        # results appended chronological; last 5 chronological then reverse for WR.. 
        form = "".join(ox["results"][-5:])
        form = form[::-1]  # newest first like SportsDB strForm
        return {
            "intRank": str(rank),
            "intPoints": str(ox["pts"]),
            "intPlayed": str(ox["played"]),
            "intWin": str(ox["won"]),
            "intDraw": str(ox["drawn"]),
            "intLoss": str(ox["lost"]),
            "intGoalsFor": str(ox["gf"]),
            "intGoalsAgainst": str(ox["ga"]),
            "intGoalDifference": str(ox["gf"] - ox["ga"]),
            "strForm": form,
            "strTeam": TEAM_NAME,
        }

    def _harvest_fixture_feed(self, force: bool = False) -> Dict[str, Any]:
        """Load League One feed once; derive standing, last, next, live."""
        matches: List[Dict] = []
        last_err = None
        for url in FIXTURE_FEED_URLS:
            try:
                # Reuse HTTP cache layer via raw requests with our budget helper shape
                cache_key = f"feed:{url}"
                cached = _HTTP_CACHE.get(cache_key)
                if cached and not force:
                    age = time.time() - float(cached.get("at") or 0)
                    if age < HTTP_CACHE_TTL_LAST:
                        matches = cached["data"]
                        break
                if not self._budget_ok() and cached:
                    matches = cached["data"]
                    break
                headers = {"User-Agent": "FiestaBoard-OxfordUnited/2.0"}
                resp = requests.get(url, headers=headers, timeout=25)
                if resp.status_code == 429:
                    self._suspend(BACKOFF_429, "HTTP 429 from fixture feed")
                    if cached:
                        matches = cached["data"]
                        break
                    resp.raise_for_status()
                resp.raise_for_status()
                matches = resp.json()
                if not isinstance(matches, list):
                    matches = []
                self._budget_consume()
                _HTTP_CACHE[cache_key] = {"at": time.time(), "data": matches}
                logger.info("Oxford fixture feed OK (%s matches) from %s", len(matches), url)
                break
            except Exception as e:
                last_err = e
                logger.warning("fixture feed failed %s: %s", url, e)
        if not matches and last_err:
            raise RuntimeError(f"Fixture feed unavailable: {last_err}")

        ox_matches = [
            m for m in matches
            if self._is_oxford_name(m.get("HomeTeam")) or self._is_oxford_name(m.get("AwayTeam"))
        ]
        standing = self._compute_standing_and_form(matches)

        finished = [
            m for m in ox_matches
            if m.get("HomeTeamScore") is not None and m.get("AwayTeamScore") is not None
        ]
        finished.sort(key=lambda m: _s(m.get("DateUtc")), reverse=True)
        scheduled = [
            m for m in ox_matches
            if m.get("HomeTeamScore") is None
        ]
        scheduled.sort(key=lambda m: _s(m.get("DateUtc")))

        last_finished = self._match_to_event(finished[0]) if finished else None
        nxt = self._match_to_event(scheduled[0]) if scheduled else None

        live = None
        for m in ox_matches:
            ev = self._match_to_event(m)
            if self._status_is_live(ev):
                live = ev
                break
            # Also: today with scores still null but within live window already set in _match_to_event
            if (ev.get("strStatus") or "").lower() in ("in play", "inplay", "live"):
                live = ev
                break

        return {
            "standing": standing,
            "last_finished": last_finished,
            "next": nxt,
            "live": live,
            "data_source": "fixturedownload",
        }

    def _http_cache_key(self, endpoint: str, params: Optional[Dict] = None) -> str:
        """Build cache key for an API path + query (params optional)."""
        ep = _s(endpoint)
        items = sorted((params or {}).items(), key=lambda x: str(x[0]))
        q = "&".join(f"{_s(k)}={_s(v)}" for k, v in items)
        return f"{ep}?{q}"

    # Back-compat alias (params must be optional — host may probe the method)
    def _cache_key(self, endpoint: str, params: Optional[Dict] = None) -> str:
        return self._http_cache_key(endpoint, params)

    def _budget_ok(self) -> bool:
        now = time.time()
        if now < float(_RATE_LIMIT.get("suspended_until") or 0):
            return False
        day = time.strftime("%Y-%m-%d", time.gmtime())
        minute = time.strftime("%Y-%m-%dT%H:%M", time.gmtime())
        if _API_BUDGET.get("day_key") != day:
            _API_BUDGET["day_key"] = day
            _API_BUDGET["day_count"] = 0
        if _API_BUDGET.get("minute_key") != minute:
            _API_BUDGET["minute_key"] = minute
            _API_BUDGET["minute_count"] = 0
        if int(_API_BUDGET.get("day_count") or 0) >= DAILY_BUDGET:
            return False
        if int(_API_BUDGET.get("minute_count") or 0) >= MINUTE_BUDGET:
            return False
        return True

    def _budget_consume(self) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        minute = time.strftime("%Y-%m-%dT%H:%M", time.gmtime())
        if _API_BUDGET.get("day_key") != day:
            _API_BUDGET["day_key"] = day
            _API_BUDGET["day_count"] = 0
        if _API_BUDGET.get("minute_key") != minute:
            _API_BUDGET["minute_key"] = minute
            _API_BUDGET["minute_count"] = 0
        _API_BUDGET["day_count"] = int(_API_BUDGET.get("day_count") or 0) + 1
        _API_BUDGET["minute_count"] = int(_API_BUDGET.get("minute_count") or 0) + 1
        global _LAST_NETWORK_AT
        _LAST_NETWORK_AT = time.time()

    def _suspend(self, seconds: int, message: str = "") -> None:
        wait = max(60, int(seconds))
        _RATE_LIMIT["suspended_until"] = time.time() + wait
        _RATE_LIMIT["message"] = (message or "rate limited")[:200]
        logger.warning("Oxford United API suspended %ss: %s", wait, _RATE_LIMIT["message"])

    def _get(
        self,
        api_key: str,
        endpoint: str,
        params: Optional[Dict] = None,
        *,
        ttl: Optional[int] = None,
        force: bool = False,
    ) -> Dict:
        """GET with response cache, budgets, and HTTP 429 backoff."""
        q = dict(params or {})
        key = self._http_cache_key(endpoint, q)
        force = force or bool(self.config.get("run_refresh_now", False))
        ttl = HTTP_CACHE_TTL if ttl is None else ttl

        cached = _HTTP_CACHE.get(key)
        if cached and not force:
            age = time.time() - float(cached.get("at") or 0)
            if age < ttl:
                return cached["data"]

        if not self._budget_ok():
            if cached:
                logger.info("Oxford API budget/suspend — serving HTTP cache for %s", endpoint)
                return cached["data"]
            raise RuntimeError(
                _RATE_LIMIT.get("message")
                or "SportsDB API budget exhausted — slowing down"
            )

        url = f"{BASE_URL.format(api_key=_s(api_key, '123'))}/{_s(endpoint)}"
        headers = {"User-Agent": "FiestaBoard-OxfordUnited/1.8"}
        try:
            resp = requests.get(url, params=q, headers=headers, timeout=15)
        except requests.RequestException as e:
            if cached:
                logger.warning("Network error %s — serving cache: %s", endpoint, e)
                return cached["data"]
            raise

        if resp.status_code == 429:
            retry_after = BACKOFF_429
            if resp.headers.get("Retry-After"):
                try:
                    retry_after = int(resp.headers["Retry-After"])
                except ValueError:
                    pass
            self._suspend(retry_after, "HTTP 429 Too Many Requests")
            if cached:
                return cached["data"]
            resp.raise_for_status()

        if resp.status_code >= 500:
            if cached:
                logger.warning("HTTP %s on %s — serving cache", resp.status_code, endpoint)
                return cached["data"]
            resp.raise_for_status()

        resp.raise_for_status()
        data = resp.json()
        self._budget_consume()
        _HTTP_CACHE[key] = {"at": time.time(), "data": data}
        # Soft slowdown as daily budget fills
        day_count = int(_API_BUDGET.get("day_count") or 0)
        if day_count >= int(DAILY_BUDGET * 0.7):
            time.sleep(0.4)
        elif day_count >= int(DAILY_BUDGET * 0.5):
            time.sleep(0.15)
        return data

    def _fetch_standing(self, api_key: str) -> Optional[Dict]:
        data = self._get(api_key, "lookuptable.php", {"l": LEAGUE_ID}, ttl=HTTP_CACHE_TTL_TABLE)
        table = data.get("table") or []
        for row in table:
            if str(row.get("idTeam")) == TEAM_ID or row.get("strTeam") == TEAM_NAME:
                return row
        return None

    def _fetch_last_events(self, api_key: str) -> List[Dict]:
        """Recent Oxford results with minimal API calls.

        1) eventslast (1 call)
        2) If newest finished is older than 3 days OR only 1 row, add eventsday for
           today + yesterday only (not 10 days).
        """
        by_id: Dict[str, Dict] = {}

        def _add(events: List[Dict]) -> None:
            for ev in events or []:
                if not ev or not self._event_involves_oxford(ev):
                    continue
                eid = str(ev.get("idEvent") or "")
                if not eid:
                    continue
                prev = by_id.get(eid)
                by_id[eid] = ev if prev is None else self._merge_event(prev, ev)

        try:
            data = self._get(
                api_key, "eventslast.php", {"id": TEAM_ID}, ttl=HTTP_CACHE_TTL_LAST
            )
            _add(list(data.get("results") or []))
        except Exception as e:
            logger.warning("eventslast failed: %s", e)

        need_days = len(by_id) < 2
        if not need_days and by_id:
            newest = max(by_id.values(), key=self._event_sort_key)
            d = (newest.get("dateEvent") or "")[:10]
            try:
                from zoneinfo import ZoneInfo
                today = datetime.now(ZoneInfo("Europe/London")).strftime("%Y-%m-%d")
            except Exception:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if d and d < today:
                # If last listed result is not today and we're within 3 days of a
                # weekend, probe today+yesterday only
                try:
                    last_d = datetime.strptime(d, "%Y-%m-%d")
                    now_d = datetime.strptime(today, "%Y-%m-%d")
                    if (now_d - last_d).days <= 5:
                        need_days = True
                except ValueError:
                    need_days = True

        if need_days and self._budget_ok():
            try:
                from zoneinfo import ZoneInfo
                base = datetime.now(ZoneInfo("Europe/London"))
            except Exception:
                base = datetime.now(timezone.utc)
            for days_ago in (0, 1, 2):  # max 3 day probes
                if not self._budget_ok():
                    break
                try:
                    day = (base - timedelta(days=days_ago)).strftime("%Y-%m-%d")
                    data = self._get(
                        api_key,
                        "eventsday.php",
                        {"d": day, "s": "Soccer", "l": LEAGUE_ID},
                        ttl=HTTP_CACHE_TTL_LAST,
                    )
                    _add(list(data.get("events") or []))
                except Exception as e:
                    logger.debug("eventsday %s: %s", days_ago, e)

        events = list(by_id.values())
        events.sort(key=self._event_sort_key, reverse=True)
        return events

    def _fetch_next_events(self, api_key: str) -> List[Dict]:
        data = self._get(api_key, "eventsnext.php", {"id": TEAM_ID})
        return list(data.get("events") or [])

    def _fetch_last_result(self, api_key: str) -> Optional[Dict]:
        return self._pick_best_last(self._fetch_last_events(api_key))

    def _fetch_next_fixture(self, api_key: str) -> Optional[Dict]:
        evs = self._fetch_next_events(api_key)
        return evs[0] if evs else None

    def _pick_best_last(self, results: List[Dict]) -> Optional[Dict]:
        """Prefer in-progress, else most recent finished with scores."""
        if not results:
            return None
        for event in results:
            if self._status_is_live(event):
                return event
        return self._pick_finished_last(results)

    def _event_sort_key(self, event: Optional[Dict]) -> str:
        """Sort key: date + time, newest first when reversed."""
        if not event:
            return ""
        d = _s(event.get("dateEvent"))[:10]
        t = _s(event.get("strTime") or event.get("strTimestamp") or "00:00")[:8]
        return f"{d}T{t}"

    def _pick_finished_last(
        self, results: List[Dict], exclude_id: str = ""
    ) -> Optional[Dict]:
        """Most recent *finished* Oxford result (never the live fixture).

        Sorted by date/time descending so last opponents stay current after each match.
        """
        if not results:
            return None
        finished: List[Dict] = []
        for event in results:
            if not event:
                continue
            eid = str(event.get("idEvent") or "")
            if exclude_id and eid and eid == exclude_id:
                continue
            if not self._event_involves_oxford(event):
                continue
            if self._status_is_live(event):
                continue
            status = (event.get("strStatus") or "").lower()
            progress = (event.get("strProgress") or "").strip().upper()
            if any(x in status for x in ("not started", "postpon", "cancel", "abandoned")):
                continue
            if progress in ("",) and "not started" in status:
                continue
            # Must have a final (or reported) score
            hs = event.get("intHomeScore")
            as_ = event.get("intAwayScore")
            if hs in (None, "") or as_ in (None, ""):
                continue
            # Prefer explicit finished; also accept older dated results with scores
            is_finished = (
                any(x in status for x in ("finish", "ft", "aet", "pen", "awarded"))
                or progress in ("FT", "AET", "PEN")
                or (
                    not self._event_is_today(event)
                    and not self._status_is_live(event)
                )
                or (
                    self._event_is_today(event)
                    and any(x in status for x in ("finish", "ft"))
                )
            )
            if not is_finished and self._event_is_today(event):
                # Today's match with score but unclear status: only if not live
                if self._status_is_live(event):
                    continue
                is_finished = True
            if is_finished:
                finished.append(event)

        if not finished:
            return None
        finished.sort(key=self._event_sort_key, reverse=True)
        return finished[0]

    def _lookup_event(self, api_key: str, event_id: str) -> Optional[Dict]:
        """Fresh single-event payload (scores, progress, goal details)."""
        try:
            data = self._get(api_key, "lookupevent.php", {"id": event_id})
            events = data.get("events") or []
            return events[0] if events else None
        except Exception as e:
            logger.warning("lookupevent %s failed: %s", event_id, e)
            return None

    @staticmethod
    def _merge_event(base: Dict, fresh: Dict) -> Dict:
        """Overlay non-empty fields from fresh lookup onto base event."""
        out = dict(base)
        for k, v in (fresh or {}).items():
            if v is None or v == "":
                continue
            out[k] = v
        return out

    def _event_involves_oxford(self, event: Optional[Dict]) -> bool:
        if not event:
            return False
        return self._is_oxford_name(event.get("strHomeTeam")) or self._is_oxford_name(
            event.get("strAwayTeam")
        )

    def _fetch_livescores_for_team(self, api_key: str) -> List[Dict]:
        """League livescores filtered to Oxford (when TheSportsDB provides them)."""
        out: List[Dict] = []
        for league_name in ("English League One", "League One"):
            try:
                data = self._get(api_key, "livescore.php", {"l": league_name})
                events = data.get("events") or data.get("livescore") or []
                for ev in events:
                    if self._event_involves_oxford(ev):
                        out.append(ev)
                if out:
                    break
            except Exception as e:
                logger.debug("livescore %s: %s", league_name, e)
        return out

    def _fetch_timeline(self, api_key: str, event_id: str) -> List[Dict]:
        """Match timeline (goals, cards, subs). Free tier works."""
        try:
            data = self._get(api_key, "lookuptimeline.php", {"id": event_id})
            return data.get("timeline") or []
        except Exception as e:
            logger.warning("Could not fetch timeline for event %s: %s", event_id, e)
            return []

    def _status_is_live(self, event: Optional[Dict]) -> bool:
        """True when TheSportsDB indicates the match is currently in progress."""
        if not event:
            return False

        status_raw = (event.get("strStatus") or "").strip()
        progress = (event.get("strProgress") or "").strip()
        status = status_raw.lower()
        prog_l = progress.lower()

        # Explicit finished / not started
        finished_tokens = (
            "match finished",
            "finished",
            "full time",
            "after et",
            "after pen",
            "awarded",
            "postponed",
            "cancelled",
            "canceled",
            "abandoned",
        )
        if any(t in status for t in finished_tokens) or status in (
            "ft",
            "aet",
            "pen",
            "aot",
        ):
            return False
        if progress.upper() in ("FT", "AET", "PEN"):
            return False
        if "not started" in status or status in ("ns", "tbd", "sched", "scheduled"):
            # Kick-off may still show NS while progress is empty
            if not (progress.endswith("'") or progress.isdigit() or prog_l in ("ht", "half")):
                return False

        # Live status strings used by TheSportsDB
        live_tokens = (
            "in play",
            "inplay",
            "in progress",
            "inprogress",
            "live",
            "kick off",
            "kickoff",
            "1st half",
            "2nd half",
            "first half",
            "second half",
            "half time",
            "halftime",
            "half-time",
            "extra time",
            "penalties",
        )
        if any(t in status for t in live_tokens):
            return True
        if any(t in prog_l for t in ("1h", "2h", "ht", "live", "1st", "2nd")):
            return True
        if progress.upper() in ("HT", "1H", "2H", "ET"):
            return True

        # Minute clock: "67'", "45+2'", "90+3"
        if progress.endswith("'") or re.match(r"^\d{1,3}(\+\d{1,2})?'?$", progress):
            return True
        if progress.isdigit() and 1 <= int(progress) <= 120:
            return True

        # Scores present while not finished and date is today (UTC/local) — weak live signal
        # handled in _resolve_live_event with date check instead

        return False

    def _event_is_today(self, event: Optional[Dict]) -> bool:
        if not event:
            return False
        d = (event.get("dateEvent") or event.get("strTimestamp") or "")[:10]
        if not d:
            return False
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            from zoneinfo import ZoneInfo
            today_uk = datetime.now(ZoneInfo("Europe/London")).strftime("%Y-%m-%d")
        except Exception:
            today_uk = today
        return d == today or d == today_uk

    def _event_minute(self, event: Optional[Dict]) -> str:
        """Match clock for display. Appends HT at half-time, FT when finished.

        Examples: 67' · 45+' HT · HT · 90+' FT · FT
        """
        if not event:
            return ""
        period = self._match_period(event)
        progress = (event.get("strProgress") or "").strip()
        minute = self._format_stoppage_minute(progress) if progress else ""
        # Strip bare HT/FT from progress so we don't double-up
        if minute.upper() in ("HT", "HALF", "FT", "FULLTIME"):
            minute = ""

        if period == "HT":
            if minute and minute.upper() not in ("HT",):
                return f"{minute} HT"[:12]
            return "HT"
        if period == "FT":
            if minute and minute.upper() not in ("FT",):
                return f"{minute} FT"[:12]
            return "FT"

        if minute:
            return minute
        status = (event.get("strStatus") or "").upper()
        if "HT" in status or "HALF TIME" in status or "HALFTIME" in status:
            return "HT"
        if "FT" in status or "FINISH" in status or "FULL TIME" in status:
            return "FT"
        return ""

    def _format_stoppage_minute(self, progress: str) -> str:
        """Normalise stoppage e.g. 90+3' / 45+2."""
        p = _s(progress).strip().replace(" ", "")
        if not p:
            return ""
        if not p.endswith("'") and re.match(r"^\d+(\+\d+)?$", p):
            p = f"{p}'"
        return p[:10]

    def _match_period(self, event: Optional[Dict]) -> str:
        """Half / phase: 1H, HT, 2H, FT."""
        if not event:
            return ""
        status = (event.get("strStatus") or "").lower()
        progress = (event.get("strProgress") or "").strip().upper()
        if any(x in status for x in ("finish", "ft", "aet", "pen")) or progress == "FT":
            return "FT"
        if "half" in status or progress in ("HT", "HALF"):
            return "HT"
        minute_raw = progress.replace("'", "")
        try:
            if "+" in minute_raw:
                base = int(minute_raw.split("+")[0] or 0)
            else:
                base = int("".join(c for c in minute_raw if c.isdigit()) or 0)
            if base == 0 and ("1h" in status or "1st" in status):
                return "1H"
            if base <= 45 or "1h" in status or "1st" in status:
                return "1H"
            return "2H"
        except ValueError:
            pass
        if "1h" in status or "1st" in status:
            return "1H"
        if "2h" in status or "2nd" in status:
            return "2H"
        if self._status_is_live(event):
            return "1H"
        return ""

    def _find_live_event(
        self,
        api_key: str,
        last: Optional[Dict],
        nxt: Optional[Dict],
    ) -> Optional[Dict]:
        """Back-compat: single last/next pair."""
        return self._resolve_live_event(
            api_key,
            [last] if last else [],
            [nxt] if nxt else [],
        )

    def _resolve_live_event(
        self,
        api_key: str,
        last_list: List[Dict],
        next_list: List[Dict],
    ) -> Optional[Dict]:
        """Find in-progress Oxford match from last, next, and league livescores."""
        # Priority: livescores → today's next/last → other last/next
        candidates: List[Dict] = []
        try:
            if self._budget_ok() and (
                self._in_scheduled_refresh_window()
                or bool(self.config.get("run_refresh_now", False))
            ):
                candidates.extend(self._fetch_livescores_for_team(api_key))
        except Exception as e:
            logger.debug("livescore scan: %s", e)
        for ev in list(next_list or []) + list(last_list or []):
            if ev:
                candidates.append(ev)

        # Dedupe by idEvent keeping first (higher priority)
        seen = set()
        unique: List[Dict] = []
        for ev in candidates:
            eid = str(ev.get("idEvent") or "")
            if eid and eid in seen:
                continue
            if eid:
                seen.add(eid)
            unique.append(ev)

        # Prefer today's Oxford fixtures first
        ordered = sorted(
            unique,
            key=lambda e: (0 if self._event_is_today(e) else 1, 0 if self._event_involves_oxford(e) else 1),
        )

        for ev in ordered:
            if not self._event_involves_oxford(ev):
                continue
            if self._status_is_live(ev):
                logger.info(
                    "Oxford live match detected: %s vs %s status=%s progress=%s score=%s-%s",
                    ev.get("strHomeTeam"),
                    ev.get("strAwayTeam"),
                    ev.get("strStatus"),
                    ev.get("strProgress"),
                    ev.get("intHomeScore"),
                    ev.get("intAwayScore"),
                )
                return ev

        # Secondary: today's fixture with scores already populated and not finished
        for ev in ordered:
            if not self._event_involves_oxford(ev) or not self._event_is_today(ev):
                continue
            status = (ev.get("strStatus") or "").lower()
            if any(x in status for x in ("finish", "ft", "postpon", "cancel", "not started")):
                continue
            if ev.get("intHomeScore") not in (None, "") or ev.get("intAwayScore") not in (
                None,
                "",
            ):
                # Enrich before accepting
                if ev.get("idEvent"):
                    fresh = self._lookup_event(api_key, str(ev["idEvent"]))
                    if fresh:
                        ev = self._merge_event(ev, fresh)
                        if self._status_is_live(ev) or (
                            (ev.get("strProgress") or "").strip()
                            and not (ev.get("strStatus") or "").lower().startswith("not")
                        ):
                            return ev
                elif self._status_is_live(ev):
                    return ev
        return None

    @staticmethod
    def _form_coloured(
        form: str,
        max_results: int = 5,
        show_letters: bool = True,
    ) -> str:
        """W/D/L form as Vestaboard colour tiles; optional W/D/L letters.

        show_letters=False → only colour flaps (green/yellow/red).
        """
        out = []
        for ch in (form or "").upper().replace(" ", "")[:max_results]:
            if ch == "W":
                out.append(f"{GREEN}W" if show_letters else GREEN)
            elif ch == "D":
                out.append(f"{YELLOW}D" if show_letters else YELLOW)
            elif ch == "L":
                out.append(f"{RED}L" if show_letters else RED)
        return "".join(out)

    @staticmethod
    def _ordinal(n: int) -> str:
        """English ordinal: 1st, 2nd, 3rd, 4th … 11th, 12th, 13th, 21st, 22nd, 23rd."""
        n = int(n)
        if 11 <= (n % 100) <= 13:
            suffix = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{suffix}"


    def _in_scheduled_refresh_window(self, now=None) -> bool:
        """True during fixed UK kick-off windows when auto-refresh is enabled.

        Saturday 15:00–17:00 and Tuesday 19:45–21:45 (Europe/London).
        """
        if not bool(self.config.get("scheduled_refresh_enabled", True)):
            return False
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Europe/London")
            now = now or datetime.now(tz)
            if now.tzinfo is None:
                now = now.replace(tzinfo=tz)
            else:
                now = now.astimezone(tz)
        except Exception:
            now = now or datetime.now(timezone.utc)

        weekday = now.weekday()  # Mon=0 … Sat=5, Tue=1
        minutes = now.hour * 60 + now.minute

        # Saturday 15:00–17:00
        if weekday == 5:
            return (15 * 60) <= minutes <= (17 * 60)
        # Tuesday 19:45–21:45
        if weekday == 1:
            return (19 * 60 + 45) <= minutes <= (21 * 60 + 45)
        return False

    def _scheduled_window_label(self, now=None) -> str:
        if not self._in_scheduled_refresh_window(now):
            return ""
        try:
            from zoneinfo import ZoneInfo
            now = now or datetime.now(ZoneInfo("Europe/London"))
            if now.weekday() == 5:
                return "SAT 15:00-17:00"
            if now.weekday() == 1:
                return "TUE 19:45-21:45"
        except Exception:
            pass
        return "SCHEDULED"

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

    def _is_goal_event(self, t: Dict) -> bool:
        """True if timeline row is a goal (handles varied TheSportsDB labels)."""
        kind = (t.get("strTimeline") or t.get("strEvent") or "").strip().lower()
        detail = (t.get("strTimelineDetail") or t.get("strDetail") or "").strip().lower()
        combined = f"{kind} {detail}"
        if any(x in combined for x in ("card", "subst", "var check", "injury")):
            if "goal" not in combined:
                return False
        if kind in GOAL_TYPES:
            return True
        if "goal" in kind or "goal" in detail or "penalty" in detail:
            return True
        return False

    def _parse_goal_details_string(
        self, details: str, is_home: bool, team: str
    ) -> List[Dict[str, Any]]:
        """Parse TheSportsDB strHomeGoalDetails / strAwayGoalDetails.

        Formats seen:
          67':Mark Harris
          10':Player1;45':Player2
          90'+2':Name
        """
        goals: List[Dict[str, Any]] = []
        if not details or not str(details).strip():
            return goals
        parts = re.split(r"[;\n]+", str(details))
        for part in parts:
            part = part.strip()
            if not part:
                continue
            m = re.match(
                r"^(?P<min>\d+)(?:\+\d+)?'?\s*:\s*(?P<player>.+)$",
                part,
            )
            if not m:
                m = re.match(
                    r"^(?P<min>\d+)(?:'\+\d+)?\s+(?P<player>.+)$",
                    part,
                )
            if not m:
                continue
            try:
                minute = int(m.group("min"))
            except ValueError:
                minute = 0
            player = m.group("player").strip()
            # Strip trailing (OG) markers for name parse; flag own goal
            is_own = "(og)" in player.lower() or "own goal" in player.lower()
            player_clean = re.sub(r"\s*\(og\)\s*", " ", player, flags=re.I).strip()
            name_info = self._name_parts(player_clean)
            goals.append(
                {
                    "minute": minute,
                    "player": player_clean,
                    "player_short": name_info["display_short"],
                    "player_display": name_info["display"],
                    "forename": name_info["forename"],
                    "initial": name_info["initial"],
                    "surname": name_info["surname"],
                    "team": team,
                    "is_home": is_home,
                    "is_own": is_own,
                    "detail": "goal_details",
                }
            )
        return goals

    def _parse_goals(
        self, timeline: List[Dict], event: Optional[Dict] = None
    ) -> List[Dict[str, Any]]:
        """Extract goals from timeline and/or event GoalDetails strings."""
        goals: List[Dict[str, Any]] = []

        for t in timeline or []:
            if not self._is_goal_event(t):
                continue
            kind = (t.get("strTimeline") or "").strip().lower()
            detail = (t.get("strTimelineDetail") or "").strip().lower()
            try:
                minute = int(float(str(t.get("intTime") or t.get("strTime") or 0)))
            except (TypeError, ValueError):
                minute = 0
            player = (
                t.get("strPlayer")
                or t.get("strPlayerName")
                or t.get("strScorer")
                or "Unknown"
            )
            player = str(player).strip()
            team = (t.get("strTeam") or "").strip()
            is_home = (t.get("strHome") or "").strip().lower() in ("yes", "1", "true", "home")
            is_own = "own" in kind or "own goal" in detail
            name_info = self._name_parts(player)
            goals.append(
                {
                    "minute": minute,
                    "player": player,
                    "player_short": name_info["display_short"],
                    "player_display": name_info["display"],
                    "forename": name_info["forename"],
                    "initial": name_info["initial"],
                    "surname": name_info["surname"],
                    "team": team,
                    "is_home": is_home,
                    "is_own": is_own,
                    "detail": t.get("strTimelineDetail") or kind,
                }
            )

        # Fallback / supplement from event goal detail strings (often populated
        # when timeline is empty on the free API during live matches).
        if event:
            home_team = event.get("strHomeTeam") or ""
            away_team = event.get("strAwayTeam") or ""
            home_details = event.get("strHomeGoalDetails") or event.get("strHomeGoals") or ""
            away_details = event.get("strAwayGoalDetails") or event.get("strAwayGoals") or ""
            extra = self._parse_goal_details_string(str(home_details), True, home_team)
            extra += self._parse_goal_details_string(str(away_details), False, away_team)
            if extra:
                # Merge by (minute, surname) avoiding duplicates
                seen = {(g["minute"], g["surname"].lower(), g["is_home"]) for g in goals}
                for g in extra:
                    key = (g["minute"], g["surname"].lower(), g["is_home"])
                    if key not in seen:
                        goals.append(g)
                        seen.add(key)

        goals.sort(key=lambda g: (g["minute"], g.get("player", "")))
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

    def _name_parts(self, full: str) -> Dict[str, str]:
        """Split player name into forename, middle initial, surname.

        Display forms:
          display       → "Mark H Harris" (forename + initial + surname)
          display_short → shorter for tight boards e.g. "M H Harris" / "Mark Harris"
        """
        if not full or not str(full).strip():
            return {
                "forename": "",
                "initial": "",
                "surname": "?",
                "display": "?",
                "display_short": "?",
            }
        parts = str(full).strip().split()
        if len(parts) == 1:
            return {
                "forename": parts[0],
                "initial": "",
                "surname": parts[0],
                "display": parts[0],
                "display_short": parts[0][:12],
            }
        forename = parts[0]
        surname = parts[-1]
        middle = parts[1:-1]
        if middle:
            # Use first middle name's initial
            initial = middle[0][0].upper()
        else:
            # No middle name: use surname initial as the "initial" slot only if
            # user asked forename, initial, surname — use forename initial
            # when no middle name: Forename + F-initial style → Forename I Surname
            # Interpretation: forename, (middle) initial, surname
            # With two parts: Forename + Surname initial + Surname → "Mark H Harris"
            initial = surname[0].upper()
        display = f"{forename} {initial} {surname}".strip()
        # Compact: initial of forename + initial + surname
        display_short = f"{forename[0]} {initial} {surname}".strip()
        if len(display_short) > 16:
            display_short = f"{forename[0]} {surname}"[:16]
        return {
            "forename": forename[:20],
            "initial": initial[:1],
            "surname": surname[:20],
            "display": display[:40],
            "display_short": display_short[:20],
        }

    def _short_name(self, full: str, max_len: int = 16) -> str:
        """Scorer label: forename + initial + surname (truncated if needed)."""
        info = self._name_parts(full)
        display = info["display"]
        if len(display) <= max_len:
            return display
        short = info["display_short"]
        if len(short) <= max_len:
            return short
        return info["surname"][:max_len]

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def _abbr(self, name: str, max_len: int = 3) -> str:
        """Always return a 3-character team abbreviation."""
        name = _s(name).strip()
        if not name:
            return "???"
        # Exact then case-insensitive known map
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
            "Birmingham City": "BIR",
            "Wrexham": "WRX",
            "Charlton Athletic": "CHA",
            "Cambridge United": "CAM",
            "Crawley Town": "CRA",
            "Shrewsbury Town": "SHR",
        }
        if name in replacements:
            return replacements[name]
        low = name.strip().lower()
        for k, v in replacements.items():
            if k.lower() == low or low in k.lower() or k.lower() in low:
                return v
        # Fallback: first letters of up to 3 words, or first 3 chars
        parts = [p for p in re.split(r"[^A-Za-z]+", name) if p]
        if len(parts) >= 3:
            short = "".join(p[0] for p in parts[:3]).upper()
        elif len(parts) == 2:
            short = (parts[0][:2] + parts[1][0]).upper()
        elif parts:
            short = parts[0][:3].upper()
        else:
            short = "???"
        short = (short + "???")[:3]
        return short

    def _abbr5(self, name: str) -> str:
        """Always return a 5-character team abbreviation (in addition to 3-char _abbr).

        Oxford is always OXFRD. Others use a known map, else consonant-heavy
        compression of the main club name (e.g. Barnsley → BRNSY / BRNSL).
        """
        name = _s(name).strip()
        if not name:
            return "?????"
        low = name.lower()
        for k, v in {
            "oxford united": "OXFRD",
            "oxford": "OXFRD",
            "barnsley": "BRNSL",
            "stockport county": "STKPT",
            "stockport": "STKPT",
            "reading": "REDNG",
            "wycombe wanderers": "WYCMB",
            "wycombe": "WYCMB",
            "peterborough united": "PTRBO",
            "peterborough": "PTRBO",
            "luton town": "LUTON",
            "luton": "LUTON",
            "huddersfield town": "HDFLD",
            "huddersfield": "HDFLD",
            "bradford city": "BRDFD",
            "bradford": "BRDFD",
            "plymouth argyle": "PLYMA",
            "plymouth": "PLYMA",
            "leicester city": "LCEST",
            "leicester": "LCEST",
            "stevenage": "STVNG",
            "wigan athletic": "WIGAN",
            "wigan": "WIGAN",
            "blackpool": "BLKPL",
            "burton albion": "BRTON",
            "burton": "BRTON",
            "exeter city": "EXETR",
            "exeter": "EXETR",
            "lincoln city": "LINCN",
            "lincoln": "LINCN",
            "northampton town": "NTHPT",
            "northampton": "NTHPT",
            "mansfield town": "MNSFD",
            "mansfield": "MNSFD",
            "rotherham united": "RTHRM",
            "rotherham": "RTHRM",
            "charlton athletic": "CHLTN",
            "charlton": "CHLTN",
            "cambridge united": "CAMBR",
            "cambridge": "CAMBR",
            "crawley town": "CRWLY",
            "crawley": "CRWLY",
            "shrewsbury town": "SHREW",
            "shrewsbury": "SHREW",
            "bolton wanderers": "BOLTN",
            "bolton": "BOLTN",
            "birmingham city": "BRMGH",
            "birmingham": "BRMGH",
            "wrexham": "WRXHM",
            "leyton orient": "LYTNO",
            "port vale": "PVALE",
            "doncaster rovers": "DONCR",
            "doncaster": "DONCR",
            "afc wimbledon": "WIMBL",
            "wimbledon": "WIMBL",
            "milton keynes dons": "MKDNS",
            "mk dons": "MKDNS",
            "bromley": "BRMLY",
            "notts county": "NOTSC",
            "sheffield wednesday": "SHFWD",
            "sheffield wed": "SHFWD",
        }.items():
            if k == low or k in low or low in k:
                return v[:5].ljust(5)[:5]

        # Intelligent: drop common suffixes, keep consonants + first vowel
        core = name
        for suffix in (
            " united", " city", " town", " athletic", " albion", " rovers",
            " wanderers", " county", " argyle", " fc", " afc",
        ):
            if core.lower().endswith(suffix):
                core = core[: -len(suffix)]
                break
        core = "".join(c for c in core.upper() if c.isalpha())
        if not core:
            return "?????"
        if len(core) <= 5:
            return core.ljust(5)[:5]
        # Prefer first letter + consonants (Barnsley → B + RNSLY → BRNSY)
        first = core[0]
        rest = core[1:]
        consonants = "".join(c for c in rest if c not in "AEIOU")
        vowels = "".join(c for c in rest if c in "AEIOU")
        body = consonants
        # If too short, weave vowels back in
        i = 0
        while len(first + body) < 5 and i < len(vowels):
            body += vowels[i]
            i += 1
        abbr = (first + body)[:5]
        return abbr.ljust(5)[:5]

    def _format_scorers_line(self, goals: List[Dict], max_items: int = 4) -> str:
        """Scorers: 'Mark H Harris 67\'' (forename initial surname + minute)."""
        if not goals:
            return ""
        bits = []
        for g in goals[:max_items]:
            name = g.get("player_display") or g.get("player_short") or g.get("player") or "?"
            own = " OG" if g.get("is_own") else ""
            bits.append(f"{_s(name)}{own} {_s(g.get('minute'))}'")
        line = "; ".join(bits)
        if len(goals) > max_items:
            line = f"{_s(line)[:40]}..."
        return _s(line)[:80]

    def _format_scorer_rows(self, goals: List[Dict], max_rows: int = 4) -> List[str]:
        """One scorer per row: minute + forename initial surname."""
        rows = []
        for g in goals[:max_rows]:
            name = _s(g.get("player_display") or g.get("player_short") or "?")
            own = " OG" if g.get("is_own") else ""
            rows.append(f"{_s(g.get('minute'))}' {name}{own}"[:45])
        return rows

    def _format_scorers_surname_line(self, goals: List[Dict], max_items: int = 4) -> str:
        """Surname only + minute: 'Harris 67'; Brannagan 12\''."""
        if not goals:
            return ""
        bits = []
        for g in goals[:max_items]:
            name = (g.get("surname") or g.get("player_short") or "?").strip()
            own = " OG" if g.get("is_own") else ""
            bits.append(f"{name}{own} {g['minute']}'")
        line = "; ".join(bits)
        if len(goals) > max_items:
            line = f"{_s(line)[:40]}..."
        return _s(line)[:80]

    def _format_scorer_surname_rows(self, goals: List[Dict], max_rows: int = 4) -> List[str]:
        """One row per scorer: minute + surname only."""
        rows = []
        for g in goals[:max_rows]:
            name = (g.get("surname") or "?").strip()
            own = " OG" if g.get("is_own") else ""
            rows.append(f"{g['minute']}' {name}{own}"[:22])
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
        last_finished: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        in_progress = live_event is not None
        live_refresh = int(self.config.get("refresh_seconds", 120) or 120)
        idle_refresh = int(self.config.get("idle_refresh_seconds", 1800) or 1800)
        # Auto live-refresh windows: Sat 15:00–17:00, Tue 19:45–21:45 UK
        scheduled_on = self._in_scheduled_refresh_window()
        active_refresh = in_progress or scheduled_on
        suggested = live_refresh if active_refresh else idle_refresh
        if in_progress:
            refresh_mode = "LIVE"
        elif scheduled_on:
            refresh_mode = "SCHEDULED"
        else:
            refresh_mode = "IDLE"

        data: Dict[str, Any] = {
            "team": TEAM_SHORT,
            "team_5": "OXFRD",
            "match_in_progress": "YES" if in_progress else "NO",
            "is_live": "YES" if in_progress else "NO",
            "current_minute": self._event_minute(live_event) if in_progress else "NO GAME",
            "run_refresh_now": "YES" if force_refresh else "NO",
            "refresh_seconds": str(live_refresh),
            "idle_refresh_seconds": str(idle_refresh),
            "suggested_refresh_seconds": str(suggested),
            "refresh_mode": refresh_mode,
            "scheduled_refresh_active": "YES" if scheduled_on else "NO",
            "scheduled_window": self._scheduled_window_label() if scheduled_on else "",
            "scheduled_refresh_enabled": (
                "YES" if bool(self.config.get("scheduled_refresh_enabled", True)) else "NO"
            ),
            # Game Status Indicator: green = live, yellow = no game
            "status_colour": "{66}" if in_progress else "{65}",
            "status_color": "{66}" if in_progress else "{65}",
            "status_colour_name": "green" if in_progress else "yellow",
            "game_status_indicator": "{66}" if in_progress else "{65}",
            "game_status_indicator_name": "green" if in_progress else "yellow",
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
            "form_coloured": "",
            "form_tiles": "",
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
            "formatted_scorers_surname": "",
            "scorers_surname": "",
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
            try:
                pos_ord = self._ordinal(int(pos)) if str(pos).strip() else "?"
            except (TypeError, ValueError):
                pos_ord = f"{pos}" if pos else "?"
            data["position_ordinal"] = pos_ord
            data["formatted_table"] = f"{TEAM_SHORT}  {pos_ord}  {pts}pts".ljust(22)[:22]
            # Form as coloured tiles: W=green D=yellow L=red
            form_str = data.get("form") or ""
            # form_style overrides form_games + form_show_letters when set
            style = str(self.config.get("form_style") or "").strip().lower()
            if style in ("last_3_tiles", "tiles_3", "3_tiles"):
                form_games, show_form_letters = 3, False
            elif style in ("last_5_tiles", "tiles_5", "5_tiles"):
                form_games, show_form_letters = 5, False
            elif style in ("last_3_letters", "letters_3", "3_letters"):
                form_games, show_form_letters = 3, True
            elif style in ("last_5_letters", "letters_5", "5_letters"):
                form_games, show_form_letters = 5, True
            else:
                form_games = int(self.config.get("form_games", 5) or 5)
                if form_games not in (3, 5):
                    form_games = 5
                show_form_letters = bool(self.config.get("form_show_letters", True))

            if form_str and len(form_str.replace(" ", "")) > form_games:
                compact = form_str.upper().replace(" ", "")
                form_str = compact[-form_games:]
                data["form"] = form_str
            if show_form and form_str:
                data["form_coloured"] = self._form_coloured(
                    form_str, max_results=form_games, show_letters=show_form_letters
                )
                data["form_tiles"] = self._form_coloured(
                    form_str, max_results=form_games, show_letters=False
                )
                data["form_letters"] = self._form_coloured(
                    form_str, max_results=form_games, show_letters=True
                )
                # Always expose last-3 tiles-only for templates
                compact_all = (data.get("form") or form_str).upper().replace(" ", "")
                last3 = compact_all[-3:] if len(compact_all) >= 3 else compact_all
                data["form_tiles_last3"] = self._form_coloured(
                    last3, max_results=3, show_letters=False
                )
            else:
                data["form_coloured"] = ""
                data["form_tiles"] = ""
                data["form_letters"] = ""
                data["form_tiles_last3"] = ""
            data["form_games"] = str(form_games)
            data["form_style"] = style or (
                f"last_{form_games}_{'letters' if show_form_letters else 'tiles'}"
            )

        # ---- Last result + HT/FT + scorers ----
        if last:
            home = _s(last.get("strHomeTeam"))
            away = _s(last.get("strAwayTeam"))
            date = last.get("dateEvent", "")
            status = last.get("strStatus") or ""

            data["last_home"] = self._abbr(home)
            data["last_away"] = self._abbr(away)
            data["last_home_5"] = self._abbr5(home)
            data["last_away_5"] = self._abbr5(away)
            data["last_date"] = date
            data["last_status"] = status

            goals = self._parse_goals(timeline, event=last)
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
            global _SCORER_CACHE
            eid = str(last.get("idEvent") or "")
            used_cache = False
            if goals:
                if eid:
                    _SCORER_CACHE[eid] = list(goals)
            elif eid and eid in _SCORER_CACHE:
                goals = list(_SCORER_CACHE[eid])
                used_cache = True
                logger.info(
                    "Oxford United: cached scorers for event %s (%s)",
                    eid,
                    len(goals),
                )

            data["scorer_count"] = str(len(goals))
            data["formatted_scorers"] = self._format_scorers_line(goals, max_scorers)
            data["scorers"] = data["formatted_scorers"]
            data["scorers_from_cache"] = "YES" if used_cache else "NO"

            scorer_rows = self._format_scorer_rows(goals, max_rows=4)
            for i, row in enumerate(scorer_rows, start=1):
                data[f"scorer_{i}"] = row

            data["formatted_scorers_surname"] = self._format_scorers_surname_line(
                goals, max_scorers
            )
            data["scorers_surname"] = data["formatted_scorers_surname"]
            surname_rows = self._format_scorer_surname_rows(goals, max_rows=4)
            for i, row in enumerate(surname_rows, start=1):
                data[f"scorer_{i}_surname"] = row
                # Also bare surname without minute
                if i <= len(goals):
                    data[f"scorer_{i}_surname_only"] = (
                        goals[i - 1].get("surname") or ""
                    )[:20]

            # Also expose structured list for advanced templates
            data["goals"] = [
                {
                    "minute": str(g["minute"]),
                    "player": g.get("player_display") or g.get("player_short") or g["player"],
                    "player_full": g["player"],
                    "forename": g.get("forename", ""),
                    "initial": g.get("initial", ""),
                    "surname": g.get("surname", ""),
                    "team": self._abbr(g["team"], 6),
                    "is_home": "Y" if g["is_home"] else "N",
                    "is_own": "Y" if g["is_own"] else "N",
                    "formatted": f"{g.get('player_display') or g.get('player_short')} {g['minute']}'",
                    "formatted_surname": f"{g.get('surname') or '?'} {g['minute']}'",
                }
                for g in goals
            ]

        # Finished match: current_minute shows FT (and optional clock)
        if last and not in_progress:
            fin_period = self._match_period(last)
            if fin_period == "FT" or any(
                x in (last.get("strStatus") or "").lower()
                for x in ("finish", "ft", "aet", "pen")
            ):
                data["current_minute"] = self._event_minute(last) or "FT"
                data["match_period"] = "FT"
                data["formatted_minute"] = data["current_minute"][:15]

        # ---- Live match extras + score/minute block ----
        if in_progress and last:
            data["live_status"] = (last.get("strStatus") or last.get("strProgress") or "LIVE")[:20]
            period = self._match_period(last)
            data["match_period"] = period
            # Single source: appends HT / FT when appropriate
            minute = _s(self._event_minute(last) or data.get("current_minute") or "")
            data["current_minute"] = minute[:15]
            if period == "HT":
                data["formatted_minute"] = minute[:15] if minute else "HT"
            elif period == "FT":
                data["formatted_minute"] = minute[:15] if minute else "FT"
            elif minute:
                data["formatted_minute"] = f"{period} {minute}".strip()[:15]
            else:
                data["formatted_minute"] = (period or "LIVE")[:15]
                minute = period or "LIVE"

            # Always prefer live event score ints (including 0-0)
            hs = self._to_int(last.get("intHomeScore"))
            as_ = self._to_int(last.get("intAwayScore"))
            if hs is not None and as_ is not None:
                score = f"{hs}-{as_}"
                data["last_score"] = score
                data["ft_score"] = score
                data["current_score"] = score
                data["home_score"] = str(hs)
                data["away_score"] = str(as_)
            else:
                score = (data.get("last_score") or data.get("ft_score") or "").strip()
                data["current_score"] = score

            home_ab = data.get("last_home") or self._abbr(last.get("strHomeTeam", ""), 6)
            away_ab = data.get("last_away") or self._abbr(last.get("strAwayTeam", ""), 6)

            data["formatted_live"] = f"{score} {minute}".strip()[:22]

            # Dedicated live summary block (Note-friendly 15×3)
            # Row1: LIVE           Row2: OXF 1-0 BRA    Row3: 67'
            r1 = "{66} LIVE".ljust(14)  # 1 colour tile + text
            if score:
                r2 = f"{home_ab} {score} {away_ab}".strip()
                r2 = r2[:15].center(15)[:15] if len(r2) < 15 else r2[:15]
            else:
                r2 = f"{home_ab} v {away_ab}"[:15].ljust(15)
            r3 = str(minute)[:15].center(15)[:15]
            data["live_row1"] = r1.ljust(15)[:15]
            data["live_row2"] = r2.ljust(15)[:15]
            data["live_row3"] = r3.ljust(15)[:15]
            data["live_match_block"] = f"{data['live_row1']}\n{data['live_row2']}\n{data['live_row3']}"
            # Single-line summary: "1-0 67'" or "OXF 1-0 BRA 67'"
            line = f"{score} {minute}".strip()
            data["live_score_minute"] = line[:22]
            data["live_summary"] = f"{home_ab} {score} {away_ab} {minute}".strip()[:45]
        else:
            data["live_status"] = ""
            data["formatted_live"] = ""
            data["live_row1"] = ""
            data["live_row2"] = ""
            data["live_row3"] = ""
            data["live_match_block"] = ""
            data["live_score_minute"] = ""
            data["live_summary"] = ""
            data["current_score"] = ""
            data["home_score"] = ""
            data["away_score"] = ""
            # Finished games may already have set current_minute to FT; otherwise NO GAME
            if not (data.get("current_minute") or "").strip() or data.get("current_minute") == "NO GAME":
                if data.get("match_period") == "FT" or (data.get("current_minute") or "").endswith("FT"):
                    pass  # keep FT from finished-game handling above
                else:
                    data["current_minute"] = "NO GAME"
                    data["formatted_minute"] = "NO GAME"
                    data["match_period"] = data.get("match_period") or ""
            if not data.get("formatted_minute"):
                data["formatted_minute"] = data.get("current_minute") or "NO GAME"

        # ---- Next fixture (+ Note block when not live) ----
        if nxt:
            # .get(k, "") still returns None when key exists with null from JSON
            home = _s(nxt.get("strHomeTeam"))
            away = _s(nxt.get("strAwayTeam"))
            date = _s(nxt.get("dateEvent"))
            time_str = _s(nxt.get("strTime"))[:5]

            data["next_home"] = self._abbr(home)
            data["next_away"] = self._abbr(away)
            data["next_home_5"] = self._abbr5(home)
            data["next_away_5"] = self._abbr5(away)
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

            # Kick-off countdown when next fixture is today (UK-oriented local)
            data["kickoff_in_minutes"] = ""
            data["kickoff_countdown"] = ""
            try:
                if date and len(date) >= 10:
                    tparts = (time_str or "00:00").split(":")
                    hh, mm = int(tparts[0]), int(tparts[1]) if len(tparts) > 1 else 0
                    kick = datetime(
                        int(date[0:4]), int(date[5:7]), int(date[8:10]),
                        hh, mm, tzinfo=timezone.utc,
                    )
                    # Treat API time as UK local approximately via Europe/London if available
                    try:
                        from zoneinfo import ZoneInfo
                        kick = kick.replace(tzinfo=None)
                        kick = datetime(
                            int(date[0:4]), int(date[5:7]), int(date[8:10]), hh, mm,
                            tzinfo=ZoneInfo("Europe/London"),
                        )
                        now_local = datetime.now(ZoneInfo("Europe/London"))
                    except Exception:
                        now_local = datetime.now(timezone.utc)
                        if kick.tzinfo is None:
                            kick = kick.replace(tzinfo=timezone.utc)
                    delta_m = int((kick - now_local).total_seconds() // 60)
                    if 0 <= delta_m <= 24 * 60:
                        data["kickoff_in_minutes"] = str(delta_m)
                        if delta_m == 0:
                            data["kickoff_countdown"] = "KICK OFF"
                        elif delta_m < 60:
                            data["kickoff_countdown"] = f"KO in {delta_m}m"
                        else:
                            data["kickoff_countdown"] = f"KO in {delta_m // 60}h{delta_m % 60:02d}m"
            except Exception:
                pass

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

        # Primary board block: live scores vs idle + next fixture
        if in_progress:
            data["match_block"] = (
                data.get("live_score_minute")
                or data.get("formatted_live")
                or data.get("formatted_last")
                or ""
            )
            data["display_mode"] = "LIVE"
            data["no_game_message"] = ""
            data["status_message"] = data["match_block"]
            data["idle_row1"] = ""
            data["idle_row2"] = ""
            data["idle_row3"] = ""
            data["idle_match_block"] = ""
        else:
            idle = "No game at the moment"
            data["no_game_message"] = idle
            data["display_mode"] = "IDLE"

            # Keep next fixture fields from above; build combined Note 15×3:
            # Row1: No game now (abbrev)  Row2: NEXT H OPP  Row3: date time
            ha = data.get("next_ha") or ""
            opp = data.get("next_opponent") or data.get("next_opponent_full") or ""
            opp_short = self._truncate(str(opp).upper(), 10)
            date = data.get("next_date") or ""
            time_str = data.get("next_time") or ""
            date_disp = date
            try:
                if date and len(date) >= 10:
                    dt = datetime.strptime(date[:10], "%Y-%m-%d")
                    date_disp = dt.strftime("%d %b").upper()
            except ValueError:
                date_disp = date[:9]

            r1 = "{65} No game"  # Game Status Indicator yellow + text
            if ha and opp_short:
                r2 = f"NEXT {ha} {opp_short}".strip()[:15].ljust(15)
            elif data.get("formatted_next"):
                r2 = str(data["formatted_next"])[:15].ljust(15)
            else:
                r2 = "NEXT TBA".ljust(15)[:15]
            when = f"{date_disp} {time_str}".strip()[:15]
            r3 = when.ljust(15)[:15] if when else "".ljust(15)

            data["idle_row1"] = r1
            data["idle_row2"] = r2
            data["idle_row3"] = r3
            data["idle_match_block"] = f"{r1}\n{r2}\n{r3}"

            # next_* rows mirror idle combined block
            data["next_row1"] = r1
            data["next_row2"] = r2
            data["next_row3"] = r3
            data["next_match_block"] = data["idle_match_block"]

            # One-line status: message + next details
            if ha and opp_short:
                detail = f"Next: {ha} {opp_short} {when}".strip()
                data["status_message"] = self._truncate(f"{idle}. {detail}", 60)
                data["match_block"] = data["status_message"][:45]
            else:
                data["status_message"] = idle
                data["match_block"] = idle

            data["next_match_line"] = self._truncate(
                f"NEXT {ha} {opp_short} {when}".strip(), 45
            ) if (ha or opp_short or when) else data.get("next_match_line", "")

        # Streamlined blocks: league_summary / last_game / match_in_progress / next_game
        data = self._finalize_data_blocks(
            data,
            in_progress=in_progress,
            last_finished=last_finished or (last if not in_progress else None),
            live_event=live_event,
            nxt=nxt,
            max_scorers=max_scorers,
        )
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

    def _note3(self, r1: str, r2: str, r3: str) -> Dict[str, str]:
        r1 = _s(r1)[:22]
        r2 = _s(r2)[:15].ljust(15)[:15]
        r3 = _s(r3)[:15].ljust(15)[:15]
        row1 = r1[:22] if "{6" in r1 else r1[:15].ljust(15)[:15]
        return {
            "row1": row1,
            "row2": r2,
            "row3": r3,
            "block": f"{row1}\n{r2}\n{r3}",
        }

    def _finalize_data_blocks(
        self,
        data: Dict[str, Any],
        *,
        in_progress: bool,
        last_finished: Optional[Dict],
        live_event: Optional[Dict],
        nxt: Optional[Dict],
        max_scorers: int,
    ) -> Dict[str, Any]:
        """Expose four clear blocks while keeping legacy field names."""

        # ---- league_summary (table + form) ----
        data["league_summary_position"] = data.get("position") or ""
        data["league_summary_position_ordinal"] = data.get("position_ordinal") or ""
        data["league_summary_points"] = data.get("points") or ""
        data["league_summary_played"] = data.get("played") or ""
        data["league_summary_won"] = data.get("won") or ""
        data["league_summary_drawn"] = data.get("drawn") or ""
        data["league_summary_lost"] = data.get("lost") or ""
        data["league_summary_gf"] = data.get("gf") or ""
        data["league_summary_ga"] = data.get("ga") or ""
        data["league_summary_gd"] = data.get("gd") or ""
        data["league_summary_form"] = data.get("form") or ""
        data["league_summary_form_coloured"] = data.get("form_coloured") or ""
        data["league_summary_form_tiles"] = data.get("form_tiles") or ""
        data["league_summary_form_tiles_last3"] = data.get("form_tiles_last3") or ""
        data["league_summary_line"] = data.get("formatted_table") or ""
        ls = self._note3(
            data["league_summary_line"] or f"{TEAM_SHORT} LEAGUE",
            (data.get("form_tiles_last3") or data.get("form_coloured") or data.get("form") or "")[:15],
            f"P{data.get('played') or '-'} W{data.get('won') or '-'} D{data.get('drawn') or '-'}"[:15],
        )
        data["league_summary_note_row1"] = ls["row1"]
        data["league_summary_note_row2"] = ls["row2"]
        data["league_summary_note_row3"] = ls["row3"]
        data["league_summary_note_block"] = ls["block"]

        # ---- last_game (finished only; always from last_finished event) ----
        lg_home = lg_away = lg_score = lg_date = lg_result = ""
        if last_finished and not self._status_is_live(last_finished):
            lg_home = self._abbr(last_finished.get("strHomeTeam") or "")
            lg_away = self._abbr(last_finished.get("strAwayTeam") or "")
            hs = self._to_int(last_finished.get("intHomeScore"))
            as_ = self._to_int(last_finished.get("intAwayScore"))
            if hs is not None and as_ is not None:
                lg_score = f"{hs}-{as_}"
            lg_date = (last_finished.get("dateEvent") or "")[:10]
            is_home = self._is_oxford_name(last_finished.get("strHomeTeam"))
            if hs is not None and as_ is not None:
                if hs == as_:
                    lg_result = "D"
                elif (is_home and hs > as_) or (not is_home and as_ > hs):
                    lg_result = "W"
                else:
                    lg_result = "L"
            # Keep legacy last_* in sync with last_game when idle
            if not in_progress:
                data["last_home"] = lg_home
                data["last_away"] = lg_away
                data["last_score"] = lg_score
                data["last_date"] = lg_date
                data["last_result"] = lg_result
                data["formatted_last"] = (
                    f"{lg_home} {lg_score} {lg_away}".strip()[:22] if lg_score else ""
                )

        data["last_game_home"] = lg_home
        data["last_game_away"] = lg_away
        data["last_game_home_5"] = (
            self._abbr5(last_finished.get("strHomeTeam"))
            if last_finished and not self._status_is_live(last_finished)
            else ""
        )
        data["last_game_away_5"] = (
            self._abbr5(last_finished.get("strAwayTeam"))
            if last_finished and not self._status_is_live(last_finished)
            else ""
        )
        data["last_game_opponent_5"] = ""
        if last_finished and not self._status_is_live(last_finished):
            if self._is_oxford_name(last_finished.get("strHomeTeam")):
                data["last_game_opponent_5"] = data["last_game_away_5"]
            else:
                data["last_game_opponent_5"] = data["last_game_home_5"]
        data["last_game_score"] = lg_score
        data["last_game_date"] = lg_date
        data["last_game_result"] = lg_result
        data["last_game_ht_score"] = data.get("ht_score") or "" if not in_progress else ""
        data["last_game_scorers"] = data.get("formatted_scorers") or "" if not in_progress else ""
        data["last_game_line"] = (
            f"{lg_home} {lg_score} {lg_away}".strip() if lg_score else f"{lg_home} v {lg_away}".strip()
        )[:22]
        lg_note = self._note3(
            "LAST GAME",
            data["last_game_line"][:15],
            (lg_date or lg_result or "")[:15],
        )
        data["last_game_note_row1"] = lg_note["row1"]
        data["last_game_note_row2"] = lg_note["row2"]
        data["last_game_note_row3"] = lg_note["row3"]
        data["last_game_note_block"] = lg_note["block"]
        data["last_game_available"] = "YES" if (lg_home or lg_score) else "NO"

        # ---- match_in_progress ----
        if in_progress and live_event:
            mip_home = data.get("last_home") or self._abbr(live_event.get("strHomeTeam") or "")
            mip_away = data.get("last_away") or self._abbr(live_event.get("strAwayTeam") or "")
            mip_score = data.get("current_score") or data.get("last_score") or ""
            mip_minute = data.get("current_minute") or self._event_minute(live_event)
            mip_period = data.get("match_period") or self._match_period(live_event)
            data["match_in_progress_home"] = mip_home
            data["match_in_progress_away"] = mip_away
            data["match_in_progress_home_5"] = self._abbr5(live_event.get("strHomeTeam"))
            data["match_in_progress_away_5"] = self._abbr5(live_event.get("strAwayTeam"))
            data["match_in_progress_score"] = mip_score
            data["match_in_progress_minute"] = mip_minute
            data["match_in_progress_period"] = mip_period
            data["match_in_progress_scorers"] = data.get("formatted_scorers") or ""
            data["match_in_progress_line"] = f"{mip_score} {mip_minute}".strip()[:22]
            data["match_in_progress_available"] = "YES"
            mip_note = self._note3(
                "{66} LIVE",
                f"{mip_home} {mip_score} {mip_away}".strip()[:15],
                (mip_minute or mip_period or "")[:15],
            )
            data["match_in_progress_note_row1"] = mip_note["row1"]
            data["match_in_progress_note_row2"] = mip_note["row2"]
            data["match_in_progress_note_row3"] = mip_note["row3"]
            data["match_in_progress_note_block"] = mip_note["block"]
            # Align legacy live block with this
            data["live_match_block"] = data["match_in_progress_note_block"]
            data["live_row1"] = data["match_in_progress_note_row1"]
            data["live_row2"] = data["match_in_progress_note_row2"]
            data["live_row3"] = data["match_in_progress_note_row3"]
        else:
            for k in (
                "home", "away", "score", "minute", "period", "scorers", "line",
            ):
                data[f"match_in_progress_{k}"] = ""
            data["match_in_progress_available"] = "NO"
            data["match_in_progress_minute"] = "NO GAME"
            data["current_minute"] = data.get("current_minute") or "NO GAME"
            idle = self._note3("NO GAME", "AT THE MOMENT", "")
            data["match_in_progress_note_row1"] = idle["row1"]
            data["match_in_progress_note_row2"] = idle["row2"]
            data["match_in_progress_note_row3"] = idle["row3"]
            data["match_in_progress_note_block"] = idle["block"]

        # ---- next_game ----
        data["next_game_home"] = data.get("next_home") or (
            self._abbr(nxt.get("strHomeTeam") or "") if nxt else ""
        )
        data["next_game_away"] = data.get("next_away") or (
            self._abbr(nxt.get("strAwayTeam") or "") if nxt else ""
        )
        data["next_game_home_5"] = data.get("next_home_5") or (
            self._abbr5(nxt.get("strHomeTeam") or "") if nxt else ""
        )
        data["next_game_away_5"] = data.get("next_away_5") or (
            self._abbr5(nxt.get("strAwayTeam") or "") if nxt else ""
        )
        data["next_game_opponent_5"] = ""
        if nxt:
            if self._is_oxford_name(nxt.get("strHomeTeam")):
                data["next_game_opponent_5"] = data["next_game_away_5"]
            else:
                data["next_game_opponent_5"] = data["next_game_home_5"]
        data["next_game_opponent"] = data.get("next_opponent") or ""
        data["next_game_date"] = data.get("next_date") or ""
        data["next_game_time"] = data.get("next_time") or ""
        data["next_game_home_away"] = data.get("next_home_away") or ""
        data["next_game_line"] = data.get("formatted_next") or ""
        data["next_game_available"] = "YES" if (data["next_game_home"] or data["next_game_opponent"]) else "NO"
        ng = self._note3(
            "NEXT GAME",
            (data["next_game_line"] or f"{data['next_game_home']} v {data['next_game_away']}")[:15],
            f"{data['next_game_date']} {data['next_game_time']}".strip()[:15],
        )
        data["next_game_note_row1"] = ng["row1"]
        data["next_game_note_row2"] = ng["row2"]
        data["next_game_note_row3"] = ng["row3"]
        data["next_game_note_block"] = ng["block"]

        return data

