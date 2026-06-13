#!/bin/bash
# FlightTracker live preview — double-click to open the LED-panel mirror in a window.
# Pulls everything from the Pi; runs entirely on this Mac (no load on the Pi).
cd "$(dirname "$0")" || exit 1
export FT_PI="${FT_PI:-http://raspberrypi.local:5000}"
export FT_PREVIEW_ADAPTER="${FT_PREVIEW_ADAPTER:-pygame}"   # set to 'browser' to use localhost:8888 instead
exec "$HOME/.ftpreview-venv/bin/python" preview.py
