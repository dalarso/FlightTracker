from datetime import datetime
from zoneinfo import ZoneInfo

from utilities.animator import Animator
from setup import colours, fonts, frames

from rgbmatrix import graphics

try:
    from config import TIMEZONE
except (ImportError, NameError):
    TIMEZONE = "America/Los_Angeles"

try:
    _TZ = ZoneInfo(TIMEZONE)
except Exception:
    _TZ = ZoneInfo("America/Los_Angeles")

# Setup
CLOCK_FONT = fonts.large_bold
CLOCK_POSITION = (0, 10)
CLOCK_COLOUR = colours.BLUE_DARK

# In 8×13B the colon glyph has 3 leading blank bitmap rows vs 1 for digits,
# making it appear lower than the digits.  Raise the colon by 1 px (user-tuned).
_COLON_Y_OFFSET = -1
_CHAR_PX = 8  # DWIDTH of 8×13B


def _draw_clock(canvas, time_str, colour):
    """Draw time string with colon raised to align with digit baseline."""
    if ":" not in time_str:
        graphics.DrawText(
            canvas, CLOCK_FONT, CLOCK_POSITION[0], CLOCK_POSITION[1], colour, time_str
        )
        return
    colon_idx = time_str.index(":")
    left  = time_str[:colon_idx]
    right = time_str[colon_idx + 1:]
    colon_x = CLOCK_POSITION[0] + len(left) * _CHAR_PX
    right_x  = colon_x + _CHAR_PX
    y = CLOCK_POSITION[1]
    graphics.DrawText(canvas, CLOCK_FONT, CLOCK_POSITION[0], y, colour, left)
    graphics.DrawText(canvas, CLOCK_FONT, colon_x, y + _COLON_Y_OFFSET, colour, ":")
    graphics.DrawText(canvas, CLOCK_FONT, right_x, y, colour, right)


class ClockScene(object):
    def __init__(self):
        super().__init__()
        self._last_time = None

    @Animator.KeyFrame.add(int(frames.PER_SECOND * 1))
    def clock(self, count):
        if len(self._data):
            # Ensure redraw when there's new data
            self._last_time = None

        elif getattr(self, "_scoreboard_active", False):
            # Hockey score is showing — suppress clock, reset so it
            # redraws immediately when hockey mode exits.
            self._last_time = None

        else:
            # If there's no data to display
            # then draw a clock
            now = datetime.now(_TZ)
            current_time = now.strftime("%H:%M")

            # Only draw if time needs updated
            if self._last_time != current_time:
                # Undraw last time if different from current
                if self._last_time is not None:
                    _draw_clock(self.canvas, self._last_time, colours.BLACK)
                self._last_time = current_time

                # Draw Time
                _draw_clock(self.canvas, current_time, CLOCK_COLOUR)
