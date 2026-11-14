"""Whole-Display simulation tests for the GOAL-celebration / flight hand-off.

Regression for the 2026-10-08 20:07 PDT blank-panel deadlock: a flight was picked up
while a "VGK GOAL!" scroll was on screen.  celebration_frame yields to flights *before*
its expiry cleanup, the flight scenes yield to `_goal_celebration_active`, so nothing
drew; plane_details never completed a pass, `_data_all_looped` stayed False, and with 2
flights grab_new_data stopped polling forever (panel black for 9 h).

Unlike the per-scene unit tests, this harness builds the REAL Display (every scene
mixin, real keyframe discovery/ordering, real Animator.play loop) with only the edges
stubbed:
  * rgbmatrix: conftest stub; graphics.DrawText/DrawLine replaced by a "lit-pixel"
    recorder bound to a FakeCanvas that is reset by Clear();
  * Overhead: FakeOverhead whose grab_data() instantly returns a scripted sky;
  * time.time(): a hand-cranked clock advanced 0.1 s per frame from the animator's sleep;
  * sports fetches never run (poll_due parked); game state is scripted per tick;
  * horn/ding/state UDP + /tmp flag files are stubbed.
"""
import os
import random
import tempfile
from unittest import mock

import pytest

import display
from rgbmatrix import graphics
from scenes import sportscore
from scenes.weather import WeatherScene
from utilities import animator as animator_mod

FPS = 10
NOFILE = os.path.join(tempfile.mkdtemp(prefix="ft-sim-"), "absent")


class _Stop(Exception):
    pass


class Clock:
    def __init__(self, t0):
        self.t = float(t0)

    def __call__(self):
        return self.t


def _is_black(c):
    return all(getattr(c, ch, 1) == 0 for ch in ("red", "green", "blue"))


class FakeCanvas:
    """Counts visible, non-black marks since the last Clear() (rgbmatrix has no GetPixel)."""

    def __init__(self):
        self.lit = 0

    def Clear(self):
        self.lit = 0

    def Fill(self, r, g, b):
        self.lit = 1 if (r or g or b) else 0

    def SetPixel(self, x, y, r, g, b):
        if (r or g or b) and 0 <= x < 64 and 0 <= y < 32:
            self.lit += 1


class FakeMatrix:
    brightness = 100

    def __init__(self, *a, **k):
        pass

    def CreateFrameCanvas(self):
        return FakeCanvas()

    def SwapOnVSync(self, canvas):
        return canvas


class FakeOptions:
    pass


class FakeOverhead:
    def __init__(self, clock, sky):
        self.clock, self.sky = clock, sky
        self.polls = []
        self._data, self._new = [], False
        self.status = None

    def grab_data(self):
        self.polls.append(self.clock())
        self._data = [dict(f) for f in self.sky(self.clock())]
        self._new = True

    @property
    def new_data(self):
        return self._new

    @property
    def processing(self):
        return False

    @property
    def data(self):
        self._new = False
        return self._data

    @property
    def data_is_empty(self):
        return not self._data

    def set_display_status(self, status):     # used by the hardened Display (no-op if absent)
        self.status = status


def plane(cs, name, hx=None):
    return {"callsign": cs, "plane": name, "display_name": name, "origin": "LAS",
            "destination": "OMA", "vertical_speed": 0, "altitude": 5000,
            "hex": hx or ("h" + cs.lower())}


def live(team, opp, gid=2026020065, state="LIVE", label="2nd"):
    return {"state": state, "team_score": team, "opp_score": opp, "opp_abbr": "TOR",
            "team_home": True, "period": 2, "period_type": "REG", "period_label": label,
            "time_remaining": "18:48", "in_intermission": False,
            "start_time_utc": "2026-10-09T02:00:00Z", "game_id": gid}


