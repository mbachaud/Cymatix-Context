@echo off
REM Runs M2 against the EnterpriseRAG sharded fixture.
REM
REM Usage:
REM   benchmarks\run_m2_enterprise_rag.bat enterprise_rag_10k 100
REM   benchmarks\run_m2_enterprise_rag.bat enterprise_rag_50k 100
REM
REM Args:
REM   %1  fixture name (enterprise_rag_10k / enterprise_rag_50k)
REM   %2  --max-questions value
REM
REM Steps:
REM   1. kill any existing helix on port 11437
REM   2. start helix pointed at main.genome.db with HELIX_USE_SHARDS=1
REM   3. wait for /health genes > 0
REM   4. run bench_enterprise_rag.py
REM   5. cleanup helix

set FIXTURE_NAME=%1
set N_Q=%2
if "%FIXTURE_NAME%"=="" set FIXTURE_NAME=enterprise_rag_10k
if "%N_Q%"=="" set N_Q=100

set MAIN_DB=F:\Projects\helix-context\genomes\bench\matrix-sharded\%FIXTURE_NAME%\main.genome.db

echo === Running M2 for fixture=%FIXTURE_NAME% n=%N_Q% ===
echo MAIN_DB=%MAIN_DB%

cd /d F:\Projects\helix-context\.claude\worktrees\vibrant-easley-73d68a

echo [1/4] starting helix...
python benchmarks\start_helix_for_enterprise_rag.py --fixture "%MAIN_DB%" --sharded
if errorlevel 1 (
  echo helix failed to start
  exit /b 1
)

echo.
echo [2/4] running bench M2 haiku %N_Q% questions...
python benchmarks\bench_enterprise_rag.py --mode helix --model haiku --max-questions %N_Q% --types basic,semantic,intra_document_reasoning --external-helix

echo.
echo [3/4] killing helix...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'uvicorn helix_context._asgi' -and $_.ProcessId -ne $PID } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"

echo [4/4] done. summary in benchmarks\results\enterprise_rag_helix_haiku_*.
