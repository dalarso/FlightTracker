"""Tests for the sports fetchers (utilities/nhl, espn, mlb) and the scoreboard
orchestrator (web/scoreboard_data._fetch_scoreboard_data).

These parse external league-API JSON — exactly the code that breaks silently when a
provider tweaks its response shape — so each fetcher runs against a recorded-shape
payload with the HTTP boundary (_get_session) mocked (no network), and the orchestrator
runs with the fetchers mocked to verify the enabled / priority / LIVE-over-FINAL
selection.  Dates are computed the same way the fetchers compute "today", so the suite
is deterministic on any run date.
"""
import datetime
import sys
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
_WEB = _ROOT / "web"
for _p in (str(_WEB), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from utilities import nhl, espn, mlb   # noqa: E402
import scoreboard_data                 # noqa: E402

_PACIFIC = ZoneInfo("America/Los_Angeles")


def _today_iso():
    return datetime.date.today().isoformat()


class _Resp:
    def __init__(self, status, payload, raise_json=False):
        self.status_code = status
        self._payload = payload
        self._raise = raise_json

    def json(self):
        if self._raise:
            raise ValueError("bad json")
        return self._payload


def _session(status=200, payload=None, raise_json=False, capture=None):
    """Stand-in for a requests.Session whose .get() returns a canned response."""
    class _S:
        def get(self, url, **kw):
            if capture is not None:
                capture.update(url=url, kw=kw)
            return _Resp(status, payload if payload is not None else {}, raise_json)
    return _S()


# ── NHL ─────────────────────────────────────────────────────────────────────────
def _nhl_game(team_id=54, home=True, team_score=3, opp_score=2, opp="EDM",
              state="LIVE", number=2, ptype="REG", clock=None, gid=2024020500):
    t = {"id": team_id, "score": team_score, "abbrev": "VGK"}
    o = {"id": 99, "score": opp_score, "abbrev": opp}
    return {
        "gameState": state, "id": gid, "startTimeUTC": "2026-06-03T02:00:00Z",
        "homeTeam": t if home else o, "awayTeam": o if home else t,
        "periodDescriptor": {"number": number, "periodType": ptype},
        "clock": clock if clock is not None else {"timeRemaining": "12:34", "inIntermission": False},
    }


def _nhl_payload(game, date_str=None):
    return {"gamesByDate": [{"date": date_str or _today_iso(), "games": [game]}]}


class NhlFetch(unittest.TestCase):
    def _run(self, payload, status=200, team_id=54, raise_json=False):
        with mock.patch.object(nhl, "_get_session",
                               return_value=_session(status, payload, raise_json)):
            return nhl.fetch_game(team_id, tz=None)

    def test_home_team_live_game(self):
        g = self._run(_nhl_payload(_nhl_game(home=True)))
        self.assertIsNotNone(g)
        self.assertEqual(g["state"], "LIVE")
        self.assertEqual((g["team_score"], g["opp_score"]), (3, 2))
        self.assertEqual(g["opp_abbr"], "EDM")
        self.assertTrue(g["team_home"])
        self.assertEqual(g["period_label"], "2nd")
        self.assertEqual(g["game_id"], 2024020500)

    def test_away_team_scores_resolved(self):
        g = self._run(_nhl_payload(_nhl_game(home=False, team_score=1, opp_score=4)))
        self.assertFalse(g["team_home"])
        self.assertEqual((g["team_score"], g["opp_score"]), (1, 4))

    def test_no_game_today_filtered_out(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        self.assertIsNone(self._run(_nhl_payload(_nhl_game(), date_str=yesterday)))

    def test_team_not_in_todays_games(self):
        self.assertIsNone(self._run(_nhl_payload(_nhl_game(team_id=54)), team_id=12))

    def test_non_200_returns_none(self):
        self.assertIsNone(self._run({}, status=503))

    def test_empty_payload_returns_none(self):
        self.assertIsNone(self._run({}))

    def test_malformed_json_returns_none(self):
        self.assertIsNone(self._run(None, raise_json=True))

    def test_period_label_variants(self):
        self.assertEqual(nhl._period_label(2, "REG", False), "2nd")
        self.assertEqual(nhl._period_label(2, "REG", True), "2nd INT")
        self.assertEqual(nhl._period_label(4, "OT", False), "OT")    # 1st OT
        self.assertEqual(nhl._period_label(5, "OT", False), "2OT")   # double OT
        self.assertEqual(nhl._period_label(6, "OT", False), "3OT")   # triple OT (Cup final)
        self.assertEqual(nhl._period_label(0, "SO", False), "SO")

    def test_game_start_local_formats(self):
        # 02:00 UTC on a June date → 19:00 PDT the prior evening.
        self.assertEqual(nhl.game_start_local("2026-06-03T02:00:00Z", _PACIFIC), "7:00 PM")
        self.assertEqual(nhl.game_start_local("2026-06-03T02:00:00Z", _PACIFIC, "24h"), "19:00")
        self.assertEqual(nhl.game_start_local("not-a-time", _PACIFIC), "")


# ── ESPN (NFL / NBA / MLS) ──────────────────────────────────────────────────────
def _espn_event(team_id=1, home=True, team_score=21, opp_score=14, opp_abbr="KC",
                status_name="STATUS_IN_PROGRESS", period=2, eid=401, clock="5:00"):
    team_c = {"team": {"id": str(team_id), "abbreviation": "LV"},
              "score": str(team_score), "homeAway": "home" if home else "away"}
    opp_c = {"team": {"id": "999", "abbreviation": opp_abbr},
             "score": str(opp_score), "homeAway": "away" if home else "home"}
    comps = [team_c, opp_c] if home else [opp_c, team_c]
    return {
        "id": str(eid), "date": "2026-06-03T02:00:00Z",
        "status": {"type": {"name": status_name}, "period": period, "displayClock": clock},
        "competitions": [{"competitors": comps}],
    }


class EspnFetch(unittest.TestCase):
    def _run(self, payload, sport="football/nfl", team_id=1, status=200, raise_json=False):
        with mock.patch.object(espn, "_get_session",
                               return_value=_session(status, payload, raise_json)):
            return espn.fetch_espn_game(sport, team_id, tz=_PACIFIC)

    def test_missing_team_id_returns_none_without_fetch(self):
        called = {"n": 0}
        def boom():
            called["n"] += 1
            return _session(200, {})
        with mock.patch.object(espn, "_get_session", side_effect=boom):
            self.assertIsNone(espn.fetch_espn_game("football/nfl", 0, tz=None))
        self.assertEqual(called["n"], 0)   # short-circuits before any HTTP

    def test_nfl_live_game(self):
        g = self._run({"events": [_espn_event(status_name="STATUS_IN_PROGRESS", period=2)]})
        self.assertEqual(g["state"], "LIVE")
        self.assertEqual((g["team_score"], g["opp_score"]), (21, 14))
        self.assertEqual(g["opp_abbr"], "KC")
        self.assertTrue(g["team_home"])
        self.assertEqual(g["period_label"], "Q2")
        self.assertFalse(g["in_intermission"])

    def test_final_state_mapped(self):
        g = self._run({"events": [_espn_event(status_name="STATUS_FINAL")]})
        self.assertEqual(g["state"], "FINAL")

    def test_halftime_is_live_and_intermission(self):
        g = self._run({"events": [_espn_event(status_name="STATUS_HALFTIME", period=2)]})
        self.assertEqual(g["state"], "LIVE")
        self.assertTrue(g["in_intermission"])
        self.assertEqual(g["period_label"], "HALF")

    def test_away_team_resolved(self):
        g = self._run({"events": [_espn_event(home=False, team_score=3, opp_score=10)]})
        self.assertFalse(g["team_home"])
        self.assertEqual((g["team_score"], g["opp_score"]), (3, 10))

    def test_team_not_found_returns_none(self):
        self.assertIsNone(self._run({"events": [_espn_event(team_id=1)]}, team_id=777))

    def test_non_200_returns_none(self):
        self.assertIsNone(self._run({}, status=500))

    def test_malformed_json_returns_none(self):
        self.assertIsNone(self._run(None, raise_json=True))

    def test_period_labels_per_sport(self):
        self.assertEqual(espn._label_nfl(1, "STATUS_HALFTIME"), "HALF")
        self.assertEqual(espn._label_nfl(3, "STATUS_END_PERIOD"), "Q3 END")
        self.assertEqual(espn._label_nfl(5, "STATUS_IN_PROGRESS"), "OT")
        self.assertEqual(espn._label_nba(4, "STATUS_IN_PROGRESS"), "Q4")
        self.assertEqual(espn._label_nba(5, "STATUS_IN_PROGRESS"), "OT")
        self.assertEqual(espn._label_nba(6, "STATUS_IN_PROGRESS"), "OT2")
        self.assertEqual(espn._label_mls(1, "STATUS_IN_PROGRESS"), "1st")
        self.assertEqual(espn._label_mls(2, "STATUS_HALFTIME"), "HLF")
        self.assertEqual(espn._label_mls(3, "STATUS_IN_PROGRESS"), "ET")


# ── MLB ─────────────────────────────────────────────────────────────────────────
def _mlb_game(team_id=119, home=True, team_score=5, opp_score=3, opp_abbr="SF",
              abstract="Live", inning=7, is_top=True, inning_half="Top", gpk=745):
    t = {"team": {"id": team_id, "abbreviation": "LAD"}, "score": team_score}
    o = {"team": {"id": 137, "abbreviation": opp_abbr}, "score": opp_score}
    return {
        "gamePk": gpk, "gameDate": "2026-06-03T02:00:00Z",
        "status": {"abstractGameState": abstract},
        "linescore": {"currentInning": inning, "isTopInning": is_top, "inningHalf": inning_half},
        "teams": {"home": t if home else o, "away": o if home else t},
    }


def _mlb_payload(game):
    return {"dates": [{"games": [game]}]}


class MlbFetch(unittest.TestCase):
    def _run(self, payload, team_id=119, status=200, raise_json=False):
        with mock.patch.object(mlb, "_get_session",
                               return_value=_session(status, payload, raise_json)):
            return mlb.fetch_mlb_game(team_id, tz=_PACIFIC)

    def test_missing_team_id_returns_none(self):
        self.assertIsNone(mlb.fetch_mlb_game(0, tz=None))

    def test_live_top_inning(self):
        g = self._run(_mlb_payload(_mlb_game(inning=7, is_top=True, inning_half="Top")))
        self.assertEqual(g["state"], "LIVE")
        self.assertEqual(g["period_label"], "TOP 7")
        self.assertEqual((g["team_score"], g["opp_score"]), (5, 3))
        self.assertEqual(g["opp_abbr"], "SF")
        self.assertFalse(g["in_intermission"])

    def test_bottom_inning_label(self):
        g = self._run(_mlb_payload(_mlb_game(inning=3, is_top=False, inning_half="Bottom")))
        self.assertEqual(g["period_label"], "BOT 3")

    def test_mid_inning_is_intermission(self):
        g = self._run(_mlb_payload(_mlb_game(inning_half="Middle")))
        self.assertTrue(g["in_intermission"])

    def test_final_regulation_label(self):
        g = self._run(_mlb_payload(_mlb_game(abstract="Final", inning=9)))
        self.assertEqual(g["state"], "FINAL")
        self.assertEqual(g["period_label"], "FINAL")

    def test_final_extra_innings_label(self):
        g = self._run(_mlb_payload(_mlb_game(abstract="Final", inning=11)))
        self.assertEqual(g["period_label"], "F/11")
        self.assertEqual(g["period_type"], "OT")

    def test_preview_state(self):
        g = self._run(_mlb_payload(_mlb_game(abstract="Preview", inning=0)))
        self.assertEqual(g["state"], "FUT")

    def test_away_team_resolved(self):
        g = self._run(_mlb_payload(_mlb_game(home=False, team_score=1, opp_score=8)))
        self.assertFalse(g["team_home"])
        self.assertEqual((g["team_score"], g["opp_score"]), (1, 8))

    def test_team_not_found_returns_none(self):
        self.assertIsNone(self._run(_mlb_payload(_mlb_game(team_id=119)), team_id=999))

    def test_no_dates_returns_none(self):
        self.assertIsNone(self._run({"dates": []}))

    def test_non_200_returns_none(self):
        self.assertIsNone(self._run({}, status=503))

    def test_malformed_json_returns_none(self):
        self.assertIsNone(self._run(None, raise_json=True))


# ── Scoreboard orchestrator (_fetch_scoreboard_data) ────────────────────────────
class ScoreboardFetch(unittest.TestCase):
    def setUp(self):
        self._orig_rc = scoreboard_data.read_config
        self._orig = {k: getattr(scoreboard_data, k)
                      for k in ("_sb_fetch_nhl", "_sb_fetch_espn", "_sb_fetch_mlb")}

    def tearDown(self):
        scoreboard_data.read_config = self._orig_rc
        for k, v in self._orig.items():
            setattr(scoreboard_data, k, v)

    def _cfg(self, **kw):
        scoreboard_data.read_config = lambda: dict(kw)

    def test_master_switch_off_short_circuits(self):
        self._cfg(SCOREBOARD_ENABLED=False)
        calls = []
        scoreboard_data._sb_fetch_nhl = lambda *a, **k: calls.append(1)
        self.assertEqual(scoreboard_data._fetch_scoreboard_data(), (None, "VGK", "NHL", False))
        self.assertEqual(calls, [])   # returns before any fetch

    def test_nothing_configured_not_enabled(self):
        self._cfg(SCOREBOARD_ENABLED=True)   # master on but no team IDs
        game, _name, _key, enabled = scoreboard_data._fetch_scoreboard_data()
        self.assertIsNone(game)
        self.assertFalse(enabled)

    def test_live_game_selected(self):
        self._cfg(SCOREBOARD_ENABLED=True, SCOREBOARD_NHL_ENABLED=True,
                  SCOREBOARD_NHL_TEAM_ID=54, SCOREBOARD_NHL_TEAM_NAME="VGK",
                  SCOREBOARD_PRIORITY=["NHL"])
        scoreboard_data._sb_fetch_nhl = lambda tid, tz: {"state": "LIVE", "team_score": 2}
        game, name, key, enabled = scoreboard_data._fetch_scoreboard_data()
        self.assertEqual(game["state"], "LIVE")
        self.assertEqual((name, key, enabled), ("VGK", "NHL", True))

    def test_live_beats_final_regardless_of_priority(self):
        # NHL (higher priority) FINAL; NFL (lower) LIVE → the LIVE game wins.
        self._cfg(SCOREBOARD_ENABLED=True,
                  SCOREBOARD_NHL_ENABLED=True, SCOREBOARD_NHL_TEAM_ID=54, SCOREBOARD_NHL_TEAM_NAME="VGK",
                  SCOREBOARD_NFL_ENABLED=True, SCOREBOARD_NFL_TEAM_ID=1, SCOREBOARD_NFL_TEAM_NAME="LV",
                  SCOREBOARD_PRIORITY=["NHL", "NFL"])
        scoreboard_data._sb_fetch_nhl = lambda tid, tz: {"state": "FINAL"}
        scoreboard_data._sb_fetch_espn = lambda path, tid, tz: {"state": "LIVE"}
        game, name, key, _enabled = scoreboard_data._fetch_scoreboard_data()
        self.assertEqual(game["state"], "LIVE")
        self.assertEqual((name, key), ("LV", "NFL"))

    def test_priority_order_breaks_live_ties(self):
        # Both LIVE; priority lists NFL before NHL → NFL wins.
        self._cfg(SCOREBOARD_ENABLED=True,
                  SCOREBOARD_NHL_ENABLED=True, SCOREBOARD_NHL_TEAM_ID=54, SCOREBOARD_NHL_TEAM_NAME="VGK",
                  SCOREBOARD_NFL_ENABLED=True, SCOREBOARD_NFL_TEAM_ID=1, SCOREBOARD_NFL_TEAM_NAME="LV",
                  SCOREBOARD_PRIORITY=["NFL", "NHL"])
        scoreboard_data._sb_fetch_nhl = lambda tid, tz: {"state": "LIVE"}
        scoreboard_data._sb_fetch_espn = lambda path, tid, tz: {"state": "LIVE"}
        _game, name, key, _enabled = scoreboard_data._fetch_scoreboard_data()
        self.assertEqual((name, key), ("LV", "NFL"))

    def test_final_fallback_when_no_live(self):
        self._cfg(SCOREBOARD_ENABLED=True, SCOREBOARD_NHL_ENABLED=True,
                  SCOREBOARD_NHL_TEAM_ID=54, SCOREBOARD_NHL_TEAM_NAME="VGK",
                  SCOREBOARD_PRIORITY=["NHL"])
        scoreboard_data._sb_fetch_nhl = lambda tid, tz: {"state": "FINAL"}
        game, _name, key, _enabled = scoreboard_data._fetch_scoreboard_data()
        self.assertEqual(game["state"], "FINAL")
        self.assertEqual(key, "NHL")

    def test_disabled_sport_not_fetched(self):
        self._cfg(SCOREBOARD_ENABLED=True,
                  SCOREBOARD_NHL_ENABLED=False, SCOREBOARD_NHL_TEAM_ID=54,
                  SCOREBOARD_PRIORITY=["NHL"])
        calls = []
        scoreboard_data._sb_fetch_nhl = lambda tid, tz: calls.append(1)
        game, _name, _key, enabled = scoreboard_data._fetch_scoreboard_data()
        self.assertIsNone(game)
        self.assertFalse(enabled)     # NHL disabled → nothing enabled
        self.assertEqual(calls, [])

    def test_nhl_legacy_team_id_fallback(self):
        # Legacy SCOREBOARD_TEAM_ID maps onto NHL when SCOREBOARD_NHL_TEAM_ID is absent.
        self._cfg(SCOREBOARD_ENABLED=True, SCOREBOARD_TEAM_ID=54, SCOREBOARD_TEAM_NAME="VGK",
                  SCOREBOARD_PRIORITY=["NHL"])
        seen = {}
        def fake(tid, tz):
            seen["tid"] = tid
            return {"state": "LIVE"}
        scoreboard_data._sb_fetch_nhl = fake
        _game, name, _key, enabled = scoreboard_data._fetch_scoreboard_data()
        self.assertEqual(seen["tid"], 54)
        self.assertEqual(name, "VGK")
        self.assertTrue(enabled)

    def test_fetcher_exception_is_swallowed(self):
        self._cfg(SCOREBOARD_ENABLED=True, SCOREBOARD_NHL_ENABLED=True,
                  SCOREBOARD_NHL_TEAM_ID=54, SCOREBOARD_NHL_TEAM_NAME="VGK",
                  SCOREBOARD_PRIORITY=["NHL"])
        def boom(tid, tz):
            raise RuntimeError("league API down")
        scoreboard_data._sb_fetch_nhl = boom
        game, _name, _key, enabled = scoreboard_data._fetch_scoreboard_data()
        self.assertIsNone(game)        # no crash
        self.assertTrue(enabled)       # still "configured", just no data this poll


if __name__ == "__main__":
    unittest.main(verbosity=2)
