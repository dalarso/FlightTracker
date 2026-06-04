@echo off
REM FlightTracker desktop preview — double-click to open the LED panel mirror in a NATIVE window.
REM Pulls everything live from the Pi; runs entirely on this PC (no load on the Pi).
REM
REM For the glowing 'real' LED look (matches the Mac), install pygame-ce once:
REM     pip install pygame-ce
REM preview.py then auto-selects it.  Without it you get a tkinter window with 'circle' dots.
REM Set FT_PI below if the Pi isn't at the default address.

if "%FT_PI%"=="" set FT_PI=http://192.168.1.50:5000
cd /d "%~dp0"
python preview.py
echo.
echo (preview exited)
pause
