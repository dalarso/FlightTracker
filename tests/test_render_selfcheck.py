"""Defence-in-depth for the 2026-10-08 blank panel: poll-gate staleness cap, render
self-check (detect -> soft heal -> exit for systemd), overhead starvation log, health."""
import time
from unittest import mock

import pytest

import display
from tests.test_celebration_flight_handoff import (CESSNA, MAX8, Sim, _incident_sky,
                                                   _setup_live_goal, plane)


class _Exited(BaseException):
    pass


def _exit_recorder(codes):
    def _exit(code):
        codes.append(code)
        raise _Exited()
    return _exit


def test_no_false_positives_in_normal_operation():
    """The incident timeline with the fix in place never trips the self-check."""
    with Sim(_incident_sky) as sim:
        _setup_live_goal(sim)
        sim.run(1000)
    assert sim.d._stall_heals_total == 0 and sim.d._stall_started is None
    assert sim.fake.status["mode"] == "scoreboard" and sim.fake.status["problem"] is None


def test_no_false_positive_across_pause():
    def sky(rel):
        return [CESSNA, MAX8] if 10 <= rel < 300 else []
    with Sim(sky, paused=lambda rel: 60 <= rel < 160) as sim:
        _setup_live_goal(sim, goal_at=1e9)
        sim.run(400)
    assert sim.d._stall_heals_total == 0


def test_poll_gate_cap_bounds_a_stalled_marquee():
    """Even if plane_details never finishes a pass (any future scene stall), 2+ flights
    can no longer freeze polling: the cap forces a poll within MAX_DATA_HOLD_SECS (+1 tick)."""
    with Sim(lambda rel: [CESSNA, MAX8]) as sim, \
            mock.patch.object(display, "STALL_DETECT_SECS", 1e9):
        for i, kf in enumerate(sim.d.keyframes):
            if kf.__name__ == "plane_details":
                stub = lambda count: None                       # marquee frozen
                stub.properties = kf.__func__.properties
                stub.__name__ = "plane_details"
                sim.d.keyframes[i] = stub
                break
        sim.run(900)
    gaps = [b - a for a, b in zip(sim.fake.polls, sim.fake.polls[1:])]
    assert gaps and max(gaps) <= display.MAX_DATA_HOLD_SECS + display.POLL_INTERVAL + 0.5


def _swap_keyframe(d, name, make):
    for i, kf in enumerate(d.keyframes):
        if getattr(kf, "__name__", "") == name:
            new = make(kf)
            new.properties = kf.__func__.properties
            new.__name__ = name
            d.keyframes[i] = new
            return


def test_selfcheck_soft_heals_the_old_mutual_yield():
    """Re-create the pre-fix mutual yield (celebration_frame skips while flights are up,
    plane_details yields to the flag) so ONLY the self-check can rescue it."""
    with Sim(lambda rel: [CESSNA, MAX8] if rel >= 5 else []) as sim:
        _setup_live_goal(sim, goal_at=1e9)
        _swap_keyframe(sim.d, "celebration_frame",
                       lambda kf: (lambda count: None if len(sim.d._data) else kf(count)))
        _swap_keyframe(sim.d, "plane_details",
                       lambda kf: (lambda count: None if sim.d._goal_celebration_active else kf(count)))

        def wedge(s):
            s.d._goal_celebration_active = True
            s.d._celebration_until = s.clock.t - 60       # long expired
        sim.at(30, wedge)
        sim.run(120)
    assert sim.d._stall_heals_total == 1
    assert not sim.d._goal_celebration_active
    redrawn = sim.first_time(lambda s: "Cessna 172 Skyhawk" in s[4] or "Boeing 737 MAX 8" in s[4],
                             start=31)
    assert redrawn is not None and redrawn <= 30 + display.STALL_DETECT_SECS + 2
    assert sim.longest_blank(start=5) <= display.STALL_DETECT_SECS + 2


def test_selfcheck_exits_nonzero_when_heal_cannot_fix_it():
    """A permanently broken marquee (plane_details raises every frame) -> heals every
    STALL_HEAL_EVERY s, then os._exit(STALL_EXIT_CODE) so Restart=on-failure restarts."""
    codes = []

    def _always_raises(_kf):
        def kf(count):
            raise RuntimeError("marquee broken")
        return kf

    with mock.patch.object(display.os, "_exit", _exit_recorder(codes)), \
         mock.patch.object(display.time, "sleep"):
        with Sim(lambda rel: [CESSNA] if rel >= 5 else []) as sim:
            _setup_live_goal(sim, goal_at=1e9)
            _swap_keyframe(sim.d, "plane_details", _always_raises)
            try:
                sim.run(400)
            except _Exited:
                pass
    assert codes == [display.STALL_EXIT_CODE]
    pick = sim.first_time(lambda s: s[3] > 0)
    first = sim.d._stall_started            # frame clock: frame 0 == sim start
    assert pick <= first <= pick + display.STALL_DETECT_SECS + 2
    assert display.STALL_EXIT_SECS <= sim.rel() - first <= display.STALL_EXIT_SECS + 2
    assert sim.d._stall_heals >= 3


def test_wall_clock_steps_do_not_look_like_a_stall():
    """The Pi has no RTC: the boot-time NTP step can jump time.time() by hours.  The poll
    cap and self-check run on the frame clock, so a step either way is a non-event."""
    def jump(secs):
        def _j(s):
            s.clock.t += secs
        return _j

    codes = []
    with mock.patch.object(display.os, "_exit", _exit_recorder(codes)), \
         mock.patch.object(display.time, "sleep"):
        with Sim(lambda rel: [CESSNA, MAX8] if rel >= 5 else []) as sim:
            _setup_live_goal(sim, goal_at=1e9)
            sim.at(60, jump(3600))
            sim.at(200, jump(-7200))
            try:
                sim.run(400)
            except _Exited:
                pass
    assert codes == []
    assert sim.d._stall_heals_total == 0 and sim.d._stall_started is None


def test_unencodable_or_non_str_plane_name_still_scrolls():
    """A non-str or lone-surrogate type string used to make DrawText raise every frame."""
    odd = [dict(plane("ODD1", "x"), display_name=12345, plane=12345),
           dict(plane("ODD2", "x"), display_name="Boeing \ud800 737", plane="")]
    codes = []
    with mock.patch.object(display.os, "_exit", _exit_recorder(codes)), \
         mock.patch.object(display.time, "sleep"):
        with Sim(lambda rel: odd if rel >= 5 else []) as sim:
            _setup_live_goal(sim, goal_at=1e9)
            try:
                sim.run(200)
            except _Exited:
                pass
    assert codes == [] and sim.d._stall_heals_total == 0
    assert sim.first_time(lambda s: "12345" in s[4]) is not None
    assert sim.first_time(lambda s: any("Boeing" in t for t in s[4])) is not None


def test_overhead_logs_poll_starvation_once():
    from utilities import overhead
    o = overhead.Overhead.__new__(overhead.Overhead)
    import threading
    o._lock = threading.Lock()
    o._processing = False
    o._display_status = None
    o._starve_logged = False
    now = time.time()
    o._last_poll_start_ts = now - overhead.POLL_STARVE_LOG_SECS - 5
    with mock.patch.object(overhead, "_log") as log:
        assert o._log_if_starved(now) is True
        assert o._log_if_starved(now + 30) is True
        assert log.call_count == 1
        o._last_poll_start_ts = now + 31
        assert o._log_if_starved(now + 32) is False
        assert o._starve_logged is False
