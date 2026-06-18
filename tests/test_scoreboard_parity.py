"""Parity tests: the TWO independent scoreboard game-selection implementations must agree.

The "which game wins" + "LIVE/CRIT beats FINAL/OFF" + SCOREBOARD_PRIORITY logic is coded
TWICE:

  * web/scoreboard_data.py :: _fetch_scoreboard_data()
        Used by the web GUI and the desktop preview.  Walks SCOREBOARD_PRIORITY, returns the
        first LIVE/CRIT game, else the first FINAL/OFF game, else None.  Returns a tuple
        (game_dict, team_name, sport_key, enabled).

  * scenes/sportscore.py :: SportScoreScene._sports_score()
        Used by the physical Pi LED matrix.  Builds priority-ordered "slots", then Pass-1
        picks the first LIVE/CRIT slot, Pass-2 (only if no live) picks the first FINAL/OFF
        slot *still inside the POST_GAME_SECONDS window*; otherwise no game.  Drives
        self._active_slot rather than returning a value.

Only the web side is currently tested (tests/test_sports.py::ScoreboardFetch).  A fix to one
side can silently desync the Pi display from what the web/preview show.  These tests feed
BOTH sides the SAME synthetic per-sport game states and assert they select the SAME winner.

THE SEAM WE COMPARE
-------------------
For a given SCOREBOARD_PRIORITY and a given {sport_key -> game_dict} mapping we reduce each
implementation to the same observable answer:

    winner = (winning_sport_key, winning_game_state)   or   None  (no game on the board)

  * web   :  game, team_name, sport_key, _ = _fetch_scoreboard_data()
             -> (sport_key, game["state"])  when game is not None, else None.
             (team_name is set == sport_key in every config below so identity is comparable.)
  * scene :  run _sports_score(0); read self._active_slot
             -> (slot["key"], slot["game"]["state"])  when a slot is active, else None.

KNOWN, DOCUMENTED DIVERGENCE (not a bug, pinned by test_finals_past_window_documented_divergence)
------------------------------------------------------------------------------------------------
_fetch_scoreboard_data() applies NO post-game window — it returns a FINAL game no matter how
long ago it ended; the 30-minute window is applied by its *caller* (server.py) via the
persisted /tmp ended-at file.  The scene applies the window INLINE.  So for FINAL games that
ended *outside* POST_GAME_SECONDS the two legitimately differ (web=FINAL, scene=no-game).
Every other case feeds *fresh* FINALs (window just opened) so the gate is satisfied on both
sides and the winners must be identical.

RIGOR
-----
test_priority_table_battery drives BOTH sides across a systematic battery of state
combinations.  If either side's ordering (LIVE-over-FINAL, or the priority walk) were
changed, at least one combination would disagree and the test would fail — verified by the
test_*_meta_* guards, which re-run the battery against deliberately broken reference
selectors and assert disagreement is detected.
"""
import sys
import types
import time
import unittest
from itertools import product
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

