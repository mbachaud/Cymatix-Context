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
REM CYMATIX_* is the canonical env prefix since 0.8.0; the package
REM mirrors each CYMATIX_X to HELIX_X at import. The block below also
REM adopts any HELIX_X already set in your shell, so old-prefix
REM deployments keep working unchanged.
REM ─────────────────────────────────────────────────────────────────

cd /d "%~dp0"

REM ── Adopt old-prefix env vars if the new ones aren't set ────────
if not defined CYMATIX_MCPO_PORT   if defined HELIX_MCPO_PORT   set "CYMATIX_MCPO_PORT=%HELIX_MCPO_PORT%"
if not defined CYMATIX_MCP_URL    if defined HELIX_MCP_URL    set "CYMATIX_MCP_URL=%HELIX_MCP_URL%"
if not defined CYMATIX_ORG        if defined HELIX_ORG        set "CYMATIX_ORG=%HELIX_ORG%"
if not defined CYMATIX_PARTY_ID   if defined HELIX_PARTY_ID   set "CYMATIX_PARTY_ID=%HELIX_PARTY_ID%"
if not defined CYMATIX_DEVICE     if defined HELIX_DEVICE     set "CYMATIX_DEVICE=%HELIX_DEVICE%"
if not defined CYMATIX_USER       if defined HELIX_USER       set "CYMATIX_USER=%HELIX_USER%"
if not defined CYMATIX_AGENT      if defined HELIX_AGENT      set "CYMATIX_AGENT=%HELIX_AGENT%"
if not defined CYMATIX_AGENT_KIND if defined HELIX_AGENT_KIND set "CYMATIX_AGENT_KIND=%HELIX_AGENT_KIND%"
if not defined CYMATIX_MCP_HANDLE if defined HELIX_MCP_HANDLE set "CYMATIX_MCP_HANDLE=%HELIX_MCP_HANDLE%"
if not defined CYMATIX_MCP_HOST   if defined HELIX_MCP_HOST   set "CYMATIX_MCP_HOST=%HELIX_MCP_HOST%"

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
REM mcpo re-execs the inner command on every restart; env vars above
REM propagate to the child python process (the CYMATIX_->HELIX_ mirror
REM runs inside that process at package import).
mcpo --port %CYMATIX_MCPO_PORT% -- python -m cymatix_context.mcp_server
