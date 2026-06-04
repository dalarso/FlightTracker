"""Tests for the render-thread logic in scenes/sportscore.py (SportScoreScene).

The fetchers (utilities/nhl,espn,mlb) are covered by tests/test_sports.py and the
web/scoreboard_data orchestrator by tests/test_web.py.  This file targets the scene's
OWN per-frame bookkeeping in ``SportScoreScene._sports_score`` — the goal/score-increase
horn, the one-shot WIN celebration (gated on ``was_live``), the post-game display window
arming/expiry, and the ESPN rain-delay guard that must NOT wipe ``was_live`` — none of
which the fetcher/orchestrator tests touch.

Approach
--------
* scenes/sportscore.py imports rgbmatrix (LED hardware) at module top, so we stub it
  before import exactly like tests/test_weather.py.  The renderer's graphics.* calls
  become MagicMock no-ops.
* We never hit the network: each slot's ``poll_due`` is pushed far into the future so
  ``_poll_slot_async`` (the only thing that calls a fetch_fn) never fires.  Instead we
  drive game state by assigning ``slot["game"]`` directly between ticks — which is exactly
  what the background worker does (a single atomic dict swap), so the per-tick logic under
  test sees the same thing it would in production.
* ``time.time()`` is patched to a hand-cranked clock (``_Clock``) so the ~30-min post-game
  window can be crossed deterministically with no real sleeping.
* ``_send_horn`` / ``_send_state`` are module-level functions referenced by bare name
  inside ``_sports_score``; we patch ``scenes.sportscore._send_horn`` to capture GOAL/WIN
  nudges and assert on them, and stub ``_send_state`` to a no-op (its own wire format is a
  separate concern; on a dev box the horn socket is None so it is already inert).

What is NOT unit-tested here (and why)
--------------------------------------
* The celebration SCROLL animation (``celebration_frame`` / ``_draw_score`` pixel layout)
  is pure rgbmatrix draw-call geometry against a mocked canvas — there is no observable
  state to assert without a real panel, so we assert the celebration *arming* (text,
  ``_celebration_until``, horn) which is the testable logic that drives it.
* ``_send_state``'s STATE|… wire string is left to a future fetcher-style payload test;
  here it is stubbed so it can't interfere with horn-call capture.
"""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT / "scenes"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Stub the LED-matrix lib so sportscore.py (and the setup/ colour+font modules it imports)
# loads on a dev box / CI.  MagicMock turns graphics.Color/Font/LoadFont/DrawText into
# no-ops.  The real rgbmatrix is used on the Pi.
try:
    import rgbmatrix  # noqa: F401
except Exception:
    _g = MagicMock(name="rgbmatrix.graphics")
    _rgb = types.ModuleType("rgbmatrix")
    _rgb.graphics = _g
    sys.modules["rgbmatrix"] = _rgb
    sys.modules["rgbmatrix.graphics"] = _g

import sportscore  # noqa: E402  (scenes/sportscore.py)


# ── Test doubles ────────────────────────────────────────────────────────────────
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
    """Build one sport slot with the exact static+runtime key set that
    sportscore._build_slots() produces (static block + _runtime template).

    fetch_fn is a sentinel that raises if ever called — the test drives game state by
    assigning slot["game"] directly, so a real fetch must never happen.
    """
    def _never_fetch(*_a, **_k):
        raise AssertionError("fetch_fn must not be called in these unit tests")

    return {
        # ── static config (mirrors _build_slots all_slots[...] entries) ──
        "key":              key,
        "enabled":          True,
        "team_id":          54,
        "team_name":        team_name,
        "fetch_fn":         _never_fetch,
        "score_font":       sportscore._SCORE_FONT,
        "score_char_px":    sportscore._SCORE_CHAR_PX,
        "celebrate":        celebrate,
        "celebration_text": celebration_text,
        # ── runtime state (mirrors _build_slots._runtime) ──
        "game":            None,
        "poll_due":        0.0,
        "game_ended_at":   None,
        "last_team_score": None,
        "last_draw":       None,
        "was_live":        False,
        "win_shown":       False,
        "_inflight":       False,
    }


def _game(state, team_score=0, opp_score=0, opp_abbr="EDM",
          period_label="", period_type="REG", period=1):
    return {
        "state":           state,
        "team_score":      team_score,
        "opp_score":       opp_score,
        "opp_abbr":        opp_abbr,
        "period_label":    period_label,
        "period_type":     period_type,
        "period":          period,
        "in_intermission": False,
    }