# ── Repo on sys.path: both the project root and web/ (scoreboard_data lives there and is
#    normally imported as a top-level module by server.py) and scenes/ (sportscore imports
#    `from setup import ...` and `from utilities ...` relative to the root). ──
_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT / "web"), str(_ROOT / "scenes"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Stub the LED-matrix lib BEFORE importing sportscore (same pattern as tests/test_weather.py).
#    sportscore.py does `from rgbmatrix import graphics` at module top; MagicMock makes every
#    graphics.Color/Font/DrawText a no-op so the scene imports and renders on a dev box. ──
try:
    import rgbmatrix  # noqa: F401
except Exception:
    _g = MagicMock(name="rgbmatrix.graphics")
    _rgb = types.ModuleType("rgbmatrix")
    _rgb.graphics = _g
    sys.modules["rgbmatrix"] = _rgb
    sys.modules["rgbmatrix.graphics"] = _g

import scoreboard_data            # noqa: E402  web/scoreboard_data.py
import sportscore                 # noqa: E402  scenes/sportscore.py


# All sports, with the same fixed team-id / team-name everywhere so a winner's
# team_name (web) and slot key (scene) are directly comparable by sport key.
_SPORTS = ("NHL", "NFL", "MLB", "NBA", "WNBA", "MLS", "FIFA")
_TEAM_ID = {"NHL": 54, "NFL": 1, "MLB": 119, "NBA": 2, "WNBA": 17, "MLS": 3, "FIFA": 660}


def _game(state, team_score=1, opp_score=0):
    """Minimal game dict carrying just the fields the selection logic reads."""
    if state is None:
        return None
    return {
        "state": state,
        "team_score": team_score,
        "opp_score": opp_score,
        "opp_abbr": "OPP",
        "period_label": "",
        "period_type": "REG",
        "period": 1,
        "in_intermission": False,
        "game_id": "g-" + state,
    }


# ──────────────────────────────────────────────────────────────────────────────────────────
#  WEB SIDE driver — wraps web/scoreboard_data._fetch_scoreboard_data into the common answer.
# ──────────────────────────────────────────────────────────────────────────────────────────
class _WebHarness:
    """Inject read_config + mock the three fetchers (keyed by sport) on scoreboard_data,
    exactly like the existing ScoreboardFetch tests, then reduce the tuple result to
    (sport_key, state) | None."""

    def __init__(self):
        self._orig_rc = scoreboard_data.read_config
        self._orig = {k: getattr(scoreboard_data, k)
                      for k in ("_sb_fetch_nhl", "_sb_fetch_espn", "_sb_fetch_mlb")}

    def restore(self):
        scoreboard_data.read_config = self._orig_rc
        for k, v in self._orig.items():
            setattr(scoreboard_data, k, v)

    def select(self, priority, games):
        """games: {sport_key -> game_dict|None}.  Returns (sport_key, state) | None."""
        cfg = {"SCOREBOARD_ENABLED": True, "SCOREBOARD_PRIORITY": list(priority)}
        for s in _SPORTS:
            cfg[f"SCOREBOARD_{s}_ENABLED"] = True
            cfg[f"SCOREBOARD_{s}_TEAM_ID"] = _TEAM_ID[s]
            cfg[f"SCOREBOARD_{s}_TEAM_NAME"] = s          # team_name == sport key
        scoreboard_data.read_config = lambda: dict(cfg)

        # ESPN fetcher is shared by NFL/NBA/MLS and is called as fetch(path, team_id, tz);
        # map team_id back to the sport so each ESPN sport can return its own game.
        espn_by_id = {_TEAM_ID["NFL"]: "NFL", _TEAM_ID["NBA"]: "NBA",
                      _TEAM_ID["WNBA"]: "WNBA", _TEAM_ID["MLS"]: "MLS",
                      _TEAM_ID["FIFA"]: "FIFA"}

        scoreboard_data._sb_fetch_nhl = lambda tid, tz: games.get("NHL")
        scoreboard_data._sb_fetch_mlb = lambda tid, tz: games.get("MLB")
        scoreboard_data._sb_fetch_espn = lambda path, tid, tz: games.get(espn_by_id.get(tid))

        game, name, key, _enabled = scoreboard_data._fetch_scoreboard_data()
        if game is None:
            return None
        # name and key should agree (team_name == key by construction); assert that invariant
        # so a future refactor that desyncs them is caught here too.
        assert name == key, f"web returned name={name!r} != key={key!r}"
        return (key, game["state"])


# ──────────────────────────────────────────────────────────────────────────────────────────
#  SCENE SIDE driver — drives SportScoreScene._sports_score into the common answer.
# ──────────────────────────────────────────────────────────────────────────────────────────
class _SceneHarness:
    """Patch sportscore's config reads + module-level priority/master so _build_slots()
    yields a faithful, real slot list, inject each slot's `game` directly (bypassing the
    async network fetch), run one _sports_score tick, and read self._active_slot."""

    def __init__(self):
        self._orig_cfg = sportscore._cfg
        self._orig_priority = sportscore.SCOREBOARD_PRIORITY
        self._orig_master = sportscore.SCOREBOARD_MASTER_ENABLED

    def restore(self):
        sportscore._cfg = self._orig_cfg
        sportscore.SCOREBOARD_PRIORITY = self._orig_priority
        sportscore.SCOREBOARD_MASTER_ENABLED = self._orig_master

    def _build_scene(self, priority, games, finals_aged_out=False):
        cfg = {"SCOREBOARD_ENABLED": True}
        for s in _SPORTS:
            cfg[f"SCOREBOARD_{s}_ENABLED"] = True
            cfg[f"SCOREBOARD_{s}_TEAM_ID"] = _TEAM_ID[s]
            cfg[f"SCOREBOARD_{s}_TEAM_NAME"] = s
        sportscore._cfg = lambda name, default, _c=cfg: _c.get(name, default)
        sportscore.SCOREBOARD_PRIORITY = list(priority)
        sportscore.SCOREBOARD_MASTER_ENABLED = True

        scene = sportscore.SportScoreScene()      # _build_slots() runs in __init__
        # Collaborators normally supplied by the MatrixDisplay mixin (display/__init__.py):
        scene._data = []                           # no flights → scoreboard not pre-empted
        scene.canvas = MagicMock(name="canvas")    # _draw_score draws onto it (graphics is mocked)
        scene.reset_scene = MagicMock(name="reset_scene")
        scene._reset_idle_scenes = MagicMock(name="_reset_idle_scenes")

        now = time.time()
        for slot in scene._sport_slots:
            slot["game"] = games.get(slot["key"])
            # Bypass the async network fetch: every slot is born with poll_due=0.0, so the
            # first _sports_score tick would otherwise fire _poll_slot_async() → the slot's
            # REAL fetch_fn (_fetch_mlb/_fetch_nhl/…) against the live API, on a background
            # thread that races select_active and can overwrite our injected game with a real
            # one (a real LIVE/FINAL game on game day → flaky parity mismatch). Push poll_due
            # far into the future so the injected game is the only thing the tick ever sees.
            slot["poll_due"] = now + 1e9
            if finals_aged_out and slot["game"] and slot["game"]["state"] in ("FINAL", "OFF"):
                # Stamp the post-game window as having opened well in the past so Pass-2's
                # `now - ended <= POST_GAME_SECONDS` gate fails — i.e. the window has lapsed.
                slot["game_ended_at"] = now - (sportscore.POST_GAME_SECONDS + 120)
            # else: leave game_ended_at = None; _sports_score stamps it to `now` this tick
            #       (fresh FINAL, window just opened → Pass-2 selects it).
        return scene

    def select(self, priority, games, finals_aged_out=False):
        scene = self._build_scene(priority, games, finals_aged_out)
        scene._sports_score(0)
        slot = scene._active_slot
        if slot is None:
            return None
        return (slot["key"], slot["game"]["state"])


# ──────────────────────────────────────────────────────────────────────────────────────────
#  Reference selector — the SINGLE source of truth for "what both sides SHOULD pick".
#  Used (a) to give each parity case an explicit expected winner and (b) as the table the
#  battery + meta-guards compare against.  It encodes ONLY the contract: walk priority, take
#  the first LIVE/CRIT, else the first FINAL/OFF, else None.
# ──────────────────────────────────────────────────────────────────────────────────────────
_LIVE = ("LIVE", "CRIT")
_FINAL = ("FINAL", "OFF")


def _reference_select(priority, games):
    """Expected (sport_key, state) | None per the shared contract (no post-game window)."""
    for key in priority:
        g = games.get(key)
        if g and g["state"] in _LIVE:
            return (key, g["state"])
    for key in priority:
        g = games.get(key)
        if g and g["state"] in _FINAL:
            return (key, g["state"])
    return None


class ScoreboardParity(unittest.TestCase):
    def setUp(self):
        self.web = _WebHarness()
        self.scene = _SceneHarness()
        self.addCleanup(self.web.restore)
        self.addCleanup(self.scene.restore)

    # Convenience: assert both impls agree with each other AND with the reference contract.
    def _assert_agree(self, priority, games, *, finals_aged_out=False):
        web_pick = self.web.select(priority, games)
        scene_pick = self.scene.select(priority, games, finals_aged_out=finals_aged_out)
        expected = _reference_select(priority, games)
        self.assertEqual(web_pick, expected,
                         f"WEB disagrees with contract for {games} @ {priority}")
        self.assertEqual(scene_pick, expected,
                         f"SCENE disagrees with contract for {games} @ {priority}")
        self.assertEqual(web_pick, scene_pick,
                         f"WEB {web_pick} != SCENE {scene_pick} for {games} @ {priority}")
        return web_pick

    # ── Case 1: one LIVE + one FINAL → both pick the LIVE one (even when FINAL is higher prio)
    def test_live_beats_final_both_pick_live(self):
        pick = self._assert_agree(
            ["NHL", "NFL"],
            {"NHL": _game("FINAL"), "NFL": _game("LIVE")},
        )
        self.assertEqual(pick, ("NFL", "LIVE"))

    def test_live_beats_final_when_live_is_higher_priority_too(self):
        pick = self._assert_agree(
            ["NFL", "NHL"],
            {"NFL": _game("LIVE"), "NHL": _game("FINAL")},
        )
        self.assertEqual(pick, ("NFL", "LIVE"))

    def test_crit_counts_as_live_and_beats_final(self):
        # CRIT (final minutes / tight game) is in the LIVE tier on both sides.
        pick = self._assert_agree(
            ["NHL", "MLB"],
            {"NHL": _game("FINAL"), "MLB": _game("CRIT")},
        )
        self.assertEqual(pick, ("MLB", "CRIT"))

    # ── Case 2: multiple LIVE games → both honor SCOREBOARD_PRIORITY order
    def test_multiple_live_honor_priority_nfl_first(self):
        pick = self._assert_agree(
            ["NFL", "NHL"],
            {"NHL": _game("LIVE"), "NFL": _game("LIVE")},
        )
        self.assertEqual(pick, ("NFL", "LIVE"))

    def test_multiple_live_honor_priority_nhl_first(self):
        # Same games, flipped priority → the other sport must win on BOTH sides.
        pick = self._assert_agree(
            ["NHL", "NFL"],
            {"NHL": _game("LIVE"), "NFL": _game("LIVE")},
        )
        self.assertEqual(pick, ("NHL", "LIVE"))

    def test_three_live_picks_first_in_priority(self):
        pick = self._assert_agree(
            ["MLS", "NBA", "NHL", "NFL", "MLB"],
            {"NHL": _game("LIVE"), "NFL": _game("LIVE"), "MLS": _game("LIVE")},
        )
        self.assertEqual(pick, ("MLS", "LIVE"))

    def test_fifa_world_cup_live_is_selected(self):
        # FIFA (World Cup) is a first-class ESPN sport — a live match wins over a final.
        pick = self._assert_agree(
            ["NHL", "FIFA"],
            {"NHL": _game("FINAL"), "FIFA": _game("LIVE")},
        )
        self.assertEqual(pick, ("FIFA", "LIVE"))

    def test_wnba_live_is_selected(self):
        # WNBA is its own ESPN sport (basketball/wnba) distinct from the NBA — a live
        # WNBA game wins over a finished NBA game, and the two never get conflated.
        pick = self._assert_agree(
            ["NBA", "WNBA"],
            {"NBA": _game("FINAL"), "WNBA": _game("LIVE")},
        )
        self.assertEqual(pick, ("WNBA", "LIVE"))

    # ── Case 3: all FINAL → both pick per priority (fresh window) …
    def test_all_final_fresh_pick_first_in_priority(self):
        pick = self._assert_agree(
            ["NHL", "NFL"],
            {"NHL": _game("FINAL"), "NFL": _game("FINAL")},
        )
        self.assertEqual(pick, ("NHL", "FINAL"))

    def test_all_final_fresh_priority_reordered(self):
        pick = self._assert_agree(
            ["NFL", "NHL"],
            {"NHL": _game("FINAL"), "NFL": _game("FINAL")},
        )
        self.assertEqual(pick, ("NFL", "FINAL"))

    def test_off_state_treated_as_final(self):
        # NHL uses "OFF" for a finished game; it shares the FINAL tier on both sides.
        pick = self._assert_agree(
            ["NHL", "NFL"],
            {"NHL": _game("OFF"), "NFL": _game("FINAL")},
        )
        self.assertEqual(pick, ("NHL", "OFF"))

    # ── Case 4: nothing live/recent → both report "no game"
    def test_no_games_at_all_both_report_none(self):
        pick = self._assert_agree(
            ["NHL", "NFL", "MLB"],
            {"NHL": None, "NFL": None, "MLB": None},
        )
        self.assertIsNone(pick)

    def test_only_future_games_both_report_none(self):
        # FUT/PRE are neither LIVE nor FINAL → neither side puts them on the board.
        pick = self._assert_agree(
            ["NHL", "NFL"],
            {"NHL": _game("FUT"), "NFL": _game("PRE")},
        )
        self.assertIsNone(pick)

    def test_future_plus_final_picks_final(self):
        pick = self._assert_agree(
            ["NHL", "NFL"],
            {"NHL": _game("FUT"), "NFL": _game("FINAL")},
        )
        self.assertEqual(pick, ("NFL", "FINAL"))

    # ── DOCUMENTED DIVERGENCE: FINAL games that ended OUTSIDE the post-game window.
    #    The web side has no window (its caller applies it) → still returns the FINAL.
    #    The scene applies the window inline → reports no game.  This is intentional; the
    #    test PINS it so a change to either side's window handling is caught and reviewed.
    def test_finals_past_window_documented_divergence(self):
        priority = ["NHL", "NFL"]
        games = {"NHL": _game("FINAL"), "NFL": _game("FINAL")}

        web_pick = self.web.select(priority, games)
        scene_pick = self.scene.select(priority, games, finals_aged_out=True)

        # Web: no inline window → first FINAL in priority is still returned.
        self.assertEqual(web_pick, ("NHL", "FINAL"),
                         "web side is expected to return a FINAL regardless of age "
                         "(post-game window is applied by server.py, not here)")
        # Scene: window lapsed → board goes idle.
        self.assertIsNone(scene_pick,
                          "scene side is expected to drop a FINAL once POST_GAME_SECONDS "
                          "has elapsed")
        # And the scene flags itself inactive so idle scenes resume.
        # (re-derive the scene to assert its public 'active' flag for the same inputs)
        scene = self.scene._build_scene(priority, games, finals_aged_out=True)
        scene._sports_score(0)
        self.assertFalse(scene._scoreboard_active)

    # ── RIGOR: systematic battery — both sides vs the same contract table ──────────────────
    def _battery_cases(self):
        """Yield (priority, games) over a systematic set of per-sport state combos.

        Uses three sports (NHL, NFL, MLB → covers the NHL fetcher, the ESPN fetcher, and the
        MLB fetcher) and both priority orderings, with each sport independently None / FUT /
        FINAL / LIVE.  FINALs are fed FRESH (window satisfied) so this isolates the *ordering*
        contract, which both sides share."""
        states = (None, "FUT", "FINAL", "LIVE")
        priorities = (("NHL", "NFL", "MLB"), ("MLB", "NFL", "NHL"))
        for prio in priorities:
            for combo in product(states, repeat=3):
                games = {sport: _game(st) for sport, st in zip(("NHL", "NFL", "MLB"), combo)}
                yield prio, games

    def test_priority_table_battery(self):
        """Drive BOTH implementations across the whole battery; assert they pick the SAME
        winner as each other and as the reference contract, every time."""
        checked = 0
        for prio, games in self._battery_cases():
            web_pick = self.web.select(prio, games)
            scene_pick = self.scene.select(prio, games)   # fresh FINALs → window satisfied
            expected = _reference_select(prio, games)
            self.assertEqual(web_pick, scene_pick,
                             f"WEB {web_pick} != SCENE {scene_pick} for {games} @ {prio}")
            self.assertEqual(web_pick, expected,
                             f"WEB {web_pick} != contract {expected} for {games} @ {prio}")
            checked += 1
        self.assertEqual(checked, 2 * 4 ** 3)   # 128 combinations actually exercised

    # ── META-GUARDS: prove the battery would CATCH a desync (the assertions have teeth) ────
    def test_meta_battery_detects_dropped_live_over_final(self):
        """If a side stopped giving LIVE priority over FINAL (took first non-None in priority
        instead), some battery combo must disagree with the real reference — proving
        test_priority_table_battery is not vacuously passing."""
        def _broken_first_nonnull(priority, games):
            for key in priority:
                g = games.get(key)
                if g and g["state"] in _LIVE + _FINAL:
                    return (key, g["state"])      # ignores LIVE-over-FINAL ordering
            return None
        mismatches = [
            (prio, games)
            for prio, games in self._battery_cases()
            if _broken_first_nonnull(prio, games) != _reference_select(prio, games)
        ]
        self.assertTrue(mismatches,
                        "a LIVE-over-FINAL regression would slip past the battery")

    def test_meta_battery_detects_priority_reversal(self):
        """If a side walked priority in REVERSE, some battery combo must disagree with the
        reference — proving the priority-order assertions have teeth."""
        def _broken_reversed(priority, games):
            return _reference_select(list(reversed(priority)), games)
        mismatches = [
            (prio, games)
            for prio, games in self._battery_cases()
            if _broken_reversed(prio, games) != _reference_select(prio, games)
        ]
        self.assertTrue(mismatches,
                        "a priority-reversal regression would slip past the battery")

    # ── Sanity: the two harnesses are really exercising the two different code paths ───────
    def test_harnesses_target_distinct_implementations(self):
        # Guard against a copy-paste error that points both harnesses at one module.
        self.assertIsNot(scoreboard_data, sportscore)
        self.assertTrue(hasattr(scoreboard_data, "_fetch_scoreboard_data"))
        self.assertTrue(hasattr(sportscore, "SportScoreScene"))
        self.assertTrue(hasattr(sportscore.SportScoreScene, "_sports_score"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
