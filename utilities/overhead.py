import math
import requests
from threading import Thread, Lock
from time import sleep

from requests.exceptions import ConnectionError
from urllib3.exceptions import NewConnectionError
from urllib3.exceptions import MaxRetryError

try:
    from config import MIN_ALTITUDE
except (ModuleNotFoundError, NameError, ImportError):
    MIN_ALTITUDE = 0  # feet

try:
    from config import ZONE_HOME, LOCATION_HOME
    ZONE_DEFAULT = ZONE_HOME
    LOCATION_DEFAULT = LOCATION_HOME
except (ModuleNotFoundError, NameError, ImportError):
    ZONE_DEFAULT = {"tl_y": 62.61, "tl_x": -13.07, "br_y": 49.71, "br_x": 3.46}
    LOCATION_DEFAULT = [51.509865, -0.118092, 6371]

try:
    from config import DUMP1090_HOST
except (ModuleNotFoundError, NameError, ImportError):
    DUMP1090_HOST = "localhost"

DUMP1090_URL = f"http://{DUMP1090_HOST}:8080/data/aircraft.json"
ADSBDB_URL = "https://api.adsbdb.com/v0/callsign/{}"

RETRIES = 3
RATE_LIMIT_DELAY = 1
MAX_FLIGHT_LOOKUP = 5
MAX_ALTITUDE = 10000  # feet
EARTH_RADIUS_KM = 6371
BLANK_FIELDS = ["", "N/A", "NONE"]

_route_cache = {}


class Flight:
    def __init__(self, ac):
        self.latitude = ac.get("lat", 0)
        self.longitude = ac.get("lon", 0)
        alt = ac.get("alt_baro", 0)
        self.altitude = alt if isinstance(alt, (int, float)) else 0
        self.vertical_speed = ac.get("baro_rate", ac.get("geom_rate", 0)) or 0
        self.callsign = (ac.get("flight") or "").strip()
        self.plane_type = (ac.get("t") or "").strip()


def distance_from_flight_to_home(flight, home=LOCATION_DEFAULT):
    def polar_to_cartesian(lat, long, alt):
        DEG2RAD = math.pi / 180
        return [
            alt * math.cos(DEG2RAD * lat) * math.sin(DEG2RAD * long),
            alt * math.sin(DEG2RAD * lat),
            alt * math.cos(DEG2RAD * lat) * math.cos(DEG2RAD * long),
        ]

    def feet_to_meters_plus_earth(altitude_ft):
        altitude_km = 0.0003048 * altitude_ft
        return altitude_km + EARTH_RADIUS_KM

    try:
        (x0, y0, z0) = polar_to_cartesian(
            flight.latitude,
            flight.longitude,
            feet_to_meters_plus_earth(flight.altitude),
        )

        (x1, y1, z1) = polar_to_cartesian(*home)

        dist = math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2)

        return dist

    except AttributeError:
        # on error say it's far away
        return 1e6


def in_zone(flight, zone=ZONE_DEFAULT):
    return (
        zone["br_y"] <= flight.latitude <= zone["tl_y"]
        and zone["tl_x"] <= flight.longitude <= zone["br_x"]
    )


def get_route(callsign):
    if not callsign or callsign.upper() in BLANK_FIELDS:
        return "", ""
    if callsign in _route_cache:
        return _route_cache[callsign]
    try:
        r = requests.get(ADSBDB_URL.format(callsign), timeout=5)
        if r.status_code == 200:
            route = r.json().get("response", {}).get("flightroute", {})
            origin = (route.get("origin") or {}).get("iata_code", "") or ""
            destination = (route.get("destination") or {}).get("iata_code", "") or ""
            _route_cache[callsign] = (origin, destination)
            return origin, destination
    except Exception:
        pass
    _route_cache[callsign] = ("", "")
    return "", ""


class Overhead:
    def __init__(self):
        self._lock = Lock()
        self._data = []
        self._new_data = False
        self._processing = False

    def grab_data(self):
        Thread(target=self._grab_data).start()

    def _grab_data(self):
        with self._lock:
            self._new_data = False
            self._processing = True

        data = []

        try:
            response = requests.get(DUMP1090_URL, timeout=5)
            aircraft_list = response.json().get("aircraft", [])

            flights = [Flight(ac) for ac in aircraft_list if "lat" in ac and "lon" in ac]
            flights = [
                f for f in flights
                if MIN_ALTITUDE < f.altitude < MAX_ALTITUDE and in_zone(f)
            ]
            flights = sorted(flights, key=distance_from_flight_to_home)

            for flight in flights[:MAX_FLIGHT_LOOKUP]:
                sleep(RATE_LIMIT_DELAY)

                origin, destination = get_route(flight.callsign)

                plane = flight.plane_type if flight.plane_type.upper() not in BLANK_FIELDS else ""
                callsign = flight.callsign if flight.callsign.upper() not in BLANK_FIELDS else ""
                origin = origin if origin.upper() not in BLANK_FIELDS else ""
                destination = destination if destination.upper() not in BLANK_FIELDS else ""

                data.append(
                    {
                        "plane": plane,
                        "origin": origin,
                        "destination": destination,
                        "vertical_speed": flight.vertical_speed,
                        "altitude": flight.altitude,
                        "callsign": callsign,
                    }
                )

            with self._lock:
                self._new_data = True
                self._processing = False
                self._data = data

        except (ConnectionError, NewConnectionError, MaxRetryError):
            self._new_data = False
            self._processing = False

    @property
    def new_data(self):
        with self._lock:
            return self._new_data

    @property
    def processing(self):
        with self._lock:
            return self._processing

    @property
    def data(self):
        with self._lock:
            self._new_data = False
            return self._data

    @property
    def data_is_empty(self):
        return len(self._data) == 0


# Main function
if __name__ == "__main__":

    o = Overhead()
    o.grab_data()
    while not o.new_data:
        print("processing...")
        sleep(1)

    print(o.data)
