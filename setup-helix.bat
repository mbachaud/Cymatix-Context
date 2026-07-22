@echo off
REM Deprecated name — the project was renamed to cymatix-context (0.8.0).
REM This forwarder calls setup-cymatix.bat and will be removed after the
REM deprecation window. Update shortcuts/scripts to the new name.
echo [setup-helix] renamed in 0.8.0 — forwarding to setup-cymatix.bat
call "%~dp0setup-cymatix.bat" %*
