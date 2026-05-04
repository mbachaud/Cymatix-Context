<#
.SYNOPSIS
  Install native observability binaries for the helix tray launcher.

.DESCRIPTION
  Reads tools/native-otel/.versions, downloads each component from its
  pinned release URL, verifies SHA256, extracts to per-service folders
  under tools/native-otel/, then renders runtime configs.

  Idempotent: re-runs skip components whose binary hash already matches.

  Archive handling: dispatches on URL extension. Both .zip (Prometheus,
  Loki, Grafana on Windows) and .tar.gz (otelcol-contrib, Tempo on
  Windows) are supported via Expand-Archive and bundled tar.exe
  respectively, with a Python tarfile fallback if tar is unavailable.

.PARAMETER WhatIf
  Show what would be downloaded without actually fetching.

.NOTES
  Spec: docs/specs/2026-05-04-native-observability-sidecar-design.md §6

  Exit codes:
    0  success
    1  setup error (missing .versions, parse failure, render failure)
    2  pinned hash is a TODO placeholder for this platform
    3  download failed
    4  archive hash mismatch
    5  expected binary not found inside archive
#>

[CmdletBinding(SupportsShouldProcess=$true)]
param(
  [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# Locate venv python (preferred) or fall back to PATH.
$python = "python"
if (Test-Path "$RepoRoot\.venv\Scripts\python.exe") {
    $python = "$RepoRoot\.venv\Scripts\python.exe"
}

$versionsFile = Join-Path $RepoRoot "tools\native-otel\.versions"
if (-not (Test-Path $versionsFile)) {
    Write-Error "[install] $versionsFile not found"
    exit 1
}

# Parse .versions via Python (TOML — Windows PowerShell has no native parser).
$specJson = & $python -c @"
import json, sys
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open(r'$versionsFile', 'rb') as f:
    print(json.dumps(tomllib.load(f)))
"@
if ($LASTEXITCODE -ne 0) {
    Write-Error "[install] Failed to parse $versionsFile"
    exit 1
}
$spec = $specJson | ConvertFrom-Json

$platform = "windows_amd64"

# Maps service-name -> (target-binary subpath inside its folder).
$binaries = [ordered]@{
    "otelcol-contrib" = "collector\otelcol-contrib.exe"
    "prometheus"      = "prometheus\prometheus.exe"
    "tempo"           = "tempo\tempo.exe"
    "loki"            = "loki\loki.exe"
    "grafana"         = "grafana\bin\grafana-server.exe"
}

foreach ($svc in $binaries.Keys) {
    $relPath = $binaries[$svc]
    $absPath = Join-Path $RepoRoot "tools\native-otel\$relPath"
    $svcDir  = Split-Path -Parent $absPath
    $expected = $spec.$svc."sha256_$platform"
    $url      = $spec.$svc."url_$platform"

    if ($null -eq $expected -or $expected.StartsWith("TODO_") -or $expected.StartsWith("PLAN_NOTE")) {
        Write-Error "[install][$svc] Hash for $platform is a placeholder ($expected). Fill in .versions before installing."
        exit 2
    }

    # Skip if already installed and matches.
    & $python -m helix_context.launcher._install_helpers verify-hash $absPath $expected 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[install][$svc] up-to-date (sha256 ok) - skipping"
        continue
    }

    if (-not $PSCmdlet.ShouldProcess($svc, "download $url")) {
        continue
    }

    Write-Host "[install][$svc] downloading $url"
    $tmpArchive = Join-Path $env:TEMP "helix-native-otel-$svc.tmp"
    & $python -m helix_context.launcher._install_helpers download $url $tmpArchive --timeout 120
    if ($LASTEXITCODE -ne 0) {
        Write-Error "[install][$svc] download failed"
        exit 3
    }

    # Verify archive against the pinned hash (each project's release page
    # publishes the archive sha; that's what we pin and what we verify).
    & $python -m helix_context.launcher._install_helpers verify-hash $tmpArchive $expected
    if ($LASTEXITCODE -ne 0) {
        Write-Error "[install][$svc] hash check failed"
        exit 4
    }

    # Extract — .zip vs .tar.gz handled inline. Tempo + otelcol-contrib
    # ship .tar.gz on Windows; the others ship .zip.
    New-Item -ItemType Directory -Force -Path $svcDir | Out-Null
    $stagingDir = Join-Path $env:TEMP "helix-native-otel-$svc-extract"
    if (Test-Path $stagingDir) { Remove-Item -Recurse -Force $stagingDir }
    New-Item -ItemType Directory -Force -Path $stagingDir | Out-Null

    if ($url.EndsWith(".zip")) {
        Expand-Archive -Path $tmpArchive -DestinationPath $stagingDir -Force
    } elseif ($url.EndsWith(".tar.gz") -or $url.EndsWith(".tgz")) {
        # tar.exe ships with Windows since 17063; fall back to python tarfile.
        & tar -xf $tmpArchive -C $stagingDir
        if ($LASTEXITCODE -ne 0) {
            & $python -c "import tarfile; tarfile.open(r'$tmpArchive').extractall(r'$stagingDir')"
            if ($LASTEXITCODE -ne 0) {
                Write-Error "[install][$svc] tar.gz extraction failed (both tar and python tarfile)"
                exit 5
            }
        }
    } else {
        Write-Error "[install][$svc] unknown archive format: $url"
        exit 5
    }

    # Find the target binary anywhere inside the staging tree and copy it.
    $exeName = Split-Path -Leaf $absPath
    $found = Get-ChildItem -Path $stagingDir -Recurse -Filter $exeName -File | Select-Object -First 1
    if ($null -eq $found) {
        Write-Error "[install][$svc] $exeName not found inside $tmpArchive"
        exit 5
    }
    Copy-Item -Path $found.FullName -Destination $absPath -Force

    # For grafana we also need the static + bin + conf trees, not just the binary.
    if ($svc -eq "grafana") {
        $grafRoot = Get-ChildItem -Path $stagingDir -Directory | Where-Object { $_.Name -like "grafana-*" } | Select-Object -First 1
        if ($null -ne $grafRoot) {
            $dest = Join-Path $RepoRoot "tools\native-otel\grafana"
            Copy-Item -Recurse -Force "$($grafRoot.FullName)\*" $dest
        }
    }

    Remove-Item -Force $tmpArchive
    Remove-Item -Recurse -Force $stagingDir
    Write-Host "[install][$svc] installed"
}

# Render runtime configs. The render module lands in a later task in
# the native-observability-sidecar plan; this call will fail until then.
Write-Host "[install] Rendering runtime configs to tools/native-otel/configs/ ..."
& $python -m helix_context.launcher.observability_render render-all
if ($LASTEXITCODE -ne 0) {
    Write-Error "[install] render step failed (helix_context.launcher.observability_render — landed by a later task in the native-observability-sidecar plan)"
    exit 6
}

Write-Host "[install] Native observability install complete."

# Write the completion sentinel — the helix tray polls for this file
# every 2 s and auto-restarts the launcher when it appears, so the
# freshly-installed binaries get picked up without the user having to
# manually quit + re-launch. Sentinel is removed by the tray after
# detection so re-runs don't re-trigger the auto-restart.
$sentinelPath = Join-Path $RepoRoot "tools\native-otel\.install-complete"
New-Item -Path $sentinelPath -ItemType File -Force | Out-Null
Write-Host "[install] Wrote completion sentinel: $sentinelPath"
Write-Host "[install] Helix tray will auto-restart shortly. You can close this window."
Start-Sleep -Seconds 2
