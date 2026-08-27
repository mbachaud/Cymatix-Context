@echo off
REM Wrapper for launch.json — sets OTel env vars, then runs the launcher.
REM Uses `set "X=Y"` (quoted) so no trailing-whitespace bug.
cd /d "%~dp0"
set "CYMATIX_OTEL_ENABLED=1"
set "CYMATIX_OTEL_ENDPOINT=localhost:4317"
set "CYMATIX_OTEL_INSECURE=1"
set "CYMATIX_OTEL_SAMPLER_RATIO=1.0"
set "CYMATIX_USER=max"
set "CYMATIX_AGENT=raude"
python -m cymatix_context.launcher.app run --host 127.0.0.1 --port 11438