class Sim:
    """Builds a real Display and runs Animator.play() for N simulated seconds."""

    def __init__(self, sky, t0=1791515190.0, dismissed=lambda t: None, paused=lambda t: False):
        self.clock = Clock(t0)
        self.t0 = t0
        self.sky_fn = sky
        self.dismissed = dismissed
        self.paused_fn = paused
        self.fake = FakeOverhead(self.clock, lambda t: sky(t - t0))
        self.frame_texts = []
        self.samples = []          # (rel_t, lit, celeb_flag, n_data, texts, paused)
        self.horn = []
        self.script = {}           # rel frame -> callable(sim)
        self._budget = 0
        self._patches = []

    # ── recorder hooks ────────────────────────────────────────────────────────
    def _draw_text(self, canvas, font, x, y, colour, text):
        w = len(text) * 6
        if isinstance(canvas, FakeCanvas) and not _is_black(colour) and x < 64 and x + w > 0:
            canvas.lit += 1
            self.frame_texts.append(text)
        return w

    def _draw_line(self, canvas, x0, y0, x1, y1, colour):
        if isinstance(canvas, FakeCanvas) and not _is_black(colour):
            canvas.lit += 1

    def _sleep(self, _delay):
        d = self.d
        rel = round(self.clock.t - self.t0, 1)
        self.samples.append((rel, d.canvas.lit > 0 and not d._paused, d._goal_celebration_active,
                             len(d._data), tuple(self.frame_texts), d._paused))
        self.frame_texts = []
        self.clock.t += 1.0 / FPS
        fr = len(self.samples)
        cb = self.script.get(fr)
        if cb:
            cb(self)
        if fr >= self._budget:
            raise _Stop()

    def __enter__(self):
        sim = self
        p = [
            mock.patch.object(display, "Overhead", lambda: sim.fake),
            mock.patch.object(display, "RGBMatrix", FakeMatrix),
            mock.patch.object(display, "RGBMatrixOptions", FakeOptions),
            mock.patch.object(WeatherScene, "_weather_refresh_loop", lambda self: None),
            mock.patch.object(display, "PAUSE_FLAG", NOFILE),
            mock.patch.object(display, "NIGHT_FLAG", NOFILE),
            mock.patch.object(display.planeding, "send_ding", lambda *a, **k: None),
            mock.patch.object(display.planeding, "send_state", lambda *a, **k: None),
            mock.patch.object(sportscore, "_send_horn", lambda *a: sim.horn.append((sim.rel(), a))),
            mock.patch.object(sportscore, "_send_state", lambda *a, **k: None),
            mock.patch.object(sportscore, "_read_dismissed_game_id",
                              lambda: sim.dismissed(sim.rel())),
            mock.patch("time.time", self.clock),
            mock.patch.object(graphics, "DrawText", self._draw_text),
            mock.patch.object(graphics, "DrawLine", self._draw_line),
            mock.patch.object(animator_mod, "sleep", self._sleep),
        ]
        for x in p:
            x.start()
        self._patches = p
        self.d = display.Display()
        # paused flag is driven by the script, not /tmp
        orig_refresh = self.d.refresh_flags

        def _refresh(count, _o=orig_refresh):
            self.d._paused = bool(self.paused_fn(self.rel()))
            self.d._night = False
        _refresh.properties = orig_refresh.__func__.properties
        for i, kf in enumerate(self.d.keyframes):
            if getattr(kf, "__name__", "") == "refresh_flags":
                self.d.keyframes[i] = _refresh
        self.slot = next(s for s in self.d._sport_slots if s["key"] == "NHL")
        self.slot["poll_due"] = 1e18
        return self

    def __exit__(self, *exc):
        for x in reversed(self._patches):
            x.stop()

    def rel(self):
        return round(self.clock.t - self.t0, 1)

    def at(self, secs, fn):
        self.script[int(round(secs * FPS))] = fn

    def run(self, secs):
        self._budget = int(round(secs * FPS))
        try:
            self.d.play()
        except _Stop:
            pass
        return self

    # ── analysis helpers ──────────────────────────────────────────────────────
    def longest_blank(self, start=0.0, end=1e18):
        best = run = 0
        for rel, lit, _f, _n, _t, paused in self.samples:
            if rel < start or rel > end or paused:
                run = 0
                continue
            run = 0 if lit else run + 1
            best = max(best, run)
        return best / FPS

    def first_time(self, pred, start=0.0):
        for s in self.samples:
            if s[0] >= start and pred(s):
                return s[0]
        return None

    def poll_gaps(self):
        ts = [t - self.t0 for t in self.fake.polls]
        return [b - a for a, b in zip(ts, ts[1:])], ts


