"""Animator resilience tests (bucket: display).

The Animator.play() loop wraps every keyframe in try/except so one scene raising
(e.g. a malformed field from the resolver cascade) skips its frame instead of
tearing down the whole render loop and blanking the panel — the single most
load-bearing resilience mechanism in the display.  The review found this had no
direct test; these cover it:

  * a keyframe that raises EVERY frame does not stop its sibling keyframes
  * the frame counter keeps advancing through the failures
  * per-keyframe error logging is rate-limited (logged once, then suppressed)

animator.py pulls in no LED hardware, so it imports directly with no stubbing.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import utilities.animator as animator_mod          # noqa: E402
from utilities.animator import Animator            # noqa: E402


class _Stop(Exception):
    """Raised from the patched sleep() to break play()'s infinite loop in a test."""


class _Harness(Animator):
    @Animator.KeyFrame.add(1)
    def good(self, count):
        self.good_runs += 1

    @Animator.KeyFrame.add(1)
    def bad(self, count):
        self.bad_runs += 1
        raise ValueError("boom")


def _run_frames(a, frames):
    """Drive play() for `frames` ticks, then break out via the patched sleep()."""
    ticks = {"n": 0}

    def _sleep(_delay):
        ticks["n"] += 1
        if ticks["n"] >= frames:
            raise _Stop

    with mock.patch.object(animator_mod, "sleep", _sleep):
        with mock.patch.object(animator_mod.sys, "stderr"):   # swallow the tracebacks
            try:
                a.play()
            except _Stop:
                pass


class AnimatorResilience(unittest.TestCase):
    def setUp(self):
        self.a = _Harness()
        self.a.good_runs = 0
        self.a.bad_runs = 0

    def test_throwing_keyframe_does_not_stop_siblings(self):
        _run_frames(self.a, 6)
        # divisor==1 keyframes run on every frame > 0; with 6 ticks that's frames 1..5.
        self.assertGreaterEqual(self.a.good_runs, 4)
        # The sibling ran exactly as often as the thrower — the exception was contained,
        # not allowed to skip the rest of the keyframe list for that frame.
        self.assertEqual(self.a.good_runs, self.a.bad_runs)

    def test_frame_counter_advances_through_failures(self):
        _run_frames(self.a, 6)
        self.assertGreaterEqual(self.a.frame, 5)

    def test_keyframe_error_logging_is_rate_limited(self):
        _run_frames(self.a, 8)
        # Logged on first failure (frame 1) and then suppressed for _ERROR_LOG_EVERY frames,
        # so the recorded "last logged" frame must stay at the first failure, not advance
        # every frame — otherwise a persistent thrower would spam the log / thrash the SD card.
        self.assertIn("bad", self.a._keyframe_error_frames)
        self.assertEqual(self.a._keyframe_error_frames["bad"], 1)


if __name__ == "__main__":
    unittest.main()
