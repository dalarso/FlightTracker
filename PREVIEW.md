# FlightTracker desktop preview

A **pixel-identical mirror of the LED panel** in a desktop window, running on the Windows
machine (next to the horn/ding apps) — *not* on the Pi.

It runs the exact same `Display` (every scene, same fonts/colours/layout) under
[RGBMatrixEmulator](https://github.com/ty-porter/RGBMatrixEmulator), and **pulls
everything from the Pi's web API**, so:

- the **Pi does zero extra work** — it can't flicker the panel (the reason this isn't a Pi service);
- **no API keys** live on the Windows box — all data comes from the Pi;
- what you see **matches the panel**: same flights, same weather, same scoreboard, same
  night/pause state.

| Shown | Pulled from |
|---|---|
| Flight cards | `GET /api/flights` |
| Temperature | `GET /api/weather` |
| Scoreboard | `GET /api/scoreboard` |
| Night / pause | `GET /api/status` |
| Timezone, local airports, team name, … | `GET /api/config` (secrets masked & unused) |
| Clock / date / day | the local system clock (keep it NTP-synced) |

## Setup (Windows)

1. **Install Python 3** from python.org — the default installer includes `tkinter` (the
   window adapter). Tick "Add Python to PATH".
2. **Copy this whole `FlightTracker` folder** to the Windows machine (it needs `display/`,
   `scenes/`, `setup/`, `utilities/`).
3. In that folder:
   ```
   pip install -r requirements-preview.txt
   ```
4. **Run it:**
   ```
   python preview.py
   ```
   A window opens showing the live mirror. If the Pi isn't at the default address, set it:
   ```
   set FT_PI=http://192.168.1.50:5000
   python preview.py
   ```

### Double-click launch (like the horn/ding)
Create `Preview.bat` next to `preview.py`:
```bat
@echo off
set FT_PI=http://192.168.1.50:5000
python preview.py
```
Then double-click `Preview.bat` (or make a desktop shortcut to it).

## Options (environment variables)

| Var | Default | Notes |
|---|---|---|
| `FT_PI` | `http://192.168.1.50:5000` | Base URL of the Pi web UI |
| `FT_PREVIEW_ADAPTER` | `tkinter` | `pygame` (smoother — `pip install pygame` first) or `browser` (serves `localhost:8888`, open in a browser) |

## Notes
- Nothing is written to your real `config.py` — the preview generates a throwaway config
  (from the Pi) on a temp path.
- The pixel look is `pixel_style: real` (glowing LED dots); edit `preview.py`'s emulator
  config block to `square` for the exact 1:1 logical grid.
- Requires network access from this machine to the Pi's `:5000` (confirmed working).