# ═════════════════════════════════════════════════════════════════════════════
# 1. Incident replay (2026-10-08 20:06:43 goal, N1950F + SWA4499 overhead)
# ═════════════════════════════════════════════════════════════════════════════
CESSNA = plane("N1950F", "Cessna 172 Skyhawk", "a17d1c")
MAX8 = plane("SWA4499", "Boeing 737 MAX 8", "ac47de")


def _incident_sky(rel):
    sky = []
    if 20 <= rel < 700:
        sky.append(CESSNA)
    if 32 <= rel < 400:
        sky.append(MAX8)
    return sky


def _setup_live_goal(sim, goal_at=10.0, gid=2026020065):
    sim.slot["game"] = live(1, 3, gid)
    sim.slot["last_team_score"] = 1
    sim.slot["last_game_id"] = gid
    sim.slot["was_live"] = True
    sim.at(goal_at, lambda s: s.slot.__setitem__("game", live(2, 3, gid)))


def test_incident_replay_flight_arrives_mid_goal_celebration():
    with Sim(_incident_sky) as sim:
        _setup_live_goal(sim)
        sim.run(1000)

    d = sim.d
    pick = sim.first_time(lambda s: s[3] > 0)
    assert pick is not None and pick < 36, "flight never picked up"
    # The celebration did run before the plane arrived (sanity: scenario is the incident's)
    assert sim.first_time(lambda s: "VGK GOAL!" in s[4]) is not None
    # (a) the plane is actually drawn promptly after pickup — the incident drew NOTHING
    shown = sim.first_time(lambda s: "Cessna 172 Skyhawk" in s[4] or "N1950F" in s[4], start=pick)
    assert shown is not None and shown - pick <= 1.0, f"flight not drawn after pickup (shown={shown})"
    # (b) the celebration flag never coexists with flights for more than a frame
    overlap = sum(1 for s in sim.samples if s[2] and s[3] > 0)
    assert overlap <= 1, f"celebration flag held for {overlap} frames while flights on screen"
    # (c) polling never starves (incident: last poll at +45 s, then none for 9 h)
    gaps, ts = sim.poll_gaps()
    assert max(gaps) <= 150, f"poll starvation: max gap {max(gaps):.0f}s, polls={ts[:12]}..."
    # (d) display converges back to the scoreboard once the sky clears (+700 s)
    assert d._data == [] and not d._goal_celebration_active
    assert sim.longest_blank(start=5) <= 2.0, f"panel blank for {sim.longest_blank(start=5)}s"


# ═════════════════════════════════════════════════════════════════════════════
# 2. One flight during a 180 s WIN scroll: flight shown, WIN resumes, then FINAL board
# ═════════════════════════════════════════════════════════════════════════════
def test_single_flyover_during_win_celebration_suspends_and_resumes():
    def sky(rel):
        return [CESSNA] if 20 <= rel < 50 else []

    with Sim(sky) as sim:
        _setup_live_goal(sim, goal_at=1e9)          # no goal; we script a WIN instead
        sim.at(10, lambda s: s.slot.__setitem__("game", live(4, 3, state="FINAL", label="3rd")))
        sim.run(260)

    pick = sim.first_time(lambda s: s[3] > 0)
    gone = sim.first_time(lambda s: s[3] == 0, start=pick)
    assert sim.first_time(lambda s: "VGK WINS!" in s[4]) < pick           # WIN started first
    assert sim.first_time(lambda s: "Cessna 172 Skyhawk" in s[4], start=pick) - pick <= 1.0
    # suspend-and-resume: the WIN scroll comes back after the flyover (still inside 180 s)
    assert sim.first_time(lambda s: "VGK WINS!" in s[4], start=gone) is not None
    # then hands back to the FINAL board once the window ends, and the WIN horn fired once
    end = sim.d._celebration_until - sim.t0
    assert 185 <= end <= 195
    assert sim.first_time(lambda s: "FINAL" in s[4], start=end) is not None
    assert not sim.d._goal_celebration_active and sim.samples[-1][1]   # board lit at the end
    assert [a[0] for _t, a in sim.horn].count("WIN") == 1
    assert sim.slot["win_shown"] is True
    assert sim.longest_blank(start=5) <= 2.0


