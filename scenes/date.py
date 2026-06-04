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

try:
    from config import DATE_FORMAT
except (ImportError, NameError):
    DATE_FORMAT = "MDY"

# Setup
DATE_COLOUR = colours.PINK_DARKER
DATE_FONT = fonts.small
DATE_POSITION = (1, 31)


class DateScene(object):
    def __init__(self):
        super().__init__()
        self._last_date = None

    @Animator.KeyFrame.add(int(frames.PER_SECOND * 1))
    def date(self, count):
        if len(self._data):
            # Ensure redraw when there's new data
            self._last_date = None

        elif getattr(self, "_scoreboard_active", False):
            self._last_date = None

        else:
            # If there's no data to display
            # then draw the date
            now = datetime.now(_TZ)
            if DATE_FORMAT == "DMY":
                current_date = now.strftime("%-d/%-m/%Y")
            elif DATE_FORMAT == "YMD":
                current_date = now.strftime("%Y-%m-%d")
            else:
                current_date = now.strftime("%-m/%-d/%Y")

            # Only draw if date needs updated
            if self._last_date != current_date:
                # Undraw last date if different from current
                if self._last_date is not None:
                    _ = graphics.DrawText(
                        self.canvas,
                        DATE_FONT,
                        DATE_POSITION[0],
                        DATE_POSITION[1],
                        colours.BLACK,
                        self._last_date,
                    )
                self._last_date = current_date

                # Draw date
                _ = graphics.DrawText(
                    self.canvas,
                    DATE_FONT,
                    DATE_POSITION[0],
                    DATE_POSITION[1],
                    DATE_COLOUR,
                    current_date,
                )
