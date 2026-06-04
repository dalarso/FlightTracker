#!/usr/bin/env python3
"""
VGK Goal Horn — LAN game mirror + horn for the FlightTracker scoreboard.

Open this window on the Windows machine.  It mirrors whatever the LED board is showing
and sounds the horn in sync, driven entirely by UDP packets from the Raspberry Pi:
  • "STATE|…"   — heartbeat every ~5 s carrying the board's current game (score, opponent,
    period) or "STATE|NONE".  Drives the connection status AND the on-screen game data,
    and resets the window in lock-step with the board's 30-min post-game window.
  • "GOAL|team|ts|opp|os"  — fired the instant the board shows "VGK GOAL!"; logs it +
    plays the goal horn.
  • "WIN|team|ts|opp|os"   — fired on "VGK WINS!"; logs it + plays the win sound.

UDP is connectionless — no socket is held open; the heartbeat is how we know the Pi's up.
CLOSE the window to go silent.  Mute to stay open but quiet.  If the Pi is off/unreachable
the status flips red — and the Pi is never affected either way.

AUDIO (put beside this script):
  • vgk_goal_horn.mp3  — goal horn (auto-downloads if missing).
  • vgk_win_horn.mp3   — OPTIONAL win sound; if absent, the goal horn plays on wins too.

Zero installs: stdlib tkinter + the built-in Windows MCI audio API (ctypes).
Auto-start at login: Win+R → shell:startup → drop a shortcut to this file there.
"""

import os
import socket
import threading
import ctypes
import datetime
import time
import tkinter as tk
from tkinter import font as tkfont

# ── Config (must match the Pi) ──────────────────────────────────────────────
UDP_PORT   = 50505                      # = SCOREBOARD_GOAL_HORN_PORT on the Pi
STALE_SECS = 14                         # no heartbeat this long → "offline" (Pi pings ~5 s)
LOG_MAX    = 20                         # keep only the last N log rows (auto-drops the oldest)
_HERE      = os.path.dirname(os.path.abspath(__file__))
HORN_FILE  = os.path.join(_HERE, "vgk_goal_horn.mp3")
WIN_FILE   = os.path.join(_HERE, "vgk_win_horn.mp3")     # optional; falls back to goal horn
HORN_URL   = "https://static.wixstatic.com/mp3/84911f_6de65a3de84c4ec98b6cf00c58e361fd.mp3"
GOLD, STEEL, DARK, PANEL = "#C8A032", "#333F48", "#0B0E11", "#11161b"
GREEN, RED, AMBER, WHITE, GREY = "#3fb950", "#e06c75", "#d9a441", "#e6edf3", "#7a8a99"

# ── Audio via Windows MCI (no dependencies) ─────────────────────────────────
_mci = ctypes.windll.winmm.mciSendStringW
def _cmd(s): return _mci(s, None, 0, 0)
def play_file(path):
    _cmd("close horn")
    if _cmd(f'open "{path}" type mpegvideo alias horn') != 0:
        _cmd(f'open "{path}" alias horn')
    _cmd("play horn from 0")
def win_sound():
    return WIN_FILE if os.path.exists(WIN_FILE) else HORN_FILE
def ensure_horn_file():
    # One-time convenience download if the mp3 is absent (replace the file to use your own).
    if os.path.exists(HORN_FILE):
        return True
    try:
        import urllib.request, tempfile
        with urllib.request.urlopen(HORN_URL, timeout=8) as r:   # bounded — never hang the GUI
            data = r.read()
        if len(data) < 1024:            # too small to be a real mp3 (error page / truncated body)
            return False
        # Write to a temp file then atomically rename, so an interrupted download can never
        # leave a truncated file masquerading as a valid horn (which would silently not play).
        fd, tmp = tempfile.mkstemp(dir=_HERE, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, HORN_FILE)
        except Exception:
            try: os.remove(tmp)
            except OSError: pass
            return False
        return os.path.exists(HORN_FILE)
    except Exception:
        return False


