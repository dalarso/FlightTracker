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
   fallback window adapter). Tick "Add Python to PATH".
2. **Copy this whole `FlightTracker` folder** to the PC (it needs `display/`, `scenes/`,
   `setup/`, `utilities/`, **`fonts/`**). Put it somewhere **writable — not `C:\Program Files\`**.
3. In that folder:
   ```
   pip install -r requirements-preview.txt
   ```
   On Windows this also pulls **`tzdata`** (Python has no system time-zone DB there) and
   **`pygame-ce`** — a drop-in for pygame that *does* ship a Python-3.14 wheel and gives the
   glowing **`real`** LED look in a native window (regular pygame has no 3.14 Windows wheel).
4. **Run it** — double-click **`Preview.bat`**, or:
   ```
   python preview.py
   ```
   A native window opens showing the live mirror. With `pygame-ce` present the preview
   auto-selects it (`real` glow, identical to the Mac); without it you get a `tkinter` window
   with `circle` dots. If the Pi isn't at the default address, set it first:
   ```
   set FT_PI=http://192.168.1.50:5000
   ```

### Double-click launch
A **`Preview.bat`** is included next to `preview.py` — just double-click it (or make a desktop
shortcut). It sets `FT_PI` and opens the preview in a native window.

## Setup (macOS)

Homebrew's Python ships **without `tkinter`**, so the Mac preview uses the **pygame** window
backend instead (already in the preview venv). Two double-click launchers are provided:

- **`FlightTracker Preview.app`** (on the Desktop) — opens the window, no Terminal. Like a real app.
- **`Preview.command`** (next to `preview.py`) — same, but runs in a Terminal window.

Both `cd` to the repo, use the persistent venv at `~/.ftpreview-venv`, and pull from `FT_PI`
(default `http://192.168.1.50:5000`). To (re)build the venv:
```
python3 -m venv ~/.ftpreview-venv
~/.ftpreview-venv/bin/pip install -r requirements-preview.txt pygame
```
If the pygame window ever misbehaves, run with `FT_PREVIEW_ADAPTER=browser` and open
`http://localhost:8888`.

## Options (environment variables)

| Var | Default | Notes |
|---|---|---|
| `FT_PI` | `http://192.168.1.50:5000` | Base URL of the Pi web UI |
| `FT_PREVIEW_ADAPTER` | _auto_ | Auto-picks `pygame` (native window, glowing `real` LED look) if installed, else `tkinter` (`circle` dots). Windows: `pip install pygame-ce`. Or force `browser` (serves `localhost:8888`). |

## Notes
- Nothing is written to your real `config.py` — the preview generates a throwaway config
  (from the Pi) on a temp path.
- The pixel look is `pixel_style: real` (glowing LED dots); edit `preview.py`'s emulator
  config block to `square` for the exact 1:1 logical grid.
- Requires network access from this machine to the Pi's `:5000` (confirmed working).