# ═════════════════════════════════════════════════════════════════════════════
# 3. "Hide this game" while a celebration is suspended under a flyover
# ═════════════════════════════════════════════════════════════════════════════
def test_hide_game_during_suspended_celebration_returns_to_idle():
    def sky(rel):
        return [CESSNA] if 20 <= rel < 50 else []

    with Sim(sky, dismissed=lambda rel: "2026020065" if rel >= 40 else None) as sim:
        _setup_live_goal(sim)
        sim.run(120)
    d = sim.d
    assert not d._goal_celebration_active and not d._scoreboard_active
    assert sim.first_time(lambda s: "VGK GOAL!" in s[4], start=50) is None   # no resume once hidden
    assert sim.longest_blank(start=5) <= 2.0


# ═════════════════════════════════════════════════════════════════════════════
# 3b. Frame-exact boundary: 2 planes picked up right around the celebration's end.
#     Keyframes run in dir() order, so celebration_frame runs before
#     check_for_loaded_data — on the old code planes loaded even 0.05 s before
#     _celebration_until latched the deadlock, while planes loaded at/after it didn't.
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("delta", [-0.15, -0.05, 0.0, 0.05, 0.15, 1.95])
def test_two_planes_loaded_at_the_celebration_boundary(delta):
    def sky(rel):
        return [CESSNA, MAX8] if 40 <= rel < 400 else []

    def set_until(s):
        s.d._celebration_until = s.t0 + 40.0 + delta

    def inject(s):
        s.fake._data, s.fake._new = [dict(CESSNA), dict(MAX8)], True

    with Sim(sky) as sim:
        _setup_live_goal(sim)
        sim.at(39.0, set_until)
        sim.at(40.0, inject)
        sim.run(240)

    shown = sim.first_time(lambda s: "Cessna 172 Skyhawk" in s[4] or "N1950F" in s[4], start=40)
    assert shown is not None and shown - 40 <= 1.5, f"planes not drawn after pickup (shown={shown})"
    assert sum(1 for s in sim.samples if s[2] and s[3] > 0) <= 1
    gaps, _ts = sim.poll_gaps()
    assert max(gaps) <= 150, f"poll starvation: max gap {max(gaps):.0f}s"
    assert sim.first_time(lambda s: "Boeing 737 MAX 8" in s[4], start=40) is not None, \
        "marquee never advanced to the 2nd plane"
    assert sim.longest_blank(start=5) <= 2.0


# ═════════════════════════════════════════════════════════════════════════════
# 3c. Two planes arriving one after the other inside a 180 s WIN window
# ═════════════════════════════════════════════════════════════════════════════
def test_two_planes_mid_win_window():
    def sky(rel):
        sky = []
        if 83 <= rel < 300:      # WIN armed at +10 → plane A ~73 s into the window
            sky.append(CESSNA)
        if 99 <= rel < 300:      # plane B ~89 s in
            sky.append(MAX8)
        return sky

    with Sim(sky) as sim:
        _setup_live_goal(sim, goal_at=1e9)
        sim.at(10, lambda s: s.slot.__setitem__("game", live(4, 3, state="FINAL", label="3rd")))
        sim.run(420)

    pick = sim.first_time(lambda s: s[3] > 0)
    assert sim.first_time(lambda s: "VGK WINS!" in s[4]) < pick
    assert sim.first_time(lambda s: "Cessna 172 Skyhawk" in s[4], start=pick) - pick <= 1.0
    assert sim.first_time(lambda s: "Boeing 737 MAX 8" in s[4], start=pick) is not None
    assert sum(1 for s in sim.samples if s[2] and s[3] > 0) <= 1
    gaps, _ts = sim.poll_gaps()
    assert max(gaps) <= 150
    assert sim.d._data == [] and not sim.d._goal_celebration_active
    assert [a[0] for _t, a in sim.horn].count("WIN") == 1
    assert sim.longest_blank(start=5) <= 2.0


