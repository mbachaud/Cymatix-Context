@echo off
cd /d F:\tmp\hx-wiring
set HELIX_OTEL_ENABLED=1
set HELIX_OTEL_ENDPOINT=localhost:4317
set HELIX_OTEL_INSECURE=1
set HELIX_OTEL_SAMPLER_RATIO=1.0
set HELIX_BUDGET_ZONE=1
set HELIX_USER=max
set HELIX_GENOME_PATH=
python -m helix_context.launcher.app --tray --grafana-url "http://localhost:3000/d/helix-overview/helix-overview" --prometheus-url "http://localhost:9090/graph" > F:\tmp\qa-tray2.log 2>&1
