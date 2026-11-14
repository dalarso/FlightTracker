from rgbmatrix import graphics

from utilities.animator import Animator
from setup import colours, fonts, screen

# Setup
PLANE_DETAILS_COLOUR = colours.PINK
PLANE_DISTANCE_FROM_TOP = 30
PLANE_TEXT_HEIGHT = 9
PLANE_FONT = fonts.regular


class PlaneDetailsScene(object):
    def __init__(self):
        super().__init__()
        self.plane_position = screen.WIDTH
        self._data_all_looped = False
        # Marquee string + its measured width, cached per index/data so the dict
        # lookups aren't recomputed on every one of the ~10 frames/sec. Reset on
        # each index advance via reset_scrolling (fired by reset_scene).
        self._plane_text = None
        self._plane_text_length = None
        # Frame of the last successful marquee step (or restart) — read by the display
        # self-check: flights on screen + this not advancing == a render stall.
        self._marquee_frame = 0

    @Animator.KeyFrame.add(1)
    def plane_details(self, count):

        # Guard against no data
        if len(self._data) == 0:
            return

        # NOTE: no `_goal_celebration_active` yield here — flights always own the canvas
        # (celebration_frame suspends itself when flights appear).  Yielding here too was
        # one half of the 2026-10-08 mutual-yield deadlock that blanked the panel.

        # Resolve the marquee string once per index/data; it's invariant for the
        # duration of a marquee pass and only changes when the index advances.
        if self._plane_text is None:
            _text = (
                self._data[self._data_index].get("display_name")
                or self._data[self._data_index].get("plane", "")
            )
            # Always a str the C DrawText binding can encode: a non-str value or a lone
            # surrogate from an upstream type API would raise on EVERY frame, freezing the
            # marquee (and tripping the render self-check into restarts while it's overhead).
            self._plane_text = str(_text or "").encode("utf-8", "replace").decode("utf-8")

        # Draw background
        self.draw_square(
            0,
            PLANE_DISTANCE_FROM_TOP - PLANE_TEXT_HEIGHT,
            screen.WIDTH,
            screen.HEIGHT,
            colours.BLACK,
        )

        # Draw text. text_length is the return of DrawText, so capture it on the
        # first draw after a reset and reuse the cached width thereafter.
        text_length = graphics.DrawText(
            self.canvas,
            PLANE_FONT,
            self.plane_position,
            PLANE_DISTANCE_FROM_TOP,
            PLANE_DETAILS_COLOUR,
            self._plane_text,
        )
        if self._plane_text_length is None:
            self._plane_text_length = text_length

        # Handle scrolling
        self.plane_position -= 1
        self._marquee_frame = self.frame
        if self.plane_position + self._plane_text_length < 0:
            self.plane_position = screen.WIDTH
            if len(self._data) > 1:
                self._data_index = (self._data_index + 1) % len(self._data)
                self._data_all_looped = (not self._data_index) or self._data_all_looped
                self.reset_scene()

    @Animator.KeyFrame.add(0)
    def reset_scrolling(self):
        self.plane_position = screen.WIDTH
        self._marquee_frame = getattr(self, "frame", 0)
        # Invalidate the cached marquee string/width so the next frame re-resolves
        # them for the now-current index/data.
        self._plane_text = None
        self._plane_text_length = None
