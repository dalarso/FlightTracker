import sys
import traceback

from time import sleep

DELAY_DEFAULT = 0.01

# Rate-limit per-keyframe error logging so a persistently-throwing scene can't
# spam plane.log and thrash the SD card on a Pi.
_ERROR_LOG_EVERY = 600  # frames (~60s at 10fps) between logged tracebacks per keyframe


class Animator(object):
    class KeyFrame(object):
        @staticmethod
        def add(divisor, offset=0):
            def wrapper(func):
                func.properties = {"divisor": divisor, "offset": offset, "count": 0}
                return func

            return wrapper

    def __init__(self):
        self.keyframes = []
        self._reset_keyframes = []
        self.frame = 0
        self._delay = DELAY_DEFAULT
        # Per-keyframe-name frame index of the last logged traceback (rate-limiting).
        self._keyframe_error_frames = {}

        self._register_keyframes()

        super().__init__()

    def _register_keyframes(self):
        # Some introspection to setup keyframes
        seen = set()
        for methodname in dir(self):
            method = getattr(self, methodname)
            if hasattr(method, "properties"):
                # Keyframes are discovered by bare method name across many scene
                # mixins; a name collision would silently drop one keyframe (a whole
                # scene stops drawing). Make that a loud startup crash instead.
                if method.__name__ in seen:
                    raise RuntimeError(
                        f"duplicate keyframe {method.__name__}"
                    )
                seen.add(method.__name__)
                self.keyframes.append(method)
                if method.properties["divisor"] == 0:
                    self._reset_keyframes.append(method)

    def reset_scene(self):
        for keyframe in self._reset_keyframes:
            keyframe()

    def play(self):
        while True:
            for keyframe in self.keyframes:
                # Isolate each keyframe: a single scene raising (e.g. a malformed
                # field from the resolver cascade) must skip its frame, not tear
                # down the whole render loop and blank the panel.
                try:
                    # If divisor == 0 then only run once on first loop
                    if self.frame == 0:
                        if keyframe.properties["divisor"] == 0:
                            keyframe()

                    # Otherwise perform normal operation
                    if (
                        self.frame > 0
                        and keyframe.properties["divisor"]
                        and not (
                            (self.frame - keyframe.properties["offset"])
                            % keyframe.properties["divisor"]
                        )
                    ):
                        if keyframe(keyframe.properties["count"]):
                            keyframe.properties["count"] = 0
                        else:
                            keyframe.properties["count"] += 1
                except Exception:
                    self._log_keyframe_error(keyframe)
                    continue

            self.frame += 1
            sleep(self._delay)

    def _log_keyframe_error(self, keyframe):
        # Rate-limited per keyframe so a persistently-throwing scene can't spam
        # the log and thrash the SD card.
        name = getattr(keyframe, "__name__", repr(keyframe))
        last = self._keyframe_error_frames.get(name)
        if last is not None and (self.frame - last) < _ERROR_LOG_EVERY:
            return
        self._keyframe_error_frames[name] = self.frame
        print(f"Keyframe {name} raised:\n{traceback.format_exc()}", file=sys.stderr)

    @property
    def delay(self):
        return self._delay

    @delay.setter
    def delay(self, value):
        self._delay = value
