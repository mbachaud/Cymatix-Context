#!/usr/bin/env bash
# Install native observability binaries on Linux/macOS.
# Capable, untested per spec §3 non-goal — refuses to run on platforms
# whose .versions row is a TODO placeholder.
#
# Spec: docs/specs/2026-05-04-native-observability-sidecar-design.md §6
#
# Exit codes:
#   0  success
#   1  setup error (missing .versions, unsupported platform)
#   2  pinned hash is a TODO placeholder for this platform
#   3  download failed (propagated from python helper)
#   4  archive hash mismatch (propagated from python helper)
#   5  expected binary not found inside archive / unknown archive format
#   6  render step failed

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python"
fi

VERSIONS="$REPO_ROOT/tools/native-otel/.versions"
[ -f "$VERSIONS" ] || { echo "[install] missing $VERSIONS" >&2; exit 1; }

case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)   PLATFORM="linux_amd64" ;;
    Darwin-arm64)   PLATFORM="darwin_arm64" ;;
    Darwin-x86_64)  PLATFORM="darwin_amd64" ;;
    *) echo "[install] unsupported platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

# Read .versions via Python (one source of truth for the parser).
SPEC_JSON="$($PYTHON -c "
import json, sys
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('$VERSIONS', 'rb') as f:
    print(json.dumps(tomllib.load(f)))
")"

# Maps: svc:relpath
read_binaries() {
    cat <<EOF
otelcol-contrib:collector/otelcol-contrib
prometheus:prometheus/prometheus
tempo:tempo/tempo
loki:loki/loki
grafana:grafana/bin/grafana-server
EOF
}

while IFS=":" read -r svc relpath; do
    abspath="$REPO_ROOT/tools/native-otel/$relpath"
    svcdir="$(dirname "$abspath")"
    expected="$(echo "$SPEC_JSON" | $PYTHON -c "import json,sys; s=json.load(sys.stdin); print(s['$svc']['sha256_$PLATFORM'])")"
    url="$(echo "$SPEC_JSON" | $PYTHON -c "import json,sys; s=json.load(sys.stdin); print(s['$svc']['url_$PLATFORM'])")"

    case "$expected" in
        TODO_*|PLAN_NOTE*)
            echo "[install][$svc] sha256 for $PLATFORM is placeholder ($expected). Fill .versions first." >&2
            exit 2
            ;;
    esac

    # `should-skip` is silent on missing-file / hash-drift (the expected
    # "please download" outcomes); verify-hash treats them as errors and
    # writes to stderr, which trips PowerShell 5.1 ErrorActionPreference=Stop
    # on the .ps1 sibling — keep both shells using the same predicate for
    # parity.
    if $PYTHON -m helix_context.launcher._install_helpers should-skip "$abspath" "$expected"; then
        echo "[install][$svc] up-to-date - skipping"
        continue
    fi

    echo "[install][$svc] downloading $url"
    tmp="$(mktemp -t helix-native-otel.XXXXXX)"
    $PYTHON -m helix_context.launcher._install_helpers download "$url" "$tmp" --timeout 120
    $PYTHON -m helix_context.launcher._install_helpers verify-hash "$tmp" "$expected"

    mkdir -p "$svcdir"
    staging="$(mktemp -d -t helix-native-otel-extract.XXXXXX)"
    case "$url" in
        *.tar.gz|*.tgz) tar -xzf "$tmp" -C "$staging" ;;
        *.zip)          unzip -q "$tmp" -d "$staging" ;;
        *) echo "[install][$svc] unknown archive format: $url" >&2; exit 5 ;;
    esac

    exename="$(basename "$abspath")"
    found="$(find "$staging" -name "$exename" -type f -print -quit || true)"
    [ -n "$found" ] || { echo "[install][$svc] $exename not found inside archive" >&2; exit 5; }
    cp "$found" "$abspath"
    chmod +x "$abspath"

    if [ "$svc" = "grafana" ]; then
        graf_root="$(find "$staging" -maxdepth 1 -type d -name 'grafana-*' -print -quit || true)"
        if [ -n "$graf_root" ]; then
            mkdir -p "$REPO_ROOT/tools/native-otel/grafana"
            cp -R "$graf_root"/* "$REPO_ROOT/tools/native-otel/grafana/"
        fi
    fi

    rm -f "$tmp"
    rm -rf "$staging"
    echo "[install][$svc] installed"
done < <(read_binaries)

echo "[install] Rendering runtime configs ..."
$PYTHON -m helix_context.launcher.observability_render render-all || {
    echo "[install] render step failed (helix_context.launcher.observability_render — landed by a later task)" >&2
    exit 6
}
echo "[install] Native observability install complete."
