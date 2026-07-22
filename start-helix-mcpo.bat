@echo off
REM Deprecated name — the project was renamed to cymatix-context (0.8.0).
REM This forwarder calls start-cymatix-mcpo.bat and will be removed after
REM the deprecation window. Update Open WebUI launch scripts to the new name.
echo [start-helix-mcpo] renamed in 0.8.0 — forwarding to start-cymatix-mcpo.bat
call "%~dp0start-cymatix-mcpo.bat" %*
