import os
import sys
import time

from setup import frames
from utilities.animator import Animator
from utilities.overhead import Overhead
from utilities import planeding

from scenes.weather import WeatherScene
from scenes.flightdetails import FlightDetailsScene
from scenes.journey import JourneyScene
from scenes.loadingpulse import LoadingPulseScene
from scenes.loadingled import LoadingLEDScene
from scenes.clock import ClockScene
from scenes.planedetails import PlaneDetailsScene
from scenes.day import DayScene
from scenes.date import DateScene
from scenes.sportscore import SportScoreScene

from rgbmatrix import graphics
from rgbmatrix import RGBMatrix, RGBMatrixOptions


def _flight_key(f):
    # Key flights by (callsign, hex) so two distinct aircraft sharing a blank
    # callsign (common for GA) don't collapse to one. Fall back to callsign
    # alone if older data dicts have no "hex".
    return (f.get("callsign"), f.get("hex")) if "hex" in f else (f.get("callsign"),)


def callsigns_match(flights_a, flights_b):
    return {_flight_key(f) for f in flights_a} == {_flight_key(f) for f in flights_b}


try:
    from config import (
        BRIGHTNESS,
        GPIO_SLOWDOWN,
        HAT_PWM_ENABLED
    )
except (ImportError, NameError):
    BRIGHTNESS = 100
    GPIO_SLOWDOWN = 1
    HAT_PWM_ENABLED = True

try:
    from config import NIGHT_BRIGHTNESS
except (ImportError, NameError):
    NIGHT_BRIGHTNESS = 20

try:
    # Attempt to load experimental config data
    from config import LOADING_LED_ENABLED

except (ImportError, NameError):
    # If there's no experimental config data
    LOADING_LED_ENABLED = False

try:
    from config import POLL_INTERVAL
except (ImportError, NameError):
    POLL_INTERVAL = 15  # seconds between ADS-B receiver polls

try:
    from config import DATA_CHECK_INTERVAL
except (ImportError, NameError):
    DATA_CHECK_INTERVAL = 2  # seconds between processed-data pickup checks

# Clamp to safe minimums so KeyFrame.add(0) one-shot traps can't happen
POLL_INTERVAL       = max(5, int(POLL_INTERVAL))
DATA_CHECK_INTERVAL = max(1, int(DATA_CHECK_INTERVAL))

PAUSE_FLAG  = "/tmp/ft_paused"
NIGHT_FLAG  = "/tmp/ft_night"