class _SceneHarness:
    """Owns a SportScoreScene wired with the host attributes that the live MatrixDisplay
    normally supplies (canvas / _data / reset_scene / _reset_idle_scenes) plus a patched
    clock and captured _send_horn, then drives ticks.

    Use as a context manager so the time/_send_horn/_send_state patches are scoped.
    """
    def __init__(self, slots, t0=1_000_000.0, flights=None):
        self.clock = _Clock(t0)
        self.horn_calls = []          # list of (kind, team, tscore, opp, oscore)
        self._patchers = []
        self.scene = sportscore.SportScoreScene.__new__(sportscore.SportScoreScene)
        # Run __init__ to set the celebration/active fields, then override slots.
        sportscore.SportScoreScene.__init__(self.scene)
        self.scene._sport_slots = slots
        # Park every slot's next poll far in the future so _poll_slot_async never fires.
        for s in slots:
            s["poll_due"] = self.clock.t + 1e9
        # Host-supplied attributes (provided by MatrixDisplay at runtime).
        self.scene.canvas = MagicMock(name="canvas")
        self.scene._data = list(flights or [])      # flights override path
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

    # ── driving ──
    def tick(self):
        """One score keyframe at the current clock time."""
        self.scene._sports_score(count=0)

    def set_game(self, slot, game):
        slot["game"] = game

    @property
    def horn_kinds(self):
        return [c[0] for c in self.horn_calls]


# ── 1. GOAL / score-increase detection ──────────────────────────────────────────
class GoalDetection(unittest.TestCase):
    def test_goal_horn_fires_on_score_increase_while_live(self):
        slot = _make_slot(celebration_text="{team} GOAL!")
        with _SceneHarness([slot]) as h:
            # Baseline must be armed via a pre-game tick so the FIRST goal counts
            # (last_team_score starts None; FUT/PRE captures the 0 baseline).
            h.set_game(slot, _game("FUT", team_score=0))
            h.tick()
            self.assertEqual(slot["last_team_score"], 0)

            h.set_game(slot, _game("LIVE", team_score=0, period_label="1st"))
            h.tick()
            self.assertEqual(h.horn_calls, [])      # no scoring yet

            # Score goes 0 -> 1 between polls during a LIVE game → GOAL.
            h.set_game(slot, _game("LIVE", team_score=1, opp_score=0,
                                    opp_abbr="EDM", period_label="1st"))
            h.tick()
        self.assertEqual(len(h.horn_calls), 1)
        kind, team, tscore, opp, oscore = h.horn_calls[0]
        self.assertEqual(kind, "GOAL")
        self.assertEqual(team, "VGK")
        self.assertEqual(tscore, 1)               # carries the new score
        self.assertEqual((opp, oscore), ("EDM", 0))
        # Celebration armed with the team-formatted text.
        self.assertEqual(h.scene._celebration_text, "VGK GOAL!")
        self.assertGreater(h.scene._celebration_until, h.clock.t)

    def test_no_horn_when_score_unchanged(self):
        slot = _make_slot()
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("FUT", team_score=2))   # join mid? no: arms baseline=2
            h.tick()
            h.set_game(slot, _game("LIVE", team_score=2, period_label="2nd"))
            h.tick()
            h.set_game(slot, _game("LIVE", team_score=2, period_label="2nd"))
            h.tick()
        self.assertEqual(h.horn_calls, [])

    def test_no_horn_when_not_live_even_if_score_rises(self):
        # A score that only appears once the game is already FINAL must not fire GOAL
        # (goal detection is gated on state in LIVE/CRIT).
        slot = _make_slot()
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("FUT", team_score=0))
            h.tick()
            # Jump straight to FINAL with a higher score (no LIVE tick in between).
            h.set_game(slot, _game("FINAL", team_score=3, opp_score=1))
            h.tick()
        self.assertNotIn("GOAL", h.horn_kinds)

    def test_no_goal_horn_when_celebrate_false(self):
        # NBA mutes its frequent goal celebrations (celebrate=False) — a score increase
        # must NOT fire a GOAL horn (the WIN path is tested separately).
        slot = _make_slot(key="NBA", team_name="LAL", celebrate=False,
                          celebration_text="")
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("FUT", team_score=0))
            h.tick()
            h.set_game(slot, _game("LIVE", team_score=2, period_label="Q1"))
            h.tick()
        self.assertEqual(h.horn_calls, [])

    def test_crit_state_also_counts_as_live_for_goal(self):
        slot = _make_slot()
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("FUT", team_score=0))
            h.tick()
            h.set_game(slot, _game("CRIT", team_score=0, period_label="3rd"))
            h.tick()
            h.set_game(slot, _game("CRIT", team_score=1, period_label="3rd"))
            h.tick()
        self.assertEqual(h.horn_kinds, ["GOAL"])


