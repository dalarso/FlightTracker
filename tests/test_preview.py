"""Regression tests for the FlightTracker desktop *preview* "view-only" safety guarantee.

``preview.py`` is an off-Pi desktop mirror that runs the REAL ``Display`` (every scene,
same fonts/colours/layout) on a machine that is NOT the Pi — typically the same Windows/Mac
box that runs the LAN plane-ding and goal-horn listener apps.  Because it runs the real
display code, it MUST NEVER emit the LAN side-effects that the physical panel emits:

  * the plane-ding   — ``utilities/planeding.py``           (``send_ding`` / ``send_state``)
  * the goal-horn     — ``scenes/sportscore.py``             (``_send_horn`` / ``_send_state``)

Otherwise the desktop would fire a SECOND ding/horn for every plane/goal the Pi already
announced (the "double-ding"), or worse, ding/horn while you only meant to *watch* a scene.

``preview.py`` guarantees view-only via TWO independent layers:

  (a) CONFIG-BLANK — ``_materialise_config()`` writes a generated ``config.py`` with
      ``PLANE_DING_HOST = ""`` and ``SCOREBOARD_GOAL_HORN_HOST = ""``.  An empty host means
      ``planeding._sock`` / ``sportscore._horn_sock`` are never opened at import (they stay
      ``None``), so every emitter early-returns and opens no UDP socket at all.

  (b) HARD-STUB — after import, ``preview.py`` reassigns ``planeding.send_ding`` /
      ``send_state`` and ``sportscore._send_horn`` / ``_send_state`` to no-ops, so even a
      mis-routed call path can't emit.

This guarantee is easy to silently break in a refactor.  Examples that these tests catch:

  * Someone drops ``PLANE_DING_HOST`` / ``SCOREBOARD_GOAL_HORN_HOST`` from the blank-list in
    ``_materialise_config`` (defeats layer (a)) — caught by ``PreviewSourceBlanksHosts``.
  * Someone removes / renames the hard-stub assignments (defeats layer (b)) — caught by
    ``PreviewSourceStubsEmitters``.
  * Someone changes ``planeding`` / ``sportscore`` so a blank host NO LONGER suppresses the
    socket (defeats layer (a) at the source) — caught by ``ConfigBlankSuppressesEmitters``.

What these tests do NOT do: import ``preview.py`` wholesale.  Importing it would materialise
the config from the LIVE Pi over the network and start the RGBMatrixEmulator window.  So for
the source-level guards (#2, #3) we read ``preview.py`` as TEXT, and for the runtime guard
(#1) we replicate just the blank-config mechanism in a temp dir and import the two emitter
modules fresh against it.

Run (the preview venv has ``requests``; ``rgbmatrix`` is stubbed here, repo is put on
``sys.path``)::

    ~/.ftpreview-venv/bin/python -m unittest tests.test_preview -v
"""

import ast
import importlib
import os
import socket
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

# ── Repo layout ──────────────────────────────────────────────────────────────────
# The two emitter modules live under the repo root as ``utilities.planeding`` and
# ``scenes.sportscore``; put the repo root on sys.path so they import as packages.
_ROOT = Path(__file__).resolve().parent.parent
_PREVIEW_PY = _ROOT / "preview.py"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# A documentation / test-net host (RFC 5737 TEST-NET-1) used ONLY by the positive-control
# case below, which proves these tests have teeth.  Packets are intercepted by a fake socket
# (see ``_SocketTripwire``) and never actually leave the machine — and even if they did,
# 192.0.2.0/24 is reserved for documentation and routes nowhere.
_TESTNET_HOST = "192.0.2.123"


# ── A socket factory that records every .sendto, so we can PROVE zero packets ─────
class _SocketSpy:
    """Stand-in for a ``socket.socket`` that never touches the network but records every
    ``.sendto`` into a shared list, so a test can assert the count is exactly zero (the
    view-only guarantee) — or, in the positive control, exactly one (proving the assertion
    can fail)."""

    def __init__(self, sends, *a, **k):
        self._sends = sends

    def setblocking(self, _flag):       # called right after construction in both modules
        pass

    def sendto(self, data, addr):
        # If this ever runs in the blank-config case, the test FAILS — a packet was emitted.
        self._sends.append((data, addr))
        return len(data)

    def close(self):
        pass


