# Spec: per-lane OTel attribution (`service.name` / `service.instance.id` / `helix.lane`)

**Date:** 2026-06-02 · **Author:** Laude (spec) → Raude (impl) · **Size:** ~6 lines across 2 files · **Risk:** low (additive, backward-compatible telemetry-resource change; no retrieval behavior touched)

## Why
Two helix daemons (dev on 11437, bench on a 2nd port) currently emit **identical `service.name="helix-context"`**, so Prometheus/Tempo/Grafana **merge them indistinguishably**. This is the lone internals item gating the "tray + bench coexist with per-port visibility" story (report §4.1). `service.name` is hardcoded at the call site and `Resource.create` reads no env override.

**Why env-only doesn't work today:** `Resource.create({SERVICE_NAME: service_name, ...})` passes `service.name` explicitly, which **overrides** the SDK's env-detected `OTEL_SERVICE_NAME`. (A custom attr via `OTEL_RESOURCE_ATTRIBUTES=helix.lane=bench` *would* merge in — but the primary dashboard dimension is `service.name`, which stays merged.) Hence a code touch is required for the `service.name` split.

## Env contract (new)
| Var | Default | Effect |
|---|---|---|
| `HELIX_OTEL_SERVICE_NAME` | `helix-context` | overrides `service.name`; set `helix-dev` / `helix-bench` per lane |
| `HELIX_LANE` | _(unset)_ | free-form lane tag → `helix.lane` resource attr (also usable by identity/session layer) |
| `HELIX_OTEL_INSTANCE_ID` | `host:port` (from call site) → else `socket.gethostname()` | `service.instance.id` |

Unset → behavior is unchanged except `service.instance.id` newly populates from the bound port (additive, harmless).

## Change 1 — `helix_context/telemetry/otel.py`

**(a) import — add `SERVICE_INSTANCE_ID`** (~line 251):
```python
# before
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
# after
from opentelemetry.sdk.resources import (
    Resource, SERVICE_NAME, SERVICE_VERSION, SERVICE_INSTANCE_ID,
)
```

**(b) signature — add optional `service_instance_id`** (~line 213):
```python
def setup_telemetry(
    app: Any = None,
    service_name: str = "helix-context",
    service_version: str = "0.4.0b",
    service_instance_id: Optional[str] = None,   # NEW
) -> bool:
```

**(c) resolve env overrides + build the resource** — replace the `resource = Resource.create({...})` block (~line 276). Insert the resolution **before** it (it must precede the `get_tracer`/`get_meter` calls at ~292/~324, which use `service_name` as the instrumentation scope):
```python
    # ── Per-lane attribution: env overrides the passed defaults so two
    # daemons (dev :11437, bench :11439) emit distinct, splittable telemetry.
    service_name = os.environ.get("HELIX_OTEL_SERVICE_NAME", service_name)
    instance_id = (
        os.environ.get("HELIX_OTEL_INSTANCE_ID")
        or service_instance_id
        or socket.gethostname()
    )
    _resource_attrs = {
        SERVICE_NAME: service_name,
        SERVICE_VERSION: service_version,
        SERVICE_INSTANCE_ID: instance_id,
        # COMPUTERNAME is Windows-only; fall back to socket.gethostname()
        "deployment.host": os.environ.get("COMPUTERNAME") or socket.gethostname(),
    }
    _lane = os.environ.get("HELIX_LANE", "").strip()
    if _lane:
        _resource_attrs["helix.lane"] = _lane
    resource = Resource.create(_resource_attrs)
```
(`socket` is already imported in `otel.py`; `Optional` is already imported via typing — confirm.)

## Change 2 — `helix_context/server/app.py` (~line 190 call site)
Pass the bound port so `service.instance.id` is always populated even without the env (env still wins):
```python
# before
setup_telemetry(app, service_name="helix-context")
# after
setup_telemetry(
    app,
    service_name="helix-context",
    service_instance_id=f"{config.server.host}:{config.server.port}",
)
```
(`config` is in scope in `create_app`; `ServerConfig.host`/`.port` exist — `config.py:176-177`.)

## Launch (post-change)
```
# dev lane (tray-managed, 11437)
set HELIX_OTEL_SERVICE_NAME=helix-dev & set HELIX_LANE=dev   # tray bat
# bench lane (hand-launched, 11439)
set HELIX_OTEL_SERVICE_NAME=helix-bench & set HELIX_LANE=bench
... -m uvicorn helix_context._asgi:app --host 127.0.0.1 --port 11439
```

## Validation (evidence before "done")
1. Two daemons up on 11437/11439 with the two env names, both `HELIX_OTEL_ENABLED=1` → collector `:4317`.
2. Prometheus: `count by (service_name) (helix_pipeline_stage_seconds_count)` returns **`helix-dev` AND `helix-bench`** as separate series (not one merged `helix-context`).
3. `service_instance_id` shows `127.0.0.1:11437` vs `:11439`.
4. `curl :11437/health` and `:11439/health` both 200 (unchanged).
5. Default (no env) run still reports `service.name=helix-context` (back-compat).

## Out of scope (separate items)
- `--port` flag + drop the blanket "kill any uvicorn on 11437" in `benchmarks/start_helix_for_enterprise_rag.py` (Raude's 5-LOC nicety — pairs with this for ergonomic dual-lane).
- Tray dashboard polling both ports (the dual-genome collector generalization, report §4).