# ── 2. One-shot WIN celebration gated on was_live ────────────────────────────────
class WinCelebration(unittest.TestCase):
    def test_live_then_final_fires_win_once(self):
        slot = _make_slot(team_name="VGK")
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("FUT", team_score=0))
            h.tick()
            h.set_game(slot, _game("LIVE", team_score=3, opp_score=2, period_label="3rd"))
            h.tick()
            self.assertTrue(slot["was_live"])
            # Final with the tracked team ahead → WIN.
            h.set_game(slot, _game("FINAL", team_score=3, opp_score=2, opp_abbr="EDM"))
            h.tick()
            win_calls = [c for c in h.horn_calls if c[0] == "WIN"]
            self.assertEqual(len(win_calls), 1)
            self.assertEqual(win_calls[0][1], "VGK")
            self.assertEqual(h.scene._celebration_text, "VGK WINS!")
            self.assertTrue(slot["win_shown"])

            # Re-ticking FINAL must NOT re-fire (one-shot).
            h.tick()
            h.tick()
        self.assertEqual([c for c in h.horn_calls if c[0] == "WIN"], win_calls)

    def test_never_live_fut_to_final_does_not_fire_win(self):
        # FUT -> FINAL with no LIVE tick (e.g. process started after the game ended, or a
        # postponed/forfeit jump): was_live stays False → no WIN false-fire.
        slot = _make_slot(team_name="VGK")
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("FUT", team_score=0))
            h.tick()
            self.assertFalse(slot["was_live"])
            h.set_game(slot, _game("FINAL", team_score=5, opp_score=1))
            h.tick()
            h.tick()
        self.assertEqual([c for c in h.horn_calls if c[0] == "WIN"], [])
        self.assertFalse(slot["win_shown"])

    def test_loss_does_not_fire_win(self):
        # Watched LIVE, but the tracked team LOST → no WIN (win requires team_score > opp).
        slot = _make_slot(team_name="VGK")
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("FUT", team_score=0))
            h.tick()
            h.set_game(slot, _game("LIVE", team_score=1, opp_score=2, period_label="3rd"))
            h.tick()
            h.set_game(slot, _game("FINAL", team_score=1, opp_score=2))
            h.tick()
        self.assertEqual([c for c in h.horn_calls if c[0] == "WIN"], [])

    def test_win_fires_for_non_celebrate_sport(self):
        # The WIN is NOT gated by the per-sport `celebrate` flag — NBA still gets a WIN.
        slot = _make_slot(key="NBA", team_name="LAL", celebrate=False, celebration_text="")
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("FUT", team_score=0))
            h.tick()
            h.set_game(slot, _game("LIVE", team_score=100, opp_score=98, period_label="Q4"))
            h.tick()
            h.set_game(slot, _game("FINAL", team_score=100, opp_score=98))
            h.tick()
        win_calls = [c for c in h.horn_calls if c[0] == "WIN"]
        self.assertEqual(len(win_calls), 1)
        self.assertEqual(win_calls[0][1], "LAL")


