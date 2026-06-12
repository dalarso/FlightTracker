"""Tests for the sports-bucket FIX findings.

Covers (one focused test per behavioural finding):
  #1  celebration_frame must yield the canvas to flights ("flights always win"), and the
      board-side _sports_score flight guard must sit ABOVE the celebration block.
  #3  per-slot runtime state must fully reset when a NEW game_id occupies the slot (so a
      back-to-back / doubleheader game 2 isn't poisoned by game 1's frozen win/score state),
      while a transient parse-miss game_id 0 must NOT wipe live state.
  #4  multiple goals inside one celebration window each nudge the LAN horn (the on-panel
      scroll stays a single coalesced celebration, but the listener logs every goal).
  #5  _send_state must not mis-report a stale/expired FINAL the process restarted INTO as a
      fresh post-game FINAL on the first tick.
  #7  the MLB fetcher must surface a LIVE doubleheader game 2 instead of pinning to game 1.
  #8  the NHL fetcher passes gameState='CRIT' straight through (CRIT is a first-class state
      downstream but only ever originates from NHL).

scenes/sportscore.py imports rgbmatrix (LED hardware) at module top, so we stub it before
import exactly like tests/test_sportscore_scene.py.  FT_DATA_DIR is pointed at a temp dir so
nothing touches the repo DB on import.  The scene's per-tick bookkeeping is driven directly
(no network): each slot's poll_due is parked far in the future so _poll_slot_async never
fires, and game state is set by assigning slot["game"] — exactly the atomic dict swap the
background worker performs.
"""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT / "scenes"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("FT_DATA_DIR", tempfile.mkdtemp(prefix="ft-sports-test-"))

# Stub the LED-matrix lib so sportscore.py (and the setup/ colour+font modules it imports)
# loads on a dev box / CI.  MagicMock turns graphics.* into no-ops.
try:
    import rgbmatrix  # noqa: F401
except Exception:
    _g = MagicMock(name="rgbmatrix.graphics")
    _rgb = types.ModuleType("rgbmatrix")
    _rgb.graphics = _g
    sys.modules["rgbmatrix"] = _rgb
    sys.modules["rgbmatrix.graphics"] = _g

import sportscore  # noqa: E402  (scenes/sportscore.py)
from utilities import nhl, mlb  # noqa: E402


# ── Shared test doubles (mirrors tests/test_sportscore_scene.py) ─────────────────
class _Clock:
    """Hand-cranked stand-in for time.time(): starts at t0, only advances on .advance()."""
    def __init__(self, t0=1_000_000.0):
        self.t = float(t0)

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs
        return self.t


def _make_slot(key="NHL", team_name="VGK", celebrate=True,
               celebration_text="{team} GOAL!"):
    """One sport slot with the exact static+runtime key set _build_slots() produces."""
    def _never_fetch(*_a, **_k):
        raise AssertionError("fetch_fn must not be called in these unit tests")

    return {
        "key":              key,
        "enabled":          True,
        "team_id":          54,
        "team_name":        team_name,
        "fetch_fn":         _never_fetch,
        "score_font":       sportscore._SCORE_FONT,
        "score_char_px":    sportscore._SCORE_CHAR_PX,
        "celebrate":        celebrate,
        "celebration_text": celebration_text,
        "game":            None,
        "poll_due":        0.0,
        "game_ended_at":   None,
        "last_team_score": None,
        "last_draw":       None,
        "was_live":        False,
        "win_shown":       False,
        "last_game_id":    None,
        "_inflight":       False,
    }


def _game(state, team_score=0, opp_score=0, opp_abbr="EDM",
          period_label="", period_type="REG", period=1, game_id=None):
    g = {
        "state":           state,
        "team_score":      team_score,
        "opp_score":       opp_score,
        "opp_abbr":        opp_abbr,
        "period_label":    period_label,
        "period_type":     period_type,
        "period":          period,
        "in_intermission": False,
    }
    if game_id is not None:
        g["game_id"] = game_id
    return g