class _SocketTripwire:
    """Context manager that swaps ``socket.socket`` for a spy whose ``.sendto`` appends to
    ``self.sends``.  Installed BEFORE the fresh import so that if a module opens a socket at
    import (it must not, when the host is blank) we still capture any later ``.sendto``."""

    def __init__(self):
        self.sends = []
        self._patch = None

    def __enter__(self):
        spy_factory = lambda *a, **k: _SocketSpy(self.sends, *a, **k)
        self._patch = mock.patch("socket.socket", side_effect=spy_factory)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


# ── Fresh-import harness ──────────────────────────────────────────────────────────
_EMITTER_MODULES = ("utilities.planeding", "scenes.sportscore")


def _write_config(workdir: Path, ding_host: str, horn_host: str) -> None:
    """Write a generated ``config.py`` exactly like ``preview._materialise_config`` would:
    real values for everything the import chain reads, with the two LAN hosts set as given.
    A blank host ("") is the production-preview value; a non-blank host is the positive
    control.  We include a couple of unrelated keys so the modules' other ``_cfg`` reads
    resolve against THIS config and not a stray one."""
    body = textwrap.dedent(
        f"""
        PLANE_DING_HOST = {ding_host!r}
        SCOREBOARD_GOAL_HORN_HOST = {horn_host!r}
        # ports/cadence present so the modules read them from THIS config, not a fallback
        PLANE_DING_PORT = 50506
        PLANE_DING_PING_SECS = 5
        SCOREBOARD_GOAL_HORN_PORT = 50505
        SCOREBOARD_GOAL_HORN_PING_SECS = 5
        TIMEZONE = "America/Los_Angeles"
        """
    ).lstrip()
    (workdir / "config.py").write_text(body)


def _purge_emitter_modules():
    """Drop the emitter modules (and a bare top-level ``config``) from ``sys.modules`` so the
    next import re-executes their module bodies — that body is where ``_sock`` / ``_horn_sock``
    are decided from the host, so a fresh import is the ONLY way to observe the blank-host
    suppression faithfully."""
    for name in list(sys.modules):
        if name in _EMITTER_MODULES or name == "config":
            del sys.modules[name]


def _import_emitters_with_config(ding_host: str, horn_host: str):
    """Materialise a blanked-style config in a temp dir placed FIRST on ``sys.path`` (so a
    bare ``import config`` inside the modules resolves to it), stub ``rgbmatrix`` (sportscore
    imports ``from rgbmatrix import graphics`` and the ``setup`` package imports it too), then
    import both emitter modules FRESH inside a socket tripwire.

    Returns ``(planeding_module, sportscore_module, tripwire)``.
    """
    workdir = Path(tempfile.mkdtemp(prefix="ft_preview_test_"))
    _write_config(workdir, ding_host, horn_host)

    # rgbmatrix is NOT installed in the preview venv; stub it (and .graphics) so the
    # sportscore import chain (setup.colours / setup.fonts do ``from rgbmatrix import
    # graphics``) succeeds without the real LED library or the emulator.
    rgb_stub = mock.MagicMock(name="rgbmatrix")
    rgb_stub.graphics = mock.MagicMock(name="rgbmatrix.graphics")

    saved_path = list(sys.path)
    saved_modules = {k: sys.modules[k] for k in ("rgbmatrix", "rgbmatrix.graphics")
                     if k in sys.modules}
    tripwire = _SocketTripwire()
    try:
        sys.path.insert(0, str(workdir))         # generated config.py wins
        sys.modules["rgbmatrix"] = rgb_stub
        sys.modules["rgbmatrix.graphics"] = rgb_stub.graphics
        _purge_emitter_modules()
        tripwire.__enter__()
        planeding = importlib.import_module("utilities.planeding")
        sportscore = importlib.import_module("scenes.sportscore")
        return planeding, sportscore, tripwire
    finally:
        # Restore sys.path / rgbmatrix immediately; the caller still owns ``tripwire`` and is
        # responsible for exiting it once it has finished exercising the emitters.
        sys.path[:] = saved_path
        for k in ("rgbmatrix", "rgbmatrix.graphics"):
            sys.modules.pop(k, None)
        sys.modules.update(saved_modules)


def _restore_after_test():
    """Leave ``sys.modules`` clean so a later real import of these modules (e.g. another test
    module, or the suite re-running) starts from scratch rather than inheriting our test
    config's blanked sockets."""
    _purge_emitter_modules()