# ── 3. Post-game window arming / expiry ──────────────────────────────────────────
class PostGameWindow(unittest.TestCase):
    # NOTE on isolation: a *winning* FINAL also arms the WIN celebration, which makes
    # _sports_score early-return (yielding the canvas to celebration_frame) so the FINAL
    # is NOT drawn as the active slot until the celebration lapses.  To test the post-game
    # window/draw path cleanly we use a *loss* (no celebration fires), so the active-slot
    # draw block runs every tick and _scoreboard_active/_active_slot reflect the window
    # directly.  test_winning_game_post_game_then_expiry below covers the win-path timeline.

    def test_final_arms_window_and_stays_active_then_expires(self):
        slot = _make_slot(team_name="VGK")
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("FUT", team_score=0))
            h.tick()
            # Team trailing the whole game → a loss → no GOAL (score never exceeds the 0
            # baseline) and no WIN, so the FINAL is drawn as the active slot each tick.
            h.set_game(slot, _game("LIVE", team_score=0, opp_score=2, period_label="3rd"))
            h.tick()
            self.assertTrue(h.scene._scoreboard_active)
            self.assertIs(h.scene._active_slot, slot)

            # First FINAL tick stamps game_ended_at = now and keeps the board active.
            t_final = h.clock.t
            h.set_game(slot, _game("FINAL", team_score=1, opp_score=2))
            h.tick()
            self.assertEqual(slot["game_ended_at"], t_final)
            self.assertTrue(h.scene._scoreboard_active)
            self.assertIs(h.scene._active_slot, slot)
            self.assertEqual([c for c in h.horn_calls if c[0] in ("GOAL", "WIN")], [])

            # The window is a substantial duration, not ~0: a FINAL must keep showing for
            # SCOREBOARD_POST_GAME_MINUTES.  Assert the configured magnitude is meaningful
            # (so a regression that collapsed it to 0 fails here) and that a tick a full
            # minute later is still active.
            self.assertGreaterEqual(sportscore.POST_GAME_SECONDS, 60)
            h.clock.advance(60)
            h.tick()
            self.assertTrue(h.scene._scoreboard_active)
            self.assertIs(h.scene._active_slot, slot)

            # At the boundary (window is inclusive: now - ended <= POST_GAME_SECONDS):
            # advance to exactly the edge → still active.  (Account for the +60 already
            # elapsed above so now - t_final lands precisely on the window.)
            h.clock.advance(sportscore.POST_GAME_SECONDS - 60)   # now - ended == window
            h.tick()
            self.assertTrue(h.scene._scoreboard_active)
            self.assertIs(h.scene._active_slot, slot)
            # game_ended_at must stay pinned to the real end time (not re-armed to now).
            self.assertEqual(slot["game_ended_at"], t_final)

            # One second PAST the boundary → yields to idle.
            h.clock.advance(1)
            h.tick()
            self.assertFalse(h.scene._scoreboard_active)
            self.assertIsNone(h.scene._active_slot)
            # Handed back to idle scenes exactly once at the transition.
            h.scene._reset_idle_scenes.assert_called_once()
            # End-time still preserved (so it can't re-arm a fresh 30-min window).
            self.assertEqual(slot["game_ended_at"], t_final)

    def test_winning_game_post_game_then_expiry(self):
        # Full win timeline: LIVE win → WIN celebration (180 s) owns the canvas → once it
        # lapses the FINAL is drawn as the active slot → still inside the 30-min window →
        # then expires to idle, and the WIN never re-fires.
        slot = _make_slot(team_name="VGK")
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("FUT", team_score=0))
            h.tick()
            h.set_game(slot, _game("LIVE", team_score=3, opp_score=2, period_label="3rd"))
            h.tick()
            t_final = h.clock.t
            h.set_game(slot, _game("FINAL", team_score=3, opp_score=2))
            h.tick()
            self.assertEqual(slot["game_ended_at"], t_final)
            self.assertEqual([c[0] for c in h.horn_calls].count("WIN"), 1)

            # Advance just past the WIN celebration window → FINAL now drawn as active.
            h.clock.advance(sportscore.WIN_CELEBRATION_SECONDS + 1)
            h.set_game(slot, _game("FINAL", team_score=3, opp_score=2))
            h.tick()
            self.assertTrue(h.scene._scoreboard_active)
            self.assertIs(h.scene._active_slot, slot)

            # Advance to the post-game boundary measured from t_final → still active.
            h.clock.advance(sportscore.POST_GAME_SECONDS
                            - (sportscore.WIN_CELEBRATION_SECONDS + 1))   # now - t_final == window
            h.tick()
            self.assertTrue(h.scene._scoreboard_active)

            # Past the boundary → idle, and WIN did not re-fire across the whole timeline.
            h.clock.advance(1)
            h.tick()
            self.assertFalse(h.scene._scoreboard_active)
            self.assertIsNone(h.scene._active_slot)
        self.assertEqual([c[0] for c in h.horn_calls].count("WIN"), 1)

    def test_expired_final_does_not_rearm_window(self):
        # Regression guard: once expired, re-ticking the same FINAL must stay idle
        # (must not reset game_ended_at to now and restart the window).  Loss path so the
        # active-slot draw runs every tick (no celebration confound).
        slot = _make_slot(team_name="VGK")
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("FUT", team_score=0))
            h.tick()
            h.set_game(slot, _game("LIVE", team_score=0, opp_score=2, period_label="3rd"))
            h.tick()
            t_final = h.clock.t
            h.set_game(slot, _game("FINAL", team_score=1, opp_score=2))
            h.tick()
            h.clock.advance(sportscore.POST_GAME_SECONDS + 1)
            h.tick()
            self.assertFalse(h.scene._scoreboard_active)
            # Several more ticks well past expiry — never re-activates.
            for _ in range(3):
                h.clock.advance(60)
                h.tick()
            self.assertFalse(h.scene._scoreboard_active)
            self.assertIsNone(h.scene._active_slot)
            self.assertEqual(slot["game_ended_at"], t_final)