class HornApp:
    def __init__(self, root):
        self.root      = root
        self.stop      = threading.Event()
        self.muted     = tk.BooleanVar(value=False)
        self.last_ping = 0.0
        self.bind_err  = None
        self.game_id   = None           # current game; change → reset score + log
        self.mp3_ok    = ensure_horn_file()

        root.title("VGK Goal Horn")
        root.configure(bg=DARK)
        root.geometry("384x396")
        root.resizable(False, False)

        f_title = tkfont.Font(family="Segoe UI", size=13, weight="bold")
        f_dot   = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        f_score = tkfont.Font(family="Segoe UI", size=22, weight="bold")
        f_state = tkfont.Font(family="Segoe UI", size=11)
        f_sml   = tkfont.Font(family="Segoe UI", size=9)
        f_log   = tkfont.Font(family="Consolas", size=9)

        tk.Label(root, text="\U0001F6A8  VGK GOAL HORN  \U0001F6A8", bg=DARK, fg=GOLD, font=f_title).pack(pady=(12, 2))
        self.status = tk.Label(root, text="●  Starting…", bg=DARK, fg=AMBER, font=f_dot)
        self.status.pack()

        self.score = tk.Label(root, text="Waiting for game", bg=DARK, fg=WHITE, font=f_score)
        self.score.pack(pady=(14, 0))
        self.state = tk.Label(root, text="Ready for the next one.", bg=DARK, fg=GREY, font=f_state)
        self.state.pack(pady=(0, 8))

        tk.Label(root, text="HORN LOG", bg=DARK, fg="#566573", font=f_sml).pack(anchor="w", padx=18)
        wrap = tk.Frame(root, bg=PANEL); wrap.pack(fill="both", expand=True, padx=14, pady=(2, 6))
        sb = tk.Scrollbar(wrap); sb.pack(side="right", fill="y")
        self.log = tk.Listbox(wrap, bg=PANEL, fg="#cfd8e3", font=f_log, bd=0, highlightthickness=0,
                              selectbackground=STEEL, activestyle="none", height=7, yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True); sb.config(command=self.log.yview)

        row = tk.Frame(root, bg=DARK); row.pack(pady=(0, 8))
        tk.Checkbutton(row, text="Mute", variable=self.muted, bg=DARK, fg="#cfd8e3", selectcolor=STEEL,
                       activebackground=DARK, activeforeground="white", font=f_sml).pack(side="left", padx=6)
        tk.Button(row, text="Test goal", command=lambda: self.on_goal("VGK", "1", "XXX", "0"),
                  bg=STEEL, fg="white", relief="flat", font=f_sml, padx=8, pady=2).pack(side="left", padx=6)
        tk.Button(row, text="Test win", command=lambda: self.on_win("VGK", "1", "XXX", "0"),
                  bg=STEEL, fg="white", relief="flat", font=f_sml, padx=8, pady=2).pack(side="left", padx=6)
        tk.Button(row, text="Clear log", command=self.clear_log,
                  bg=STEEL, fg="white", relief="flat", font=f_sml, padx=8, pady=2).pack(side="left", padx=6)

        if not self.mp3_ok:
            self._log("! vgk_goal_horn.mp3 missing — put it beside this file")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        threading.Thread(target=self.listen, daemon=True).start()
        self.tick_status()

    # ── networking ──────────────────────────────────────────────────────────
    def listen(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # No SO_REUSEADDR: a 2nd instance should fail to bind (→ a red "Can't
            # listen" status) instead of silently splitting the Pi's packets.
            s.bind(("0.0.0.0", UDP_PORT))
            s.settimeout(0.5)
        except Exception as e:
            self.bind_err = str(e)
            return
        while not self.stop.is_set():
            try:
                data, _ = s.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if self.stop.is_set():              # window closed mid-recv — don't touch a dead root
                break
            self.last_ping = time.time()        # any packet proves the Pi is alive
            try:
                parts = data.decode("utf-8", "replace").split("|")
            except Exception:
                continue
            self.root.after(0, self.handle, parts)
        try: s.close()
        except Exception: pass

    def handle(self, p):
        kind = p[0] if p else ""
        if kind == "STATE":
            self.on_state(p[1:])
        elif kind == "GOAL" and len(p) >= 5:
            self.on_goal(p[1], p[2], p[3], p[4])
        elif kind == "WIN" and len(p) >= 5:
            self.on_win(p[1], p[2], p[3], p[4])

    # ── game state (mirrors the board) ────────────────────────────────────────
    def on_state(self, f):
        if not f or f[0] == "NONE" or len(f) < 7:
            self.set_idle()
            return
        state, team, ts, opp, os_, period, gid = f[0], f[1], f[2], f[3], f[4], f[5], f[6]
        if gid and gid != self.game_id:                 # new game → reset
            self.game_id = gid
            self.log.delete(0, tk.END)
            self._log(f"--- {team} vs {opp} ---")
        self.score.config(text=f"{team}   {ts} - {os_}   {opp}", fg=WHITE)
        if state in ("LIVE", "CRIT"):
            self.state.config(text=period or "Live", fg=GREY)
        elif state in ("FINAL", "OFF"):
            try: won = int(ts) > int(os_)
            except Exception: won = False
            self.state.config(text=("FINAL  —  VEGAS WINS!" if won else "FINAL"),
                              fg=(GREEN if won else GREY))
        else:
            self.set_idle()

    def set_idle(self):
        if self.game_id is not None:                    # game just cleared (post-game window ended)
            self.game_id = None
            self.log.delete(0, tk.END)
        self.score.config(text="Waiting for game", fg=WHITE)
        self.state.config(text="Ready for the next one.", fg=GREY)

    # ── horn events ───────────────────────────────────────────────────────────
    def on_goal(self, team, ts, opp, os_):
        self.score.config(text=f"{team}   {ts} - {os_}   {opp}", fg=WHITE)   # instant, with the horn
        self._log(f"{self._t()}   GOAL   {team} {ts}-{os_}")
        if not self.muted.get():
            try: play_file(HORN_FILE)
            except Exception: pass

    def on_win(self, team, ts, opp, os_):
        self._log(f"{self._t()}   WIN!   {team} {ts}-{os_}")
        if not self.muted.get():
            try: play_file(win_sound())
            except Exception: pass

    # ── helpers ───────────────────────────────────────────────────────────────
    def _t(self):
        return datetime.datetime.now().strftime("%I:%M:%S %p").lstrip("0")

    def _log(self, text):
        self.log.insert(0, "  " + text)         # newest on top
        if self.log.size() > LOG_MAX:           # keep only the last LOG_MAX rows (also clears per game)
            self.log.delete(tk.END)             # drop the oldest (now at the bottom)
        self.log.see(0)                         # keep the newest in view

    def clear_log(self):
        self.log.delete(0, tk.END)              # manual wipe (e.g. after running tests)

    def tick_status(self):
        if self.bind_err:
            self.status.config(text=f"●  Can't listen on :{UDP_PORT}", fg=RED)
        elif self.last_ping == 0.0:
            self.status.config(text="●  Waiting for Pi…", fg=AMBER)
        elif time.time() - self.last_ping <= STALE_SECS:
            self.status.config(text=f"●  Connected to Pi   ·   UDP :{UDP_PORT}", fg=GREEN)
        else:
            self.status.config(text=f"●  Pi offline ({int(time.time() - self.last_ping)}s)", fg=RED)
        if not self.stop.is_set():
            self.root.after(1000, self.tick_status)

    def on_close(self):
        self.stop.set()
        try: _cmd("close horn")
        except Exception: pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    HornApp(root)
    root.mainloop()
