import sys
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from display import Display

# Stamp crash/exit log lines in the user's configured timezone so they line up with every
# other log line (overhead.py / web/server.py do the same).  config.py is user-authored and
# gitignored, so TIMEZONE may be missing AND ZoneInfo() can raise on a bad string — guard both.
try:
    from config import TIMEZONE
    _PACIFIC = ZoneInfo(TIMEZONE)
except Exception:
    _PACIFIC = ZoneInfo("America/Los_Angeles")


def _ts():
    return datetime.now(_PACIFIC).strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    # Create a display and start its animation.
    # Wrap in a broad exception handler so any Python-level crash is logged
    # to plane.log before the process exits.  C-level segfaults in the
    # rgbmatrix library cannot be caught here — those show up as silent
    # restarts in the systemd journal.
    try:
        run_text = Display()
        run_text.run()
    except KeyboardInterrupt:
        print(f"[{_ts()}] [display] CTRL-C — exiting", flush=True)
        sys.exit(0)
    except Exception:
        print(
            f"[{_ts()}] [display] FATAL uncaught exception — process will restart:\n"
            + traceback.format_exc(),
            flush=True,
        )
        sys.exit(1)
