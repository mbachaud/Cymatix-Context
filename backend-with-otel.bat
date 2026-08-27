@echo off
REM Wrapper for launch.json — sets OTel env vars, then runs the backend directly.
cd /d "%~dp0"
set "CYMATIX_OTEL_ENABLED=1"
set "CYMATIX_OTEL_ENDPOINT=localhost:4317"
set "CYMATIX_OTEL_INSECURE=1"
set "CYMATIX_OTEL_SAMPLER_RATIO=1.0"
python -m uvicorn cymatix_context._asgi:app --host 127.0.0.1 --port 11437