class _SceneHarness:
    """SportScoreScene wired with host attributes (canvas / _data / reset_scene /
    _reset_idle_scenes), a patched clock, and a captured _send_horn — same approach as
    tests/test_sportscore_scene.py but exposing _data so flight-priority can be driven."""
    def __init__(self, slots, t0=1_000_000.0, flights=None):
        self.clock = _Clock(t0)
        self.horn_calls = []
        self._patchers = []
        self.scene = sportscore.SportScoreScene.__new__(sportscore.SportScoreScene)
        sportscore.SportScoreScene.__init__(self.scene)
        self.scene._sport_slots = slots
        for s in slots:
            s["poll_due"] = self.clock.t + 1e9
        self.scene.canvas = MagicMock(name="canvas")
        self.scene._data = list(flights or [])
        self.scene.reset_scene = MagicMock(name="reset_scene")
        self.scene._reset_idle_scenes = MagicMock(name="_reset_idle_scenes")

    def __enter__(self):
        def _cap(kind, team, tscore, opp, oscore):
            self.horn_calls.append((kind, team, tscore, opp, oscore))
        self._patchers = [
            mock.patch.object(sportscore.time, "time", self.clock),
            mock.patch.object(sportscore, "_send_horn", _cap),
            mock.patch.object(sportscore, "_send_state", lambda *_a, **_k: None),
        ]
        for p in self._patchers:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patchers:
            p.stop()
        return False

    def tick(self):
        self.scene._sports_score(count=0)

    def celebration_tick(self, count=0):
        self.scene.celebration_frame(count)

    def set_game(self, slot, game):
        slot["game"] = game

    @property
    def horn_kinds(self):
        return [c[0] for c in self.horn_calls]


# ── #1  Flights always win — celebration yields the canvas ───────────────────────
class CelebrationYieldsToFlights(unittest.TestCase):
    def test_celebration_frame_does_not_clear_canvas_while_flights_overhead(self):
        # Arm a celebration, then a plane appears overhead.  celebration_frame must return
        # WITHOUT Clear()-ing the canvas so the flight scenes own the display.
        slot = _make_slot()
        with _SceneHarness([slot]) as h:
            h.scene._goal_celebration_active = True
            h.scene._celebration_until = h.clock.t + 100   # celebration in progress
            h.scene._data = [{"callsign": "AAL1", "hex": "abc"}]  # plane overhead
            h.scene.canvas.Clear.reset_mock()
            h.celebration_tick(0)
        h.scene.canvas.Clear.assert_not_called()   # flight scenes keep the canvas

    def test_board_flight_guard_runs_before_celebration_block(self):
        # _sports_score must treat flights as top priority even when a celebration is
        # active: with a plane overhead it resets sport draws and returns, rather than
        # running the celebration early-return branch.
        slot = _make_slot()
        with _SceneHarness([slot]) as h:
            h.scene._goal_celebration_active = True
            h.scene._celebration_until = h.clock.t + 100
            h.scene._data = [{"callsign": "UAL2", "hex": "def"}]
            with mock.patch.object(h.scene, "_reset_sport_draws") as reset_draws:
                h.tick()
            reset_draws.assert_called_once()   # took the flight-priority path

    def test_celebration_frame_clears_when_no_flights(self):
        # Sanity: with no flights the celebration still draws (Clear is called) so the fix
        # only suppresses drawing while a plane is overhead.
        slot = _make_slot()
        with _SceneHarness([slot]) as h:
            h.scene._goal_celebration_active = True   # already initialised → skip init block
            h.scene._celebration_until = h.clock.t + 100
            h.scene._celebration_text = "VGK GOAL!"
            h.scene._celebration_scroll_x = 10        # real ints so the wrap check works
            h.scene._celebration_tw = 60
            h.scene._data = []
            h.scene.canvas.Clear.reset_mock()
            h.celebration_tick(0)
        h.scene.canvas.Clear.assert_called()


