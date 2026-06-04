#!/usr/bin/env python3
"""
Plane Ding — LAN listener + chime for the FlightTracker overhead display.

Open this window on the Windows machine.  It plays a "ding" you choose the instant the
LED board puts a NEW aircraft on the matrix, and mirrors what's overhead — driven entirely
by UDP packets from the Raspberry Pi:
  • "STATE|…"  — heartbeat every ~5 s carrying the plane currently on screen (callsign,
    route, type, how many are up) or "STATE|NONE".  Drives the connection status AND the
    on-screen "what's overhead" panel.
  • "DING|callsign|origin|dest|type|count"  — fired the instant a new plane goes up; logs
    it + plays your chosen ding.

UDP is connectionless — no socket is held open; the heartbeat is how we know the Pi's up.
CLOSE the window to go silent.  Mute to stay open but quiet.  If the Pi is off/unreachable
the status flips red — and the Pi is never affected either way.

SOUND (put beside this script):
  • plane_ding.mp3  — the ding played on every new plane.  Swap the file to change it.

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
UDP_PORT   = 50506                       # = PLANE_DING_PORT on the Pi
STALE_SECS = 14                          # no heartbeat this long → "offline" (Pi pings ~5 s)
LOG_MAX    = 20                          # keep only the last N log rows (auto-drops the oldest)
_HERE      = os.path.dirname(os.path.abspath(__file__))
DING_FILE  = os.path.join(_HERE, "plane_ding.mp3")   # put your ding here, beside this script
SKY, STEEL, DARK, PANEL = "#5ac8fa", "#2f3b45", "#0B0E11", "#11161b"
GREEN, RED, AMBER, WHITE, GREY = "#3fb950", "#e06c75", "#d9a441", "#e6edf3", "#7a8a99"

# ── Audio via Windows MCI (no dependencies) ─────────────────────────────────
_mci = ctypes.windll.winmm.mciSendStringW
def _cmd(s): return _mci(s, None, 0, 0)
def play_file(path):
    _cmd("close ding")
    if _cmd(f'open "{path}" type mpegvideo alias ding') != 0:   # mp3
        _cmd(f'open "{path}" alias ding')                       # wav / other
    _cmd("play ding from 0")


class PlaneApp:
    def __init__(self, root):
        self.root      = root
        self.stop      = threading.Event()
        self.muted     = tk.BooleanVar(value=False)
        self.last_ping = 0.0
        self.bind_err  = None
        self.ding_ok   = os.path.exists(DING_FILE)

        root.title("Plane Ding")
        root.configure(bg=DARK)
        root.geometry("384x404")
        root.resizable(False, False)

        f_title = tkfont.Font(family="Segoe UI", size=13, weight="bold")
        f_dot   = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        f_call  = tkfont.Font(family="Segoe UI", size=22, weight="bold")
        f_det   = tkfont.Font(family="Segoe UI", size=11)
        f_sml   = tkfont.Font(family="Segoe UI", size=9)
        f_log   = tkfont.Font(family="Consolas", size=9)

        tk.Label(root, text="✈  PLANE OVERHEAD  ✈", bg=DARK, fg=SKY, font=f_title).pack(pady=(12, 2))
        self.status = tk.Label(root, text="●  Starting…", bg=DARK, fg=AMBER, font=f_dot)
        self.status.pack()

        self.callsign = tk.Label(root, text="Clear skies", bg=DARK, fg=WHITE, font=f_call)
        self.callsign.pack(pady=(14, 0))
        self.detail = tk.Label(root, text="Waiting for a plane overhead…", bg=DARK, fg=GREY, font=f_det)
        self.detail.pack(pady=(0, 8))

        tk.Label(root, text="PLANE LOG", bg=DARK, fg="#566573", font=f_sml).pack(anchor="w", padx=18)
        wrap = tk.Frame(root, bg=PANEL); wrap.pack(fill="both", expand=True, padx=14, pady=(2, 6))
        sb = tk.Scrollbar(wrap); sb.pack(side="right", fill="y")
        self.log = tk.Listbox(wrap, bg=PANEL, fg="#cfd8e3", font=f_log, bd=0, highlightthickness=0,
                              selectbackground=STEEL, activestyle="none", height=7, yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True); sb.config(command=self.log.yview)

        row = tk.Frame(root, bg=DARK); row.pack(pady=(0, 10))
        tk.Checkbutton(row, text="Mute", variable=self.muted, bg=DARK, fg="#cfd8e3", selectcolor=STEEL,
                       activebackground=DARK, activeforeground="white", font=f_sml).pack(side="left", padx=6)
        tk.Button(row, text="Test", command=lambda: self.on_ding("UAL123", "LAS", "SFO", "B738", "1"),
                  bg=STEEL, fg="white", relief="flat", font=f_sml, padx=10, pady=2).pack(side="left", padx=6)
        tk.Button(row, text="Clear log", command=self.clear_log,
                  bg=STEEL, fg="white", relief="flat", font=f_sml, padx=8, pady=2).pack(side="left", padx=6)

        if not self.ding_ok:
            self._log("! plane_ding.mp3 missing — put it beside this file")

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
        elif kind == "DING" and len(p) >= 5:
            self.on_ding(p[1], p[2], p[3], p[4], p[5] if len(p) > 5 else "1")

    # ── overhead state (mirrors the board) ────────────────────────────────────
    def on_state(self, f):
        if not f or f[0] == "NONE" or len(f) < 4:
            self.set_idle()
            return
        cs, org, dst, typ = f[0], f[1], f[2], f[3]
        count = f[4] if len(f) > 4 else "1"
        self.show_plane(cs, org, dst, typ, count, fg=WHITE)

    def set_idle(self):
        self.callsign.config(text="Clear skies", fg=WHITE)
        self.detail.config(text="Nothing overhead right now.", fg=GREY)

    def show_plane(self, cs, org, dst, typ, count, fg):
        self.callsign.config(text=cs or "—", fg=fg)
        line = f"{org or '?'}  →  {dst or '?'}" + (f"     {typ}" if typ else "")
        try:
            if count and int(count) > 1:
                line += f"      ({count} overhead)"
        except Exception:
            pass
        self.detail.config(text=line, fg=GREY)

    # ── ding event ────────────────────────────────────────────────────────────
    def on_ding(self, cs, org, dst, typ, count="1"):
        self.show_plane(cs, org, dst, typ, count, fg=SKY)        # flash the callsign in sky-blue
        tail = f"  {typ}" if typ else ""
        self._log(f"{self._t()}  ✈ {cs or 'N/A'}  {org or '?'}→{dst or '?'}{tail}")
        if not self.muted.get():
            try: play_file(DING_FILE)
            except Exception: pass

    # ── helpers ───────────────────────────────────────────────────────────────
    def _t(self):
        return datetime.datetime.now().strftime("%I:%M:%S %p").lstrip("0")

    def _log(self, text):
        self.log.insert(0, "  " + text)         # newest on top
        if self.log.size() > LOG_MAX:           # keep only the last LOG_MAX rows
            self.log.delete(tk.END)             # drop the oldest (now at the bottom)
        self.log.see(0)                         # keep the newest in view

    def clear_log(self):
        self.log.delete(0, tk.END)              # manual wipe

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
        try: _cmd("close ding")
        except Exception: pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    PlaneApp(root)
    root.mainloop()
