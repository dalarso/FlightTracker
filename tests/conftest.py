"""Shared pytest setup for the FlightTracker test suite.

Runs once, before any test module is imported, so every test sees ONE consistent
LED-hardware stub regardless of collection order.

Several test files install their own rgbmatrix stub at import time. When they ran
in a different order, the colour palette in setup.colours — built exactly once, at
first import, from rgbmatrix.graphics.Color — could end up constructed from a bare
MagicMock, making colour arithmetic yield MagicMocks and breaking order-independent
assertions (review finding #17: "no runner config; modules collide across the
shared interpreter"). Here we register a real numeric Color and pin setup.colours
immediately, so the palette is always real no matter which test runs first.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Keep any incidental on-disk state off the real repo DB.
os.environ.setdefault("FT_DATA_DIR", tempfile.mkdtemp(prefix="ft-test-"))


class _Color:
    """Minimal stand-in for rgbmatrix.graphics.Color with real numeric channels."""

    __slots__ = ("red", "green", "blue")

    def __init__(self, r=0, g=0, b=0):
        self.red, self.green, self.blue = r, g, b


_graphics = mock.MagicMock(name="rgbmatrix.graphics")
_graphics.Color = _Color
_rgbmatrix = mock.MagicMock(name="rgbmatrix")
_rgbmatrix.graphics = _graphics

for _name, _mod in (
    ("rgbmatrix", _rgbmatrix),
    ("rgbmatrix.graphics", _graphics),
    ("RPi", mock.MagicMock(name="RPi")),
    ("RPi.GPIO", mock.MagicMock(name="RPi.GPIO")),
):
    sys.modules.setdefault(_name, _mod)

# Pin the colour palette NOW, under the real Color, so a test that later swaps in
# its own (possibly MagicMock) rgbmatrix can't cause setup.colours to be rebuilt
# from it. setup.colours is import-cached after this, so the palette stays real.
try:
    import setup.colours  # noqa: F401
except Exception:
    pass
