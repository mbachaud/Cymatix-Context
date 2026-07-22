@echo off
REM Deprecated name — the project was renamed to cymatix-context (0.8.0).
REM This forwarder calls start-cymatix-tray.bat and will be removed after
REM the deprecation window. Re-pin taskbar/desktop shortcuts to the new name
REM (or rerun setup-cymatix.bat, which refreshes them).
echo [start-helix-tray] renamed in 0.8.0 — forwarding to start-cymatix-tray.bat
call "%~dp0start-cymatix-tray.bat" %*
