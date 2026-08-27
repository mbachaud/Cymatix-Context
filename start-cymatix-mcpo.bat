@echo off
REM ─────────────────────────────────────────────────────────────────
REM mcpo launcher — exposes cymatix MCP (stdio) as an OpenAPI server
REM so Open WebUI (and any other OpenAPI-consuming frontend) can call
REM cymatix tools from an Ollama chat session.
REM
REM Flow:
REM   Open WebUI  ──OpenAPI──▶  mcpo :8788  ──stdio MCP──▶  python -m cymatix_context.mcp_server
REM                                                          └──HTTP──▶  cymatix FastAPI :11437
REM
REM Prereqs:
REM   1. Cymatix FastAPI must be running on :11437 (start-cymatix-tray.bat
REM      or backend-with-otel.bat). This script waits for it.
REM   2. `pip install mcpo` in the same Python env that runs cymatix.
REM
REM To customize: copy to start-cymatix-mcpo.local.bat (gitignored) and
REM edit there — port, agent identity, log verbosity.
REM
REM Environment variables use the canonical CYMATIX_* prefix.
REM ─────────────────────────────────────────────────────────────────

cd /d "%~dp0"

REM ── mcpo port (Open WebUI registers this as an OpenAPI server) ──
if "%CYMATIX_MCPO_PORT%"=="" set CYMATIX_MCPO_PORT=8788

REM ── Cymatix upstream the MCP shim talks to ──────────────────────
if "%CYMATIX_MCP_URL%"=="" set CYMATIX_MCP_URL=http://127.0.0.1:11437

REM ── 4-layer federation identity — distinct from Claude Code's ──
REM Collision guard: if CYMATIX_AGENT is empty OR equals "laude", force
REM "openwebui" so this MCPO session doesn't merge with Claude Code's.
if "%CYMATIX_ORG%"==""        set CYMATIX_ORG=swiftwing
REM CYMATIX_PARTY_ID / CYMATIX_DEVICE identify this machine in CWoLa + session registry.
REM Change to your own party id (operator's preferred stable identifier).
if not defined CYMATIX_PARTY_ID set "CYMATIX_PARTY_ID=%COMPUTERNAME%"
if not defined CYMATIX_DEVICE set "CYMATIX_DEVICE=%COMPUTERNAME%"
if "%CYMATIX_USER%"==""       set CYMATIX_USER=max
if "%CYMATIX_AGENT%"=="laude" set CYMATIX_AGENT=openwebui
if "%CYMATIX_AGENT%"==""      set CYMATIX_AGENT=openwebui
if "%CYMATIX_AGENT_KIND%"=="" set CYMATIX_AGENT_KIND=ollama-chat
if "%CYMATIX_MCP_HANDLE%"=="" set CYMATIX_MCP_HANDLE=%CYMATIX_AGENT%
if "%CYMATIX_MCP_HOST%"==""   set CYMATIX_MCP_HOST=ollama-chat

REM ── Wait for cymatix :11437 to answer /health (up to ~60s) ──────
echo [mcpo] waiting for cymatix at %CYMATIX_MCP_URL% ...
set /a _tries=0
:wait_cymatix
curl.exe -s -f -o NUL --max-time 2 "%CYMATIX_MCP_URL%/health" && goto cymatix_ready
set /a _tries+=1
if %_tries% GEQ 30 (
  echo [mcpo] cymatix did not answer after 60s — start it with start-cymatix-tray.bat first.
  exit /b 1
)
timeout /t 2 /nobreak >NUL
goto wait_cymatix
:cymatix_ready
echo [mcpo] cymatix is up. Launching mcpo on :%CYMATIX_MCPO_PORT% as agent=%CYMATIX_AGENT%

REM ── Launch mcpo wrapping the stdio cymatix MCP ──────────────────
mcpo --port %CYMATIX_MCPO_PORT% -- python -m cymatix_context.mcp_server
