"""Direct unit tests for the shared scoreboard selector (utilities.scoreboard_select).

The web + display sides both call select_active; tests/test_scoreboard_parity.py proves
they stay in agreement through the real call sites. These tests pin the contract of the
extracted function itself in isolation.
"""
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utilities.scoreboard_select import select_active  # noqa: E402


def _g(state):
    return {"state": state} if state else None


class SelectActive(unittest.TestCase):
    def test_live_beats_final_regardless_of_order(self):
        self.assertEqual(
            select_active([("NHL", _g("FINAL")), ("NFL", _g("LIVE"))]), "NFL")

    def test_crit_counts_as_live(self):
        self.assertEqual(
            select_active([("NHL", _g("FINAL")), ("MLB", _g("CRIT"))]), "MLB")

    def test_multiple_live_takes_first_in_order(self):
        self.assertEqual(
            select_active([("NFL", _g("LIVE")), ("NHL", _g("LIVE"))]), "NFL")

    def test_all_final_takes_first_in_order(self):
        self.assertEqual(
            select_active([("NHL", _g("FINAL")), ("NFL", _g("OFF"))]), "NHL")

    def test_future_and_none_are_never_selected(self):
        self.assertIsNone(select_active([("NHL", _g("FUT")), ("NFL", None)]))

    def test_empty_is_none(self):
        self.assertIsNone(select_active([]))

    def test_final_eligible_gate_skips_ineligible_finals(self):
        # The display passes a window check; an aged-out FINAL must be skipped.
        entries = [("NHL", _g("FINAL")), ("NFL", _g("FINAL"))]
        self.assertEqual(select_active(entries, final_eligible=lambda k: k == "NFL"), "NFL")
        self.assertIsNone(select_active(entries, final_eligible=lambda k: False))

    def test_final_eligible_does_not_gate_live(self):
        # A LIVE game is never subject to the post-game window.
        entries = [("NHL", _g("LIVE"))]
        self.assertEqual(select_active(entries, final_eligible=lambda k: False), "NHL")


if __name__ == "__main__":
    unittest.main(verbosity=2)