class Display(
    WeatherScene,
    FlightDetailsScene,
    JourneyScene,
    LoadingLEDScene if LOADING_LED_ENABLED else LoadingPulseScene,
    PlaneDetailsScene,
    ClockScene,
    DayScene,
    DateScene,
    SportScoreScene,
    Animator,
):
    def __init__(self):
        # Setup Display
        options = RGBMatrixOptions()
        options.hardware_mapping = "adafruit-hat-pwm" if HAT_PWM_ENABLED else "adafruit-hat"
        options.rows = 32
        options.cols = 64
        options.chain_length = 1
        options.parallel = 1
        options.row_address_type = 0
        options.multiplexing = 0
        options.pwm_bits = 11
        options.brightness = BRIGHTNESS
        options.pwm_lsb_nanoseconds = 130
        options.led_rgb_sequence = "RGB"
        options.pixel_mapper_config = ""
        options.show_refresh_rate = 0
        options.gpio_slowdown = GPIO_SLOWDOWN
        options.disable_hardware_pulsing = True
        options.drop_privileges = True
        self.matrix = RGBMatrix(options=options)

        # Setup canvas
        self.canvas = self.matrix.CreateFrameCanvas()
        self.canvas.Clear()

        # Data to render
        self._data_index = 0
        self._data = []

        # Start Looking for planes
        self.overhead = Overhead()
        self.overhead.grab_data()

        # Initialise animator and scenes
        super().__init__()

        # Overwrite any default settings from
        # Animator or Scenes
        self.delay = frames.PERIOD

        # Track pause/night state so we can force redraws on transitions.
        # The flag files are polled once per second by refresh_flags() and
        # cached here; sync() (per-frame) reads the cached booleans instead of
        # stat-ing the filesystem every frame.
        self._paused = os.path.exists(PAUSE_FLAG)
        self._night = os.path.exists(NIGHT_FLAG)
        self._was_paused = self._paused
        self._was_night = self._night

    def draw_square(self, x0, y0, x1, y1, colour):
        for x in range(x0, x1):
            _ = graphics.DrawLine(self.canvas, x, y0, x, y1, colour)

    @Animator.KeyFrame.add(0)
    def clear_screen(self):
        # First operation after
        # a screen reset
        self.canvas.Clear()

    @Animator.KeyFrame.add(int(frames.PER_SECOND * DATA_CHECK_INTERVAL))
    def check_for_loaded_data(self, count):
        if self.overhead.new_data:
            # Check if there's data
            there_is_data = len(self._data) > 0 or not self.overhead.data_is_empty

            # this marks self.overhead.data as no longer new
            new_data = self.overhead.data

            # See if this matches the data already on the screen
            # This test only checks if it's 2 lists with the same
            # callsigns, regardless or order
            data_is_different = not callsigns_match(self._data, new_data)

            # True when we're currently showing flights and the incoming set is
            # empty — i.e. a flights→idle transition back to clock/date/day/weather.
            transitioning_to_idle = len(self._data) > 0 and len(new_data) == 0

            if not data_is_different:
                return  # same callsign set — keep showing/scrolling exactly what's up

            # The aircraft currently mid-scroll (if any flights are on screen).
            _cur     = self._data
            _cur_key = (_flight_key(_cur[self._data_index])
                        if _cur and 0 <= self._data_index < len(_cur) else None)
            _new_keys = {_flight_key(f) for f in new_data}

            # ── CONTINUE-IN-PLACE ─────────────────────────────────────────────────
            # Planes were added and/or removed, but the one currently mid-scroll is
            # still overhead.  Keep scrolling it instead of snapping back to plane 1
            # and restarting the marquee:
            #   • retained planes keep their on-screen order, so the active plane's
            #     "n/N" position indicator stays put (only the total ticks up/down);
            #   • the active plane keeps its OWN dict, so its text never changes mid-
            #     scroll; the others adopt the refreshed data;
            #   • departed planes drop out, genuinely-new planes append to the rotation.
            # No reset_scene(), no index→0, no scroll reset — the scroll just continues.
            if _cur_key is not None and _cur_key in _new_keys:
                _new_by_key = {_flight_key(f): f for f in new_data}
                _cur_keys   = {_flight_key(f) for f in _cur}
                _retained   = [(f if _flight_key(f) == _cur_key else _new_by_key[_flight_key(f)])
                               for f in _cur if _flight_key(f) in _new_keys]
                _added      = [f for f in new_data if _flight_key(f) not in _cur_keys]
                self._data       = _retained + _added
                self._data_index = next(i for i, f in enumerate(self._data)
                                        if _flight_key(f) == _cur_key)
                try:
                    if _added:   # ding only for genuinely-new aircraft; departures are silent
                        planeding.send_ding(_added[0], len(self._data))
                except Exception:
                    pass
                return

            # ── DISRUPTIVE / IDLE ─────────────────────────────────────────────────
            # The plane being shown has left (or nothing was on screen, or we're
            # going back to idle).  Reset to the first plane and restart, as before.
            _prev_keys = {_flight_key(f) for f in _cur}
            self._data_index = 0
            self._data_all_looped = False
            self._data = new_data
            try:
                _new = [f for f in new_data if _flight_key(f) not in _prev_keys]
                if _new:
                    planeding.send_ding(_new[0], len(new_data))
            except Exception:
                pass

            if there_is_data:    # data_is_different is True here
                self.reset_scene()
                if transitioning_to_idle:
                    # Going flights→idle: reset_scene() alone leaves the idle scenes
                    # (clock/date/day/weather) holding stale state, so they won't repaint
                    # until a value changes.  Force them to redraw.
                    self._reset_idle_scenes()

    def _reset_idle_scenes(self):
        """Force clock/date/day/weather/sport-score to redraw on their next tick."""
        self._last_time = None
        self._last_date = None
        self._last_day = None
        # WeatherScene tracking — forces temperature and rainfall to erase+redraw
        self._last_temperature_str = None
        self._last_upcoming_rain_and_temp = None
        # SportScoreScene tracking — forces score to redraw on next tick
        self._reset_sport_draws()

    @Animator.KeyFrame.add(int(frames.PER_SECOND))
    def refresh_flags(self, count):
        # Poll the pause/night flag files once per second (not every frame) and
        # cache the result; sync() reads these cached booleans.
        self._paused = os.path.exists(PAUSE_FLAG)
        self._night  = os.path.exists(NIGHT_FLAG)

    @Animator.KeyFrame.add(1)
    def sync(self, count):
        paused = self._paused
        night  = self._night

        if paused:
            # Blank the canvas every frame so nothing bleeds through
            self.canvas.Clear()
            self.matrix.brightness = 0
        else:
            if self._was_paused:
                # Transitioning back on — canvas is already blank from the
                # last paused frame; reset states so everything redraws.
                self._reset_idle_scenes()
            elif night != self._was_night:
                # Night mode toggled — force redraws so the brightness
                # change is immediately visible on all static elements.
                self._reset_idle_scenes()

            self.matrix.brightness = NIGHT_BRIGHTNESS if night else BRIGHTNESS

        self._was_paused = paused
        self._was_night  = night
        _ = self.matrix.SwapOnVSync(self.canvas)

    @Animator.KeyFrame.add(int(frames.PER_SECOND * POLL_INTERVAL))
    def grab_new_data(self, count):
        # Only grab data if we're not already searching
        # for planes, or if there's new data available
        # which hasn't been displayed.
        #
        # We also need wait until all previously grabbed
        # data has been looped through the display.
        #
        # Last, if our internal store of the data
        # is empty, try and grab data
        if not (self.overhead.processing and self.overhead.new_data) and (
            self._data_all_looped or len(self._data) <= 1
        ):
            self.overhead.grab_data()

    @Animator.KeyFrame.add(int(frames.PER_SECOND * 2))
    def ping_plane_ding(self, count):
        # Heartbeat → desktop listener shows connection status + what's overhead.
        # Throttled internally to PLANE_DING_PING_SECS; non-blocking and swallowed.
        try:
            planeding.send_state(self._data, time.time())
        except Exception:
            pass

    def run(self):
        try:
            # Start loop
            print("Press CTRL-C to stop")
            self.play()

        except KeyboardInterrupt:
            print("Exiting\n")
            sys.exit(0)