# ── 4. ESPN rain-delay fix: PRE after LIVE must keep was_live ─────────────────────
class RainDelayGuard(unittest.TestCase):
    def test_live_then_pre_keeps_was_live_and_win_still_fires(self):
        # ESPN maps an in-progress STATUS_RAIN_DELAY to "PRE".  A game already watched LIVE
        # then briefly re-reporting PRE must NOT clear was_live/win_shown — otherwise the
        # post-resumption WIN celebration would be suppressed.
        slot = _make_slot(team_name="LAD")
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("FUT", team_score=0))
            h.tick()
            h.set_game(slot, _game("LIVE", team_score=2, opp_score=1, period_label="TOP 5"))
            h.tick()
            self.assertTrue(slot["was_live"])

            # Rain delay → ESPN reports PRE.  was_live must survive.
            h.set_game(slot, _game("PRE", team_score=2, opp_score=1))
            h.tick()
            self.assertTrue(slot["was_live"], "rain-delay PRE wiped was_live")
            self.assertFalse(slot["win_shown"])

            # Play resumes, then final with the team ahead → WIN must still fire.
            h.set_game(slot, _game("LIVE", team_score=3, opp_score=1, period_label="BOT 7"))
            h.tick()
            h.set_game(slot, _game("FINAL", team_score=3, opp_score=1))
            h.tick()
        win_calls = [c for c in h.horn_calls if c[0] == "WIN"]
        self.assertEqual(len(win_calls), 1)
        self.assertEqual(win_calls[0][1], "LAD")

    def test_genuinely_never_live_pre_arms_baseline(self):
        # A real pre-game PRE (never LIVE) arms the score baseline normally and keeps
        # was_live False so no spurious WIN can fire later.
        slot = _make_slot(team_name="LAD")
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("PRE", team_score=0))
            h.tick()
            self.assertFalse(slot["was_live"])
            self.assertEqual(slot["last_team_score"], 0)   # baseline armed from PRE
            # Now it goes live and the FIRST run scores → GOAL fires (baseline was 0).
            h.set_game(slot, _game("LIVE", team_score=1, opp_score=0, period_label="TOP 1"))
            h.tick()
        self.assertEqual(h.horn_kinds, ["GOAL"])

    def test_pre_after_live_does_not_rearm_baseline_no_false_goal(self):
        # During a rain-delay PRE the baseline must NOT be re-captured to the current score,
        # otherwise a goal scored just before the delay/around resumption could be missed or
        # double-counted.  was_live stays True and last_team_score is preserved.
        slot = _make_slot(team_name="LAD")
        with _SceneHarness([slot]) as h:
            h.set_game(slot, _game("FUT", team_score=0))
            h.tick()
            h.set_game(slot, _game("LIVE", team_score=2, opp_score=1, period_label="TOP 5"))
            h.tick()
            self.assertEqual(slot["last_team_score"], 2)
            h.set_game(slot, _game("PRE", team_score=2, opp_score=1))
            h.tick()
            # last_team_score unchanged (the FUT/PRE baseline branch is guarded by
            # last_team_score is None, which it no longer is).
            self.assertEqual(slot["last_team_score"], 2)
            self.assertTrue(slot["was_live"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