# ═════════════════════════════════════════════════════════════════════════════
# 3d. Unpause repaints the reset-only flight rows (callsign + journey)
# ═════════════════════════════════════════════════════════════════════════════
def test_unpause_repaints_flight_rows():
    def sky(rel):
        return [CESSNA] if rel < 200 else []

    with Sim(sky, paused=lambda rel: 30 <= rel < 40) as sim:
        sim.run(80)

    # journey() draws the airport codes and flight_details() the callsign ONLY inside
    # reset_scene(); a paused frame Clear()s the canvas, so unpause must reset the scene.
    back = sim.first_time(lambda s: not s[5], start=30)
    assert back is not None
    row = sim.first_time(lambda s: "OMA" in s[4] or "N1950F" in s[4], start=back)
    assert row is not None and row - back <= 0.5, f"flight rows not repainted after unpause ({row})"


# ═════════════════════════════════════════════════════════════════════════════
# 4. Randomised soak: goals, wins, flights (0-3), pauses — global invariants
# ═════════════════════════════════════════════════════════════════════════════
POOL = [CESSNA, MAX8, plane("AAL204", "Airbus A321-200"),
        plane("N145LV", "Airbus Helicopters Squirrel AS.350 B3e (H125)"),
        plane("DAL88", "Boeing 757-200")]


@pytest.mark.parametrize("seed", range(6))
def test_soak_invariants(seed):
    rng = random.Random(seed)
    horizon = 1800
    # flights: random intervals; bias arrivals to overlap goal celebrations
    goals = sorted(rng.uniform(20, horizon - 200) for _ in range(8))
    intervals = []
    for g in goals:
        for k in range(rng.choice([1, 2, 2, 3])):
            start = g + rng.uniform(-5, 28)
            intervals.append((POOL[(len(intervals)) % len(POOL)], start, start + rng.uniform(15, 200)))

    def sky(rel):
        seen, out = set(), []
        for f, a, b in intervals:
            if a <= rel < b and f["callsign"] not in seen:
                seen.add(f["callsign"])
                out.append(f)
        return out

    with Sim(sky) as sim:
        sim.slot["game"] = live(0, 0)
        sim.slot["last_team_score"] = 0
        sim.slot["last_game_id"] = 2026020065
        sim.slot["was_live"] = True
        score = {"v": 0}

        def goal(s):
            score["v"] += 1
            s.slot["game"] = live(score["v"], 0)
        for g in goals:
            sim.at(g, goal)
        sim.run(horizon)

    # I1: flights on screen (not paused) => a flight string drawn within the last 2 s
    last_flight_draw, viol = None, []
    names = {f["display_name"] for f in POOL} | {f["callsign"] for f in POOL}
    for rel, lit, flag, n, texts, paused in sim.samples:
        if any(t in names for t in texts):
            last_flight_draw = rel
        if n and not paused:
            since = rel - (last_flight_draw if last_flight_draw is not None else -1e9)
            if since > 2.0 and not viol:
                viol.append((rel, since))
        elif not n:
            last_flight_draw = rel   # timer restarts on (re)appearance
    assert not viol, f"seed {seed}: flights on screen but not drawn: {viol[:3]}"
    # I2: celebration flag never held while flights are on screen (beyond the hand-off frame)
    assert sum(1 for s in sim.samples if s[2] and s[3]) <= 8 * 2
    # I3: never blank for > 2 s
    assert sim.longest_blank(start=5) <= 2.0
    # I4: polls never starve
    gaps, _ = sim.poll_gaps()
    assert max(gaps) <= 150
    # I5: the render self-check (if present) never had to intervene
    assert getattr(sim.d, "_stall_heals_total", 0) == 0
