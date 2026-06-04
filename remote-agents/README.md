# Remote Agents (optional desktop companions)

Two tiny **Windows desktop apps** that mirror what the LED board is doing and play a sound,
over fire-and-forget **UDP** on your LAN. They are completely optional — the FlightTracker
Pi runs fine whether or not these are open, and if the desktop is off/asleep the packets
simply vanish (the Pi is never blocked or affected either way).

**Zero installs:** pure Python standard library — `tkinter` for the window and the built-in
Windows **MCI** audio API (via `ctypes`) for sound. No `pip install` anything.

| App | Folder | Listens | Fires when… | Plays |
|-----|--------|---------|-------------|-------|
| **Goal Horn** | `goal-horn/` | UDP **50505** | the scoreboard shows a goal / win for your team | `vgk_goal_horn.mp3` (and optional `vgk_win_horn.mp3`) |
| **Plane Ding** | `plane-ding/` | UDP **50506** | a **new** aircraft is put on the matrix | `plane_ding.mp3` |

Both also show a live status (Connected / Pi offline), mirror the current game / plane, keep
a short scrolling log, and have **Mute**, **Test**, and **Clear log** buttons.

## Setup

1. Copy the app's folder (e.g. `goal-horn/`) to the Windows machine.
2. Put the sound file **next to the `.pyw`**:
   - `plane-ding/plane_ding.mp3` — any short chime you like (swap the file to change the sound).
   - `goal-horn/vgk_goal_horn.mp3` — your goal horn. If it's missing, the app does a **one-time
     download** of a default horn from a public URL (see the note below); replace the file to
     use your own. Optionally add `vgk_win_horn.mp3` for a distinct end-of-game win sound.
3. Double-click the `.pyw` (runs with no console window).
4. **Auto-start at login (optional):** press `Win+R`, type `shell:startup`, and drop a shortcut
   to the `.pyw` in that folder.

## Enable it on the Pi

The Pi only sends these packets when the matching host is set in `config.py`
(empty = feature off, the sender is a complete no-op):

```python
# Goal horn → goal-horn app
SCOREBOARD_GOAL_HORN_HOST      = "192.168.1.30"   # the Windows machine's LAN IP ("" = off)
SCOREBOARD_GOAL_HORN_PORT      = 50505
SCOREBOARD_GOAL_HORN_PING_SECS = 5             # heartbeat cadence

# Plane ding → plane-ding app
PLANE_DING_HOST      = "192.168.1.30"             # ("" = off)
PLANE_DING_PORT      = 50506
PLANE_DING_PING_SECS = 5
```

Use the desktop machine's **IP address** (not a hostname) so the Pi never does a DNS lookup
on its render path.

## Notes

- **UDP is connectionless** — nothing is held open. The heartbeat (~every 5 s) is how the app
  knows the Pi is alive; no heartbeat for ~14 s flips the status to "Pi offline."
- **Close the window** to go silent; **Mute** keeps it open but quiet.
- **Goal-horn one-time download:** on first launch, if `vgk_goal_horn.mp3` is absent, the app
  fetches a default horn once over HTTPS and writes it atomically beside the script. It is the
  app's only network call. To avoid it entirely, just drop your own `vgk_goal_horn.mp3` in
  first. (The Pi sender does **not** download anything.)
- The Pi-side senders live in `utilities/planeding.py` (ding) and `scenes/sportscore.py`
  (goal horn); the matching wire formats are documented at the top of each `.pyw`.