# ── #3  New game_id fully resets per-slot state ──────────────────────────────────
class NewGameIdResetsState(unittest.TestCase):
    def test_new_game_id_rearms_win_and_score_baseline(self):
        # Game 1 ends as a displayed WIN.  A different game_id then occupies the slot
        # (FUT) — win_shown/last_team_score/game_ended_at/was_live must fully reset so
        # game 2's WIN can fire and its goals are detected from a fresh baseline.
        slot = _make_slot(team_name="VGK")
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("FUT", team_score=0, game_id=111))
            h.tick()
            h.set_game(slot, _game("LIVE", team_score=3, opp_score=2,
                                    period_label="3rd", game_id=111))
            h.tick()
            h.set_game(slot, _game("FINAL", team_score=3, opp_score=2, game_id=111))
            h.tick()
            self.assertTrue(slot["win_shown"])
            self.assertEqual([c[0] for c in h.horn_calls].count("WIN"), 1)

            # Game 2 arrives — DIFFERENT game_id, FUT.  Despite was_live being True from
            # game 1, the id change must reset everything.
            h.set_game(slot, _game("FUT", team_score=0, game_id=222))
            h.tick()
            self.assertFalse(slot["win_shown"], "win_shown leaked from game 1")
            self.assertFalse(slot["was_live"], "was_live leaked from game 1")
            self.assertIsNone(slot["game_ended_at"], "game_ended_at leaked from game 1")
            self.assertEqual(slot["last_game_id"], 222)
            self.assertEqual(slot["last_team_score"], 0)   # fresh baseline from game 2 FUT

            # Game 2 goes LIVE and the team wins → a SECOND WIN must fire.
            h.set_game(slot, _game("LIVE", team_score=5, opp_score=1,
                                    period_label="3rd", game_id=222))
            h.tick()
            h.set_game(slot, _game("FINAL", team_score=5, opp_score=1, game_id=222))
            h.tick()
        self.assertEqual([c[0] for c in h.horn_calls].count("WIN"), 2)

    def test_game_id_zero_does_not_masquerade_as_new_game(self):
        # A transient parse miss yields game_id 0 (the `or 0` fetcher fallback).  That must
        # NOT be treated as a new game and wipe the live state we already track.
        slot = _make_slot(team_name="VGK")
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("FUT", team_score=0, game_id=111))
            h.tick()
            h.set_game(slot, _game("LIVE", team_score=2, opp_score=1,
                                    period_label="2nd", game_id=111))
            h.tick()
            self.assertTrue(slot["was_live"])
            self.assertEqual(slot["last_game_id"], 111)
            # Same game, but a momentary parse miss reports id 0.
            h.set_game(slot, _game("LIVE", team_score=2, opp_score=1,
                                    period_label="2nd", game_id=0))
            h.tick()
            self.assertTrue(slot["was_live"], "id 0 wiped live state")
            self.assertEqual(slot["last_game_id"], 111, "id 0 overwrote the real id")


