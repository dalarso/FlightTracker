import os
import sys

from setup import frames
from utilities.animator import Animator
from utilities.overhead import Overhead

from scenes.weather import WeatherScene
from scenes.flightdetails import FlightDetailsScene
from scenes.journey import JourneyScene
from scenes.loadingpulse import LoadingPulseScene
from scenes.loadingled import LoadingLEDScene
from scenes.clock import ClockScene
from scenes.planedetails import PlaneDetailsScene
from scenes.day import DayScene
from scenes.date import DateScene

from rgbmatrix import graphics
from rgbmatrix import RGBMatrix, RGBMatrixOptions


def callsigns_match(flights_a, flights_b):
    return {f["callsign"] for f in flights_a} == {f["callsign"] for f in flights_b}


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

        # Track pause/night state so we can force redraws on transitions
        self._was_paused = os.path.exists(PAUSE_FLAG)
        self._was_night = os.path.exists(NIGHT_FLAG)

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

            if data_is_different:
                self._data_index = 0
                self._data_all_looped = False
                self._data = new_data

            # Only reset if there's flight data already
            # on the screen, or if there's some new
            # data available to draw which is different
            # from the current data
            reset_required = there_is_data and data_is_different

            if reset_required:
                self.reset_scene()

    def _reset_idle_scenes(self):
        """Force clock/date/day/weather to redraw on their next tick."""
        self._last_time = None
        self._last_date = None
        self._last_day = None
        # WeatherScene tracking — forces temperature and rainfall to erase+redraw
        self._last_temperature_str = None
        self._last_upcoming_rain_and_temp = None

    @Animator.KeyFrame.add(1)
    def sync(self, count):
        paused = os.path.exists(PAUSE_FLAG)
        night  = os.path.exists(NIGHT_FLAG)

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

    def run(self):
        try:
            # Start loop
            print("Press CTRL-C to stop")
            self.play()

        except KeyboardInterrupt:
            print("Exiting\n")
            sys.exit(0)
