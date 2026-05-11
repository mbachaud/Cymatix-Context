# deploy/otel/ — Docker observability stack (advanced)

The default helix install ships native observability binaries managed
by the tray launcher. To set those up without bringing up the tray, run
[`scripts/setup-grafana-telem.ps1`](../../scripts/setup-grafana-telem.ps1)
(Windows) or [`scripts/setup-grafana-telem.sh`](../../scripts/setup-grafana-telem.sh)
(Linux / macOS).

This Docker Compose stack is the alternate path — useful for:

- Production-shape deployment (containerized, declarative)
- Environments where native binaries don't fit (locked-down user dirs,
  multi-host shared observability, etc.)
- Fallback testing against a known-good runtime

Both runtimes are first-class. Choose by deployment shape, not status.

## Components

| Service       | Image                                          | Port |
| ------------- | ---------------------------------------------- | ---- |
| OTel Collector| otel/opentelemetry-collector-contrib:0.105.0   | 4317, 4318, 8889 |
| Prometheus    | prom/prometheus:v2.54.1                        | 9090 |
| Tempo         | grafana/tempo:2.6.0                            | 3200 |
| Loki          | grafana/loki:3.2.0                             | 3100 |
| Grafana       | grafana/grafana:11.3.0                         | 3000 |

Wire format, ports, and dashboard provisioning are bit-for-bit
identical to the native sidecar — only the receiver runtime differs.

## Run

```bash
cd deploy/otel
docker-compose up -d
```

## Configs

- `otel-collector-config.yaml` — collector pipelines (used verbatim by Docker; templated for native).
- `prometheus.yml` — scrape config.
- `tempo.yaml` — Tempo storage + metrics-generator config.
- `loki-config.yaml` — explicit Loki config (mounted into the loki service).
- `grafana/provisioning/` — datasources + dashboard provisioning.
- `grafana/dashboards/` — committed dashboard JSON (runtime-agnostic).

The native sidecar (`tools/native-otel/`) reads these same files but
substitutes Docker-DNS hostnames → `localhost` and Linux container paths
→ per-user state dirs at install time. See
`docs/specs/2026-05-04-native-observability-sidecar-design.md` §6.3.

## Stop

```bash
docker-compose down
```