# ── #4  Each goal during a celebration still nudges the LAN horn ──────────────────
class MultipleGoalsDuringCelebration(unittest.TestCase):
    def test_second_goal_during_celebration_fires_horn(self):
        slot = _make_slot(celebration_text="{team} GOAL!")
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("FUT", team_score=0, game_id=7))
            h.tick()
            h.set_game(slot, _game("LIVE", team_score=0, period_label="1st", game_id=7))
            h.tick()
            # First goal arms the celebration (one horn).  _goal_celebration_active is
            # flipped by celebration_frame; we set it directly to model the celebration
            # already being in progress without exercising the scroll-draw geometry.
            h.set_game(slot, _game("LIVE", team_score=1, period_label="1st", game_id=7))
            h.tick()
            self.assertEqual(h.horn_kinds, ["GOAL"])
            self.assertEqual(slot["last_team_score"], 1)
            h.scene._goal_celebration_active = True

            # A SECOND goal lands while the celebration is still active.  _sports_score
            # takes the celebration branch but must still nudge the LAN horn for the goal.
            h.set_game(slot, _game("LIVE", team_score=2, period_label="1st", game_id=7))
            h.tick()
        self.assertEqual(h.horn_kinds, ["GOAL", "GOAL"])
        # The second nudge carries the new score and did NOT re-arm a second scroll
        # (last_team_score advanced to 2 so no coalesced re-fire after the celebration).
        self.assertEqual(h.horn_calls[1][2], 2)
        self.assertEqual(slot["last_team_score"], 2)

    def test_no_extra_scroll_refire_after_celebration_for_in_window_goal(self):
        # After the celebration ends, the goal that landed during it must NOT trigger a
        # fresh celebration (it was already nudged + baseline advanced).
        slot = _make_slot()
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("FUT", team_score=0, game_id=7))
            h.tick()
            h.set_game(slot, _game("LIVE", team_score=0, period_label="1st", game_id=7))
            h.tick()
            h.set_game(slot, _game("LIVE", team_score=1, period_label="1st", game_id=7))
            h.tick()                                   # arms celebration #1
            h.scene._goal_celebration_active = True    # celebration in progress
            h.set_game(slot, _game("LIVE", team_score=2, period_label="1st", game_id=7))
            h.tick()                                   # 2nd goal nudged mid-celebration
            # End the celebration and run a normal tick on the same (2) score.
            h.clock.advance(sportscore.GOAL_CELEBRATION_SECONDS + 1)
            h.scene._goal_celebration_active = False   # celebration_frame would clear this
            h.tick()
        # Only the two mid-window goals nudged; no third (re-fire) horn.
        self.assertEqual(h.horn_kinds, ["GOAL", "GOAL"])


# ── #5  _send_state must not report a stale FINAL on the first tick ──────────────
class SendStateStaleFinal(unittest.TestCase):
    # These call the REAL _send_state directly (the scene harness stubs it to a no-op, so
    # it is not used here).  The horn socket is None on a dev box, so we patch a fake socket
    # to capture the wire payload and reset the ping throttle so the heartbeat fires.
    _NOW = 1_000_000.0

    def _run_send_state(self, slot):
        sent = []

        class _Sock:
            def sendto(self, data, addr):
                sent.append(data.decode("utf-8"))

        with mock.patch.object(sportscore, "_horn_sock", _Sock()), \
             mock.patch.object(sportscore, "_last_horn_ping", 0.0):
            sportscore._send_state([slot], self._NOW)
        return sent

    def test_send_state_does_not_report_expired_final_after_restart(self):
        # Process restarted hours after the buzzer: game_ended_at is stamped (mirroring the
        # pre-stamp _sports_score now does before _send_state) but well outside the window.
        slot = _make_slot(team_name="VGK")
        slot["game"] = _game("FINAL", team_score=4, opp_score=1, game_id=9)
        slot["game_ended_at"] = self._NOW - (sportscore.POST_GAME_SECONDS + 3600)
        sent = self._run_send_state(slot)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0], "STATE|NONE")   # expired FINAL → not reported

    def test_send_state_reports_fresh_final_within_window(self):
        # A genuinely just-ended FINAL (stamped ~now) is still reported.
        slot = _make_slot(team_name="VGK")
        slot["game"] = _game("FINAL", team_score=4, opp_score=1, opp_abbr="EDM", game_id=9)
        slot["game_ended_at"] = self._NOW   # just ended this tick
        sent = self._run_send_state(slot)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("STATE|FINAL|VGK|4|EDM|1|"))

    def test_send_state_reports_live_regardless_of_ended_at(self):
        # A LIVE game is always reportable (the stale-FINAL guard only affects Pass-2).
        slot = _make_slot(team_name="VGK")
        slot["game"] = _game("LIVE", team_score=2, opp_score=1, opp_abbr="EDM",
                              period_label="2nd", game_id=9)
        sent = self._run_send_state(slot)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("STATE|LIVE|VGK|2|EDM|1|"))