# ──────────────────────────────────────────────────────────────────────────────────
# #1  RUNTIME: the CONFIG-BLANK layer actually suppresses the emitters.
# ──────────────────────────────────────────────────────────────────────────────────
class ConfigBlankSuppressesEmitters(unittest.TestCase):
    """With a config exposing ``PLANE_DING_HOST = ""`` and ``SCOREBOARD_GOAL_HORN_HOST = ""``,
    importing the two emitter modules must leave their sockets ``None`` and calling the
    emitters must send ZERO UDP packets."""

    def tearDown(self):
        _restore_after_test()

    def test_blank_hosts_leave_sockets_none_and_emit_nothing(self):
        planeding, sportscore, tripwire = _import_emitters_with_config(
            ding_host="", horn_host="")
        try:
            # The host the module saw must be the blank one (proves our config won the path).
            self.assertEqual(planeding.PLANE_DING_HOST, "")
            self.assertEqual(sportscore.GOAL_HORN_HOST, "")

            # Layer (a): blank host ⇒ no socket opened at import.
            self.assertIsNone(planeding._sock,
                              "planeding._sock must stay None when PLANE_DING_HOST is blank")
            self.assertIsNone(sportscore._horn_sock,
                              "sportscore._horn_sock must stay None when "
                              "SCOREBOARD_GOAL_HORN_HOST is blank")

            # Exercise EVERY public emitter on both modules.  None may emit a packet.
            planeding.send_ding({"callsign": "TEST123", "origin": "LAS",
                                 "destination": "SFO", "plane": "B738"}, count=3)
            planeding.send_state([{"callsign": "TEST123", "origin": "LAS",
                                   "destination": "SFO", "plane": "B738"}], now_ts=10_000.0)

            sportscore._send_horn("GOAL", "VGK", 3, "EDM", 2)
            sportscore._send_state(
                [{"team_name": "VGK", "game": {"state": "LIVE", "team_score": 3,
                  "opp_abbr": "EDM", "opp_score": 2, "period_label": "P2",
                  "game_id": "1"}, "game_ended_at": None}],
                now_ts=10_000.0)
        finally:
            tripwire.__exit__(None, None, None)

        # The whole point: not a single ``.sendto`` happened.
        self.assertEqual(
            tripwire.sends, [],
            "view-only guarantee VIOLATED: a blank-host preview config still emitted "
            f"{len(tripwire.sends)} UDP packet(s): {tripwire.sends!r}")

    def test_positive_control_nonblank_host_DOES_emit(self):
        """Proves the test above has teeth: with a NON-blank host (and otherwise the identical
        mechanism) the very same modules DO open a socket and DO emit — so if a refactor ever
        stopped ``preview.py`` from blanking the hosts, the test above would genuinely fail
        rather than pass vacuously.  Packets are caught by the spy and never leave the box."""
        planeding, sportscore, tripwire = _import_emitters_with_config(
            ding_host=_TESTNET_HOST, horn_host=_TESTNET_HOST)
        try:
            self.assertEqual(planeding.PLANE_DING_HOST, _TESTNET_HOST)
            self.assertEqual(sportscore.GOAL_HORN_HOST, _TESTNET_HOST)

            # Non-blank host ⇒ a socket WAS opened at import (the spy stands in for it).
            self.assertIsNotNone(
                planeding._sock,
                "sanity: a non-blank PLANE_DING_HOST should open planeding._sock")
            self.assertIsNotNone(
                sportscore._horn_sock,
                "sanity: a non-blank SCOREBOARD_GOAL_HORN_HOST should open "
                "sportscore._horn_sock")

            planeding.send_ding({"callsign": "TEST123", "origin": "LAS",
                                 "destination": "SFO", "plane": "B738"}, count=1)
            sportscore._send_horn("GOAL", "VGK", 3, "EDM", 2)
        finally:
            tripwire.__exit__(None, None, None)

        # At least the ding and the horn must have tried to send — confirming the blank-host
        # test isn't passing for some unrelated reason (e.g. a globally-broken socket).
        self.assertGreaterEqual(
            len(tripwire.sends), 2,
            "positive control failed: a NON-blank host emitted no packets, so the "
            "zero-packet assertion in the blank-host test would pass vacuously")
        for _data, addr in tripwire.sends:
            self.assertEqual(addr[0], _TESTNET_HOST)


# ──────────────────────────────────────────────────────────────────────────────────
# Helpers for the source-level guards (#2, #3): read preview.py as TEXT.
# ──────────────────────────────────────────────────────────────────────────────────
def _preview_source() -> str:
    return _PREVIEW_PY.read_text()