# ── #7  MLB doubleheader: a LIVE game 2 must beat a FINAL game 1 ─────────────────
class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


def _mlb_dh_game(gpk, abstract, inning, team_score, opp_score, team_id=119):
    t = {"team": {"id": team_id, "abbreviation": "LAD"}, "score": team_score}
    o = {"team": {"id": 137, "abbreviation": "SF"}, "score": opp_score}
    return {
        "gamePk": gpk, "gameDate": "2026-06-03T02:00:00Z",
        "status": {"abstractGameState": abstract},
        "linescore": {"currentInning": inning, "isTopInning": True, "inningHalf": "Top"},
        "teams": {"home": t, "away": o},
    }


class MlbDoubleheader(unittest.TestCase):
    def _run(self, games, team_id=119):
        payload = {"dates": [{"games": games}]}

        class _S:
            def get(self, url, **kw):
                return _Resp(payload)

        with mock.patch.object(mlb, "_get_session", return_value=_S()):
            return mlb.fetch_mlb_game(team_id, tz=None)

    def test_live_game2_beats_final_game1(self):
        # Game 1 (earlier in array) is FINAL; game 2 is LIVE.  The fetcher must surface the
        # LIVE game 2, not pin to the first (FINAL) match.
        g1 = _mlb_dh_game(745, "Final", 9, 2, 5)
        g2 = _mlb_dh_game(746, "Live",  4, 1, 0)
        g = self._run([g1, g2])
        self.assertEqual(g["state"], "LIVE")
        self.assertEqual(g["game_id"], 746)

    def test_single_game_day_unchanged(self):
        g = self._run([_mlb_dh_game(745, "Live", 7, 5, 3)])
        self.assertEqual(g["state"], "LIVE")
        self.assertEqual(g["game_id"], 745)

    def test_pre_game2_surfaces_after_final_game1(self):
        # Game 1 FINAL, game 2 not yet started (Preview) — the upcoming game surfaces so the
        # slot keeps polling rather than sleeping until midnight.
        g1 = _mlb_dh_game(745, "Final",   9, 2, 5)
        g2 = _mlb_dh_game(746, "Preview", 0, 0, 0)
        g = self._run([g1, g2])
        self.assertEqual(g["state"], "FUT")
        self.assertEqual(g["game_id"], 746)

    def test_both_final_picks_later_game(self):
        # Both games FINAL — the later game (game 2) wins the post-game window.
        g1 = _mlb_dh_game(745, "Final", 9, 2, 5)
        g2 = _mlb_dh_game(746, "Final", 9, 6, 1)
        g = self._run([g1, g2])
        self.assertEqual(g["state"], "FINAL")
        self.assertEqual(g["game_id"], 746)


# ── #8  NHL fetcher passes gameState='CRIT' straight through ─────────────────────
class _NhlResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class NhlCritState(unittest.TestCase):
    def _run(self, state, team_id=54):
        import datetime
        today = datetime.date.today().isoformat()
        game = {
            "gameState": state, "id": 2024020600,
            "startTimeUTC": "2026-06-03T02:00:00Z",
            "homeTeam": {"id": 54, "score": 2, "abbrev": "VGK"},
            "awayTeam": {"id": 99, "score": 2, "abbrev": "EDM"},
            "periodDescriptor": {"number": 3, "periodType": "REG"},
            "clock": {"timeRemaining": "0:48", "inIntermission": False},
        }
        payload = {"gamesByDate": [{"date": today, "games": [game]}]}

        class _S:
            def get(self, url, **kw):
                return _NhlResp(payload)

        with mock.patch.object(nhl, "_get_session", return_value=_S()):
            return nhl.fetch_game(team_id, tz=None)

    def test_crit_state_passes_through(self):
        g = self._run("CRIT")
        self.assertIsNotNone(g)
        self.assertEqual(g["state"], "CRIT")
        self.assertEqual((g["team_score"], g["opp_score"]), (2, 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