def _materialise_config_source() -> str:
    """Return the source of ``preview._materialise_config`` (located via AST so a moved
    function or reordered file still resolves), as text — for the substring/literal checks."""
    tree = ast.parse(_preview_source(), filename=str(_PREVIEW_PY))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_materialise_config":
            return ast.get_source_segment(_preview_source(), node)
    raise AssertionError("preview.py no longer defines _materialise_config()")


# ──────────────────────────────────────────────────────────────────────────────────
# #2  SOURCE: preview.py STILL blanks BOTH hosts in _materialise_config's blank-list.
# ──────────────────────────────────────────────────────────────────────────────────
class PreviewSourceBlanksHosts(unittest.TestCase):
    """Guards layer (a) at the source: ``_materialise_config`` must list BOTH host keys among
    the names it blanks.  Dropping either from the literal would let the preview inherit a
    live host and fire LAN packets — this fails first."""

    def test_materialise_config_blanks_both_hosts(self):
        src = _materialise_config_source()
        for key in ("PLANE_DING_HOST", "SCOREBOARD_GOAL_HORN_HOST"):
            self.assertIn(
                f'"{key}"', src,
                f"preview._materialise_config no longer blanks {key!r}: the preview would "
                f"inherit the Pi's live host and emit LAN packets")

    def test_blanked_hosts_are_string_literals_in_the_blank_tuple(self):
        """Stronger than a substring: walk the AST of ``_materialise_config`` and confirm both
        host names appear as STRING CONSTANTS inside the ``for k in (...)`` blank-loop — so a
        stray mention in a comment/docstring wouldn't satisfy this, only a real blank-list
        entry does."""
        func = None
        tree = ast.parse(_preview_source(), filename=str(_PREVIEW_PY))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_materialise_config":
                func = node
                break
        self.assertIsNotNone(func, "preview.py no longer defines _materialise_config()")

        blanked_literals = set()
        for node in ast.walk(func):
            # Look for ``for k in (<str>, <str>, ...): cfg[k] = ""`` — collect the iterables'
            # string constants.  We collect every string-constant iterable in the function,
            # which in practice is exactly the blank-list tuple.
            if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
                for elt in node.iter.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        blanked_literals.add(elt.value)

        for key in ("PLANE_DING_HOST", "SCOREBOARD_GOAL_HORN_HOST"):
            self.assertIn(
                key, blanked_literals,
                f"{key!r} is not a string entry in _materialise_config's blank-list "
                f"loop (found: {sorted(blanked_literals)})")


# ──────────────────────────────────────────────────────────────────────────────────
# #3  SOURCE: preview.py STILL hard-stubs the four LAN emitters to no-ops.
# ──────────────────────────────────────────────────────────────────────────────────
class PreviewSourceStubsEmitters(unittest.TestCase):
    """Guards layer (b) at the source: ``preview.py`` must reassign each LAN emitter to a
    no-op.  Removing/renaming any stub (or, e.g., switching to ``from utilities.planeding
    import send_ding`` which would bind a local name the display calls and defeat the
    module-attribute stub) is caught here."""

    def _norm(self, text: str) -> str:
        # Collapse runs of whitespace so "send_ding  =" and "send_ding =" both match, and a
        # reformat (black/spaces) doesn't break the guard.
        return " ".join(text.split())

    def test_planeding_emitters_are_stubbed(self):
        src = self._norm(_preview_source())
        for attr in ("send_ding", "send_state"):
            self.assertIn(
                f"_planeding.{attr} = lambda", src,
                f"preview.py no longer hard-stubs planeding.{attr} to a no-op")

    def test_sportscore_emitters_are_stubbed(self):
        src = self._norm(_preview_source())
        for attr in ("_send_horn", "_send_state"):
            self.assertIn(
                f"_sb.{attr} = lambda", src,
                f"preview.py no longer hard-stubs sportscore.{attr} to a no-op")

    def test_stubs_target_the_real_emitter_modules(self):
        """The stubs only protect the display if ``_planeding`` and ``_sb`` are bound to the
        ACTUAL emitter modules.  Confirm preview.py imports them as those aliases (so a
        rename that points the alias elsewhere, leaving the real module's emitters live, is
        caught)."""
        src = self._norm(_preview_source())
        self.assertIn("from utilities import planeding as _planeding", src,
                      "preview.py must alias utilities.planeding as _planeding for the stubs "
                      "to land on the real plane-ding emitter")
        self.assertIn("import scenes.sportscore as _sb", src,
                      "preview.py must alias scenes.sportscore as _sb for the stubs to land "
                      "on the real goal-horn emitter")


if __name__ == "__main__":
    unittest.main(verbosity=2)
