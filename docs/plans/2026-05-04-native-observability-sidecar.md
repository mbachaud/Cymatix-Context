# Native Observability Sidecar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Docker Compose observability stack with native binaries managed by the helix tray launcher.

**Architecture:** Five native binaries (otelcol-contrib, prometheus, tempo, loki, grafana) launched as subprocesses by the existing tray launcher; install script downloads + verifies SHA256 per platform; configs templated at install time from existing `deploy/otel/` sources; runtime state in `%LOCALAPPDATA%/helix-context/observability/`; lifecycle bound to tray via Windows Job Object.

**Tech Stack:** Python 3.11+, pywin32 (Job Object), platformdirs (state-dir resolution), pystray (tray menu + balloon notifications, already a dep), PowerShell + Bash install scripts.

---

## Spec

`docs/specs/2026-05-04-native-observability-sidecar-design.md` (locked at commit `3057096`).

## File structure

| File | Action | Responsibility |
| --- | --- | --- |
| `deploy/otel/loki-config.yaml` | Create | Explicit Loki config (replaces image-default). Both Docker and native runtimes read it (templated for native). |
| `deploy/otel/docker-compose.yml` | Modify | Mount `./loki-config.yaml` into the loki service. |
| `deploy/otel/README.md` | Create | "Alternate Docker path" doc per spec §8. |
| `tools/native-otel/.versions` | Create | TOML version pins + per-platform SHA256 entries. |
| `tools/native-otel/README.md` | Create | Layout, install script invocation, version-update procedure. |
| `tools/native-otel/configs/.gitkeep` | Create | Pin the dir so renders land in a tracked location. |
| `scripts/install-native-observability.ps1` | Create | Windows bootstrap. |
| `scripts/install-native-observability.sh` | Create | Linux/macOS bootstrap (capable, untested). |
| `helix_context/launcher/observability_paths.py` | Create | `state_dir()` + `configs_dir()` + `binary_path()` helpers built on `platformdirs`. |
| `helix_context/launcher/observability_render.py` | Create | Source-yaml → rendered-config substitution module. |
| `helix_context/launcher/observability_health.py` | Create | Port-bind poll + HTTP health probe. |
| `helix_context/launcher/observability_supervisor.py` | Create | 5-child supervisor + Job Object setup + spawn-order sequencer. |
| `helix_context/launcher/tray.py` | Modify | Add Observability submenu (Status, Restart [service], Open log directory, balloon-notification first-launch hook). |
| `helix_context/launcher/app.py` | Modify | Read `HELIX_OBSERVABILITY` env, build supervisor, hand off to tray. |
| `Start-helix-tray.bat` | Modify | Add commented-out `set "HELIX_OBSERVABILITY=0"` opt-out hint. |
| `pyproject.toml` | Modify | Add `platformdirs` to `[launcher]`; add `pywin32` (Windows-only marker) to `[launcher-tray]`. |
| `.gitignore` | Modify | Add `tools/native-otel/{collector,prometheus,tempo,loki,grafana}/`. |
| `README.md` | Modify | Native observability section + Docker advanced footnote (per spec §8). |
| `tests/test_observability_render.py` | Create | Render unit tests (per spec §9). |
| `tests/test_observability_paths.py` | Create | platformdirs wrapper unit tests. |
| `tests/test_observability_health.py` | Create | Port + HTTP poll unit tests. |
| `tests/test_observability_supervisor.py` | Create | Spawn/order/cleanup unit tests + Job Object setup test (Windows-conditional). |
| `tests/test_install_observability.py` | Create | Bootstrap unit tests — corrupt-download, idempotency. |

**Files explicitly NOT touched:**
- `helix_context/launcher/supervisor.py` — `HelixSupervisor` is unrelated; ObservabilitySupervisor is a parallel sibling.
- `helix_context/telemetry.py` — exporter wiring is unchanged; this plan only swaps the receiver runtime.
- `deploy/otel/grafana/dashboards/*` — dashboard JSON is runtime-agnostic per spec §3.

---

## Task 1: Add explicit `loki-config.yaml`; mount in docker-compose

**Files:**
- Create: `deploy/otel/loki-config.yaml`
- Modify: `deploy/otel/docker-compose.yml` (loki service block, lines 44-52)

Spec §11.2: both runtimes read one source. Loki's image-default and our explicit config must be byte-equivalent on the Docker side so behavior is preserved.

- [ ] **Step 1: Add a docker-side regression test**

Create `tests/test_loki_config_docker_compat.py`:

```python
"""Regression: deploy/otel/loki-config.yaml is mounted by docker-compose
and uses the same path Loki's image-default expects, so the Docker
runtime's behavior is unchanged when we add an explicit config.

Spec §11.2 — locked: Loki config is shared by Docker and native runtimes.
"""

from pathlib import Path

import pytest

# pyyaml ships transitively (sentence-transformers, etc.) but isn't a
# declared dep; skip cleanly in barebones envs rather than ImportError.
yaml = pytest.importorskip("yaml")


REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "deploy" / "otel" / "docker-compose.yml"
LOKI_CFG = REPO / "deploy" / "otel" / "loki-config.yaml"


def test_loki_config_file_exists():
    assert LOKI_CFG.exists(), (
        "deploy/otel/loki-config.yaml must exist — both runtimes read it."
    )


def test_docker_compose_mounts_loki_config():
    text = COMPOSE.read_text(encoding="utf-8")
    spec = yaml.safe_load(text)
    loki = spec["services"]["loki"]
    volumes = loki.get("volumes", [])
    mount_target = "./loki-config.yaml:/etc/loki/local-config.yaml:ro"
    assert mount_target in volumes, (
        f"docker-compose loki service must mount {mount_target}; "
        f"got volumes={volumes}"
    )


def test_docker_compose_loki_command_points_at_mounted_config():
    spec = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    cmd = spec["services"]["loki"].get("command")
    assert cmd == ["-config.file=/etc/loki/local-config.yaml"], (
        f"docker-compose loki command must point at the mounted config; got {cmd!r}"
    )


def test_loki_config_parses_as_yaml_and_listens_on_3100():
    spec = yaml.safe_load(LOKI_CFG.read_text(encoding="utf-8"))
    # Loki's HTTP listen port is 3100 — must match docker-compose port mapping.
    assert spec["server"]["http_listen_port"] == 3100
```

- [ ] **Step 2: Run, verify fail**

```
py -3 -m pytest tests/test_loki_config_docker_compat.py -v
```

Expected: 4 FAIL — `loki-config.yaml` does not exist; docker-compose lacks the mount + command.

- [ ] **Step 3: Create `deploy/otel/loki-config.yaml`**

The contents below mirror Loki 3.2.0's image-default `local-config.yaml` (filesystem store, in-memory ring) so the Docker runtime stays bit-identical:

```yaml
auth_enabled: false

server:
  http_listen_port: 3100
  grpc_listen_port: 9095

common:
  instance_addr: 127.0.0.1
  path_prefix: /loki
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

ruler:
  alertmanager_url: http://localhost:9093

# Native runtime substitutes /loki -> %LOCALAPPDATA%/helix-context/observability/loki
# at install time. See helix_context/launcher/observability_render.py.
```

- [ ] **Step 4: Modify `deploy/otel/docker-compose.yml`**

In the `loki` service block (around lines 44-52), add the volume mount alongside the existing data volume so the file is read at startup:

```yaml
  loki:
    image: grafana/loki:3.2.0
    container_name: helix-loki
    command: ["-config.file=/etc/loki/local-config.yaml"]
    volumes:
      - ./loki-config.yaml:/etc/loki/local-config.yaml:ro
      - loki-data:/loki
    ports:
      - "3100:3100"
    restart: unless-stopped
```

(The `command:` line was already present — only the `./loki-config.yaml:...` mount is new.)

- [ ] **Step 5: Run, verify pass**

```
py -3 -m pytest tests/test_loki_config_docker_compat.py -v
```

Expected: 4 PASS.

- [ ] **Step 6: Manual sanity check (only if Docker is on the dev machine)**

```
cd deploy/otel && docker-compose up -d loki
docker-compose logs --tail=20 loki
docker-compose down
```

Expected: Loki starts cleanly with no "config" complaints. Skip if Docker isn't available locally.

- [ ] **Step 7: Commit**

```bash
git add deploy/otel/loki-config.yaml deploy/otel/docker-compose.yml tests/test_loki_config_docker_compat.py
git commit -m "feat(deploy): add explicit loki-config.yaml + mount in docker-compose

Both runtimes (Docker compose + the upcoming native sidecar) now read one
source of truth for Loki config. Behavior on the Docker side is preserved
because the explicit config matches Loki 3.2.0's image-default local-config
(filesystem store, inmemory ring, 3100 HTTP listen).

Lands first per spec §11.2 — preserves the zero-functional-change claim
for existing Docker users while making the file available for the native
render pipeline.

See docs/specs/2026-05-04-native-observability-sidecar-design.md §11.2."
```

---

## Task 2: `tools/native-otel/.versions` schema + initial values

**Files:**
- Create: `tools/native-otel/.versions`
- Create: `tools/native-otel/README.md`
- Create: `tools/native-otel/configs/.gitkeep`
- Modify: `.gitignore`

The `.versions` file is plain TOML (not Python — it's read by both PowerShell and bash bootstrap scripts). One `[<service>]` section per binary, with `version`, `url_<platform>`, `sha256_<platform>` keys. Windows hashes are committed live. Linux/macOS hashes are placeholder `TODO_<platform>` strings — bootstrap script refuses to install on platforms whose row is still a placeholder.

- [ ] **Step 1: Add the schema regression test**

Create `tests/test_versions_file.py`:

```python
"""Regression: tools/native-otel/.versions is parseable, contains all 5
services, and Windows hashes are non-placeholder.

Per spec §6 + §11.6: hashes ship live for Windows on day 1; Linux/macOS
rows mark TODO so the bootstrap can fail loud rather than silently
installing an unverified binary.
"""

from pathlib import Path

import pytest

try:
    import tomllib  # py3.11+
except ImportError:
    import tomli as tomllib  # type: ignore


REPO = Path(__file__).resolve().parent.parent
VERSIONS = REPO / "tools" / "native-otel" / ".versions"
SERVICES = ["otelcol-contrib", "prometheus", "tempo", "loki", "grafana"]
PLATFORMS = ["windows_amd64", "linux_amd64", "darwin_arm64", "darwin_amd64"]


def test_versions_file_exists():
    assert VERSIONS.exists()


def test_versions_file_parses_as_toml():
    with VERSIONS.open("rb") as f:
        spec = tomllib.load(f)
    assert isinstance(spec, dict)


def test_versions_has_all_five_services():
    with VERSIONS.open("rb") as f:
        spec = tomllib.load(f)
    for svc in SERVICES:
        assert svc in spec, f"missing service block: {svc}"
        assert "version" in spec[svc], f"{svc}: missing version"


@pytest.mark.parametrize("svc", SERVICES)
def test_versions_windows_hash_is_real(svc):
    with VERSIONS.open("rb") as f:
        spec = tomllib.load(f)
    h = spec[svc].get("sha256_windows_amd64")
    assert h, f"{svc}: windows_amd64 hash missing"
    # Real SHA256 is 64 hex chars; placeholders look like TODO_*.
    assert len(h) == 64, f"{svc}: windows_amd64 hash is placeholder ({h!r})"
    int(h, 16)  # raises ValueError if not hex


@pytest.mark.parametrize("svc", SERVICES)
@pytest.mark.parametrize("plat", ["linux_amd64", "darwin_arm64", "darwin_amd64"])
def test_versions_other_platforms_are_present_even_if_todo(svc, plat):
    """Non-Windows rows may be TODO placeholders, but the keys must exist
    so the bootstrap script can detect-and-refuse rather than KeyError."""
    with VERSIONS.open("rb") as f:
        spec = tomllib.load(f)
    assert f"sha256_{plat}" in spec[svc]
    assert f"url_{plat}" in spec[svc]
```

- [ ] **Step 2: Run, verify fail**

```
py -3 -m pytest tests/test_versions_file.py -v
```

Expected: all FAIL — file does not exist.

- [ ] **Step 3: Create `tools/native-otel/.versions`**

```toml
# Pinned versions + SHA256 per platform for the native observability
# sidecar. Read by:
#   - scripts/install-native-observability.ps1
#   - scripts/install-native-observability.sh
#   - tests/test_versions_file.py (schema regression)
#
# Bumping a version: update version + url_<platform> + sha256_<platform>
# for every platform you care about. Re-run the install script — it
# will detect the hash change and re-download.
#
# Windows hashes are LIVE.
# Linux/macOS hashes are TODO placeholders; the bootstrap script
# refuses to install on those platforms until they're filled in
# (locked decision, spec §3 non-goal: cross-platform unvalidated).

[otelcol-contrib]
version = "0.105.0"
url_windows_amd64 = "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v0.105.0/otelcol-contrib_0.105.0_windows_amd64.tar.gz"
sha256_windows_amd64 = "PLAN_NOTE_FILL_FROM_RELEASE_PAGE_AT_IMPL_TIME_64HEXCHARS_____"
url_linux_amd64 = "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v0.105.0/otelcol-contrib_0.105.0_linux_amd64.tar.gz"
sha256_linux_amd64 = "TODO_linux_amd64"
url_darwin_arm64 = "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v0.105.0/otelcol-contrib_0.105.0_darwin_arm64.tar.gz"
sha256_darwin_arm64 = "TODO_darwin_arm64"
url_darwin_amd64 = "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v0.105.0/otelcol-contrib_0.105.0_darwin_amd64.tar.gz"
sha256_darwin_amd64 = "TODO_darwin_amd64"

[prometheus]
version = "2.54.1"
url_windows_amd64 = "https://github.com/prometheus/prometheus/releases/download/v2.54.1/prometheus-2.54.1.windows-amd64.zip"
sha256_windows_amd64 = "PLAN_NOTE_FILL_FROM_RELEASE_PAGE_AT_IMPL_TIME_64HEXCHARS_____"
url_linux_amd64 = "https://github.com/prometheus/prometheus/releases/download/v2.54.1/prometheus-2.54.1.linux-amd64.tar.gz"
sha256_linux_amd64 = "TODO_linux_amd64"
url_darwin_arm64 = "https://github.com/prometheus/prometheus/releases/download/v2.54.1/prometheus-2.54.1.darwin-arm64.tar.gz"
sha256_darwin_arm64 = "TODO_darwin_arm64"
url_darwin_amd64 = "https://github.com/prometheus/prometheus/releases/download/v2.54.1/prometheus-2.54.1.darwin-amd64.tar.gz"
sha256_darwin_amd64 = "TODO_darwin_amd64"

[tempo]
version = "2.6.0"
url_windows_amd64 = "https://github.com/grafana/tempo/releases/download/v2.6.0/tempo_2.6.0_windows_amd64.tar.gz"
sha256_windows_amd64 = "PLAN_NOTE_FILL_FROM_RELEASE_PAGE_AT_IMPL_TIME_64HEXCHARS_____"
url_linux_amd64 = "https://github.com/grafana/tempo/releases/download/v2.6.0/tempo_2.6.0_linux_amd64.tar.gz"
sha256_linux_amd64 = "TODO_linux_amd64"
url_darwin_arm64 = "https://github.com/grafana/tempo/releases/download/v2.6.0/tempo_2.6.0_darwin_arm64.tar.gz"
sha256_darwin_arm64 = "TODO_darwin_arm64"
url_darwin_amd64 = "https://github.com/grafana/tempo/releases/download/v2.6.0/tempo_2.6.0_darwin_amd64.tar.gz"
sha256_darwin_amd64 = "TODO_darwin_amd64"

[loki]
version = "3.2.0"
url_windows_amd64 = "https://github.com/grafana/loki/releases/download/v3.2.0/loki-windows-amd64.exe.zip"
sha256_windows_amd64 = "PLAN_NOTE_FILL_FROM_RELEASE_PAGE_AT_IMPL_TIME_64HEXCHARS_____"
url_linux_amd64 = "https://github.com/grafana/loki/releases/download/v3.2.0/loki-linux-amd64.zip"
sha256_linux_amd64 = "TODO_linux_amd64"
url_darwin_arm64 = "https://github.com/grafana/loki/releases/download/v3.2.0/loki-darwin-arm64.zip"
sha256_darwin_arm64 = "TODO_darwin_arm64"
url_darwin_amd64 = "https://github.com/grafana/loki/releases/download/v3.2.0/loki-darwin-amd64.zip"
sha256_darwin_amd64 = "TODO_darwin_amd64"

[grafana]
version = "11.3.0"
url_windows_amd64 = "https://dl.grafana.com/oss/release/grafana-11.3.0.windows-amd64.zip"
sha256_windows_amd64 = "PLAN_NOTE_FILL_FROM_RELEASE_PAGE_AT_IMPL_TIME_64HEXCHARS_____"
url_linux_amd64 = "https://dl.grafana.com/oss/release/grafana-11.3.0.linux-amd64.tar.gz"
sha256_linux_amd64 = "TODO_linux_amd64"
url_darwin_arm64 = "https://dl.grafana.com/oss/release/grafana-11.3.0.darwin-arm64.tar.gz"
sha256_darwin_arm64 = "TODO_darwin_arm64"
url_darwin_amd64 = "https://dl.grafana.com/oss/release/grafana-11.3.0.darwin-amd64.tar.gz"
sha256_darwin_amd64 = "TODO_darwin_amd64"
```

> **Plan note: defer to user.** The five `PLAN_NOTE_FILL_FROM_RELEASE_PAGE_AT_IMPL_TIME_64HEXCHARS_____` strings are placeholders for the real SHA256 values that will be fetched from each project's release page during implementation (manually, by the implementer; the spec does not enumerate them). The schema test enforces that `sha256_windows_amd64` is a 64-char hex string, so real hashes must replace these before the test passes. **Implementer must obtain the five Windows SHA256s from the official release pages and substitute them before running step 4.**

- [ ] **Step 4: Create `tools/native-otel/configs/.gitkeep`**

Empty file — pins the directory in git so the render step has a tracked output location:

```
# This dir holds rendered runtime configs produced by
# scripts/install-native-observability.{ps1,sh}.
# The rendered files themselves are gitignored individually so
# accidentally-committed local renders don't leak. Keep this file.
```

- [ ] **Step 5: Append to `.gitignore`**

```
# Native observability sidecar — binaries downloaded by the install script.
tools/native-otel/collector/
tools/native-otel/prometheus/
tools/native-otel/tempo/
tools/native-otel/loki/
tools/native-otel/grafana/
# Rendered configs are local to each install (paths embedded reflect the
# user's home/AppData). Don't commit them.
tools/native-otel/configs/*.yaml
tools/native-otel/configs/*.yml
!tools/native-otel/configs/.gitkeep
```

- [ ] **Step 6: Create `tools/native-otel/README.md`**

```markdown
# Native observability binaries

Layout:

```
tools/native-otel/
  .versions              pinned versions + SHA256 per platform (TOML)
  configs/               rendered runtime configs (gitignored except .gitkeep)
  collector/             otelcol-contrib binary  (gitignored)
  prometheus/            prometheus + promtool   (gitignored)
  tempo/                 tempo binary            (gitignored)
  loki/                  loki binary             (gitignored)
  grafana/               grafana-server binary   (gitignored)
```

## Install

Windows (PowerShell):

```powershell
scripts/install-native-observability.ps1
```

Linux/macOS:

```bash
bash scripts/install-native-observability.sh
```

The install script reads `.versions`, downloads each binary from the
official release page, verifies SHA256 against the platform-specific
hash, extracts to the per-service folder, and renders runtime configs
(host + path substitution) into `configs/`.

Idempotent — re-runs skip components whose binary hash matches the pinned
value. Bumping a version in `.versions` and re-running upgrades only that
component.

## State

Per-user observability state (TSDB, traces, logs) lives outside the repo
under `platformdirs.user_data_dir("helix-context") / "observability"`:

- Windows: `%LOCALAPPDATA%\helix-context\observability\`
- Linux:   `~/.local/share/helix-context/observability/`
- macOS:   `~/Library/Application Support/helix-context/observability/`

## Uninstall

`Remove-Item -Recurse tools/native-otel/{collector,prometheus,tempo,loki,grafana}`
(or the equivalent `rm -rf` on POSIX). The `configs/` dir clears itself
on next install. Per-user state at `platformdirs.user_data_dir(...)/observability`
is left in place — delete manually if you want a fully clean slate.

See `docs/specs/2026-05-04-native-observability-sidecar-design.md`.
```

- [ ] **Step 7: Run, verify pass**

```
py -3 -m pytest tests/test_versions_file.py -v
```

Expected: 23 PASS (1 file-exists + 1 parses + 5 services-block + 5 windows-hash-real + 12 platforms-present).

If `test_versions_windows_hash_is_real` fails because the real hashes weren't substituted in step 3, fill them in now from the upstream release pages.

- [ ] **Step 8: Commit**

```bash
git add tools/native-otel/.versions tools/native-otel/README.md tools/native-otel/configs/.gitkeep .gitignore tests/test_versions_file.py
git commit -m "feat(native-otel): version pin file + dir layout + gitignore

Adds tools/native-otel/.versions (TOML) with version + per-platform
URL + SHA256 entries for the five binaries. Windows hashes are live;
Linux/macOS rows are TODO placeholders so the bootstrap can refuse
loudly until cross-platform validation work lands (spec §3 non-goal).

Schema regression test pins the structure so future version bumps
can't silently drop a row.

See docs/specs/2026-05-04-native-observability-sidecar-design.md §6."
```

---

## Task 3: Bootstrap scripts (PowerShell + bash)

**Files:**
- Create: `scripts/install-native-observability.ps1`
- Create: `scripts/install-native-observability.sh`
- Create: `tests/test_install_observability.py` (PowerShell-side end-to-end is hard to unit test; we exercise the *render* via the supervisor render module in Task 5. Here we cover the corrupt-download path with a Python harness that drives the script's verify_hash function — both shell scripts delegate hash verification to `python -c "import hashlib; ..."` so the test covers both runtimes.)

@superpowers:test-driven-development

- [ ] **Step 1: Test plan — what we cover**

The shell script's behaviors that need automated coverage:

1. SHA256 mismatch on download → fail loud, leave previous binary untouched
2. Idempotency — re-run with current binary already present → skip download
3. Render step runs after all binaries land → presence of every rendered config

Render correctness lives in Task 4's render module (Python — easier to unit-test than parsing PowerShell output).

For (1) and (2) we keep the install script thin: it shells out to `python -m helix_context.launcher._install_helpers` for `verify_hash`, `download_with_progress`, `extract_archive`. The Python entry-point is what we test.

- [ ] **Step 2: Failing test for verify_hash**

Create `tests/test_install_observability.py`:

```python
"""Tests for the bootstrap script's Python helper entry-points.

Both install-native-observability.ps1 and .sh delegate hash verification
and archive extraction to a Python helper module so the platform-specific
shell wrapper stays small and the testable surface is one place.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


def _real_sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def test_verify_hash_accepts_match(tmp_path):
    from helix_context.launcher._install_helpers import verify_hash
    f = tmp_path / "binary.bin"
    f.write_bytes(b"hello world")
    expected = _real_sha256(f)
    # Returns None on success (no raise).
    verify_hash(f, expected)


def test_verify_hash_raises_on_mismatch(tmp_path):
    from helix_context.launcher._install_helpers import (
        HashMismatch,
        verify_hash,
    )
    f = tmp_path / "binary.bin"
    f.write_bytes(b"corrupt")
    with pytest.raises(HashMismatch):
        verify_hash(f, "0" * 64)


def test_verify_hash_rejects_placeholder(tmp_path):
    """A `TODO_<platform>` placeholder must NOT be accepted as a valid hash.

    Bootstrap path: refuses to install on platforms where the row is still
    placeholder, so a Linux user running this on day 1 (before §11.6 work
    lands) gets a clean error instead of an unverified binary.
    """
    from helix_context.launcher._install_helpers import (
        HashPlaceholder,
        verify_hash,
    )
    f = tmp_path / "binary.bin"
    f.write_bytes(b"hello")
    with pytest.raises(HashPlaceholder):
        verify_hash(f, "TODO_linux_amd64")


def test_should_skip_when_existing_binary_matches(tmp_path):
    """Idempotency: present-and-correct binary returns True from
    should_skip; download is not re-run."""
    from helix_context.launcher._install_helpers import should_skip
    f = tmp_path / "binary.bin"
    f.write_bytes(b"present and correct")
    expected = _real_sha256(f)
    assert should_skip(f, expected) is True


def test_should_skip_false_when_binary_absent(tmp_path):
    from helix_context.launcher._install_helpers import should_skip
    assert should_skip(tmp_path / "nope.bin", "0" * 64) is False


def test_should_skip_false_when_hash_drifts(tmp_path):
    """Version bump → hash drift → re-download triggers."""
    from helix_context.launcher._install_helpers import should_skip
    f = tmp_path / "binary.bin"
    f.write_bytes(b"old version")
    assert should_skip(f, "0" * 64) is False
```

- [ ] **Step 3: Run, verify fail**

```
py -3 -m pytest tests/test_install_observability.py -v
```

Expected: 6 FAIL — module does not exist.

- [ ] **Step 4: Create `helix_context/launcher/_install_helpers.py`**

```python
"""Helpers for scripts/install-native-observability.{ps1,sh}.

Both shell scripts delegate the cross-platform-tricky bits (hash verify,
download with timeout, archive extraction) to this module so the test
surface stays in Python and the shells stay thin orchestrators.

Usage from the shell:

    python -m helix_context.launcher._install_helpers verify-hash <path> <hex>

Or invoked directly from Python (for tests + the launcher's first-launch
prompt path).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("helix.launcher.install")

_HASH_CHUNK = 1 << 20  # 1 MiB


class HashMismatch(Exception):
    """Downloaded artifact's SHA256 doesn't match the pinned value."""


class HashPlaceholder(Exception):
    """The pinned hash is a `TODO_<platform>` placeholder — install refused."""


def _is_placeholder(hex_hash: str) -> bool:
    return hex_hash.startswith("TODO_") or "PLAN_NOTE_FILL" in hex_hash


def verify_hash(path: Path, expected_hex: str) -> None:
    """Raise HashMismatch / HashPlaceholder if path's SHA256 doesn't match.

    Returns None on success.
    """
    if _is_placeholder(expected_hex):
        raise HashPlaceholder(
            f"Pinned hash for this platform is a placeholder ({expected_hex!r}); "
            "fill in tools/native-otel/.versions before installing."
        )
    if len(expected_hex) != 64:
        raise HashMismatch(
            f"Pinned hash for {path.name} is malformed (got {expected_hex!r}, "
            "expected 64 hex chars)."
        )
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    actual = h.hexdigest()
    if actual.lower() != expected_hex.lower():
        raise HashMismatch(
            f"SHA256 mismatch for {path.name}: got {actual}, expected {expected_hex}"
        )


def should_skip(path: Path, expected_hex: str) -> bool:
    """Return True iff path exists AND its hash matches expected_hex.

    Used by the bootstrap to skip the download for already-installed binaries.
    Never raises — placeholder + mismatched hashes both return False.
    """
    if not path.exists() or not path.is_file():
        return False
    try:
        verify_hash(path, expected_hex)
    except (HashMismatch, HashPlaceholder, OSError):
        return False
    return True


def download_to(url: str, dest: Path, *, timeout: float = 30.0) -> None:
    """Stream a URL to dest atomically (download to .part, then rename).

    Per global preference: every urlopen call has an explicit timeout.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp, tmp.open("wb") as out:
            while True:
                chunk = resp.read(_HASH_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
        tmp.replace(dest)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _cli(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog="install-helpers")
    sub = p.add_subparsers(dest="cmd", required=True)
    s_v = sub.add_parser("verify-hash")
    s_v.add_argument("path")
    s_v.add_argument("expected_hex")
    s_d = sub.add_parser("download")
    s_d.add_argument("url")
    s_d.add_argument("dest")
    s_d.add_argument("--timeout", type=float, default=30.0)
    args = p.parse_args(argv)
    try:
        if args.cmd == "verify-hash":
            verify_hash(Path(args.path), args.expected_hex)
            print("OK")
        elif args.cmd == "download":
            download_to(args.url, Path(args.dest), timeout=args.timeout)
            print("OK")
        return 0
    except HashPlaceholder as exc:
        print(f"PLACEHOLDER: {exc}", file=sys.stderr)
        return 2
    except HashMismatch as exc:
        print(f"MISMATCH: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(_cli())
```

- [ ] **Step 5: Run, verify pass**

```
py -3 -m pytest tests/test_install_observability.py -v
```

Expected: 6 PASS.

- [ ] **Step 6: Create `scripts/install-native-observability.ps1`**

```powershell
<#
.SYNOPSIS
  Install native observability binaries for the helix tray launcher.

.DESCRIPTION
  Reads tools/native-otel/.versions, downloads each component from its
  pinned release URL, verifies SHA256, extracts to per-service folders
  under tools/native-otel/, then renders runtime configs.

  Idempotent: re-runs skip components whose binary hash already matches.

.PARAMETER WhatIf
  Show what would be downloaded without actually fetching.

.NOTES
  Spec: docs/specs/2026-05-04-native-observability-sidecar-design.md §6
#>

[CmdletBinding(SupportsShouldProcess=$true)]
param(
  [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# ── Locate venv python ─────────────────────────────────────────────
$python = "python"
if (Test-Path "$RepoRoot\.venv\Scripts\python.exe") {
    $python = "$RepoRoot\.venv\Scripts\python.exe"
}

$versionsFile = Join-Path $RepoRoot "tools\native-otel\.versions"
if (-not (Test-Path $versionsFile)) {
    Write-Error "$versionsFile not found"
    exit 1
}

# ── Parse .versions via Python (TOML — Windows PowerShell has no native parser) ──
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
    Write-Error "Failed to parse $versionsFile"
    exit 1
}
$spec = $specJson | ConvertFrom-Json

$platform = "windows_amd64"

# Maps service-name → (target-binary subpath inside its folder)
$binaries = @{
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
        Write-Error "[$svc] Hash for $platform is a placeholder ($expected). Fill in .versions before installing."
        exit 2
    }

    # Skip if already installed and matches.
    & $python -m helix_context.launcher._install_helpers verify-hash $absPath $expected 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[$svc] up-to-date (sha256 ok) — skipping"
        continue
    }

    if (-not $PSCmdlet.ShouldProcess($svc, "download $url")) {
        continue
    }

    Write-Host "[$svc] downloading $url"
    $tmpArchive = Join-Path $env:TEMP "helix-native-otel-$svc.tmp"
    & $python -m helix_context.launcher._install_helpers download $url $tmpArchive --timeout 120
    if ($LASTEXITCODE -ne 0) { Write-Error "[$svc] download failed"; exit 3 }

    # We don't know the archive hash — projects publish the archive sha,
    # but the binary inside is what we run. Verify the archive against
    # the pinned hash (which IS the archive hash from each project's
    # release page).
    & $python -m helix_context.launcher._install_helpers verify-hash $tmpArchive $expected
    if ($LASTEXITCODE -ne 0) { Write-Error "[$svc] hash check failed"; exit 4 }

    # Extract — .zip vs .tar.gz handled inline.
    New-Item -ItemType Directory -Force -Path $svcDir | Out-Null
    $stagingDir = Join-Path $env:TEMP "helix-native-otel-$svc-extract"
    if (Test-Path $stagingDir) { Remove-Item -Recurse -Force $stagingDir }
    New-Item -ItemType Directory -Force -Path $stagingDir | Out-Null

    if ($url.EndsWith(".zip")) {
        Expand-Archive -Path $tmpArchive -DestinationPath $stagingDir -Force
    } else {
        # tar is in Windows since 17063; falls back to python tarfile if not present.
        & tar -xf $tmpArchive -C $stagingDir
        if ($LASTEXITCODE -ne 0) {
            & $python -c "import tarfile; tarfile.open(r'$tmpArchive').extractall(r'$stagingDir')"
        }
    }

    # Find the target binary anywhere inside the staging tree and copy it.
    $exeName = Split-Path -Leaf $absPath
    $found = Get-ChildItem -Path $stagingDir -Recurse -Filter $exeName -File | Select-Object -First 1
    if ($null -eq $found) {
        Write-Error "[$svc] $exeName not found inside $tmpArchive"
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
    Write-Host "[$svc] installed"
}

# ── Render runtime configs ─────────────────────────────────────────
Write-Host "Rendering runtime configs to tools/native-otel/configs/ ..."
& $python -m helix_context.launcher.observability_render render-all
if ($LASTEXITCODE -ne 0) { Write-Error "render failed"; exit 6 }

Write-Host "Native observability install complete."
```

- [ ] **Step 7: Create `scripts/install-native-observability.sh`**

```bash
#!/usr/bin/env bash
# Install native observability binaries on Linux/macOS.
# Capable, untested per spec §3 non-goal — refuses to run on platforms
# whose .versions row is a TODO placeholder.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python"
fi

VERSIONS="$REPO_ROOT/tools/native-otel/.versions"
[ -f "$VERSIONS" ] || { echo "Missing $VERSIONS" >&2; exit 1; }

case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)   PLATFORM="linux_amd64" ;;
    Darwin-arm64)   PLATFORM="darwin_arm64" ;;
    Darwin-x86_64)  PLATFORM="darwin_amd64" ;;
    *) echo "Unsupported platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

# ── Read .versions via Python (one source of truth for the parser) ──
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
            echo "[$svc] sha256 for $PLATFORM is placeholder ($expected). Fill .versions first." >&2
            exit 2
            ;;
    esac

    if $PYTHON -m helix_context.launcher._install_helpers verify-hash "$abspath" "$expected" 2>/dev/null; then
        echo "[$svc] up-to-date — skipping"
        continue
    fi

    echo "[$svc] downloading $url"
    tmp="$(mktemp -t helix-native-otel.XXXXXX)"
    $PYTHON -m helix_context.launcher._install_helpers download "$url" "$tmp" --timeout 120
    $PYTHON -m helix_context.launcher._install_helpers verify-hash "$tmp" "$expected"

    mkdir -p "$svcdir"
    staging="$(mktemp -d -t helix-native-otel-extract.XXXXXX)"
    case "$url" in
        *.tar.gz|*.tgz) tar -xzf "$tmp" -C "$staging" ;;
        *.zip)          unzip -q "$tmp" -d "$staging" ;;
        *) echo "Unknown archive format: $url" >&2; exit 5 ;;
    esac

    exename="$(basename "$abspath")"
    found="$(find "$staging" -name "$exename" -type f -print -quit || true)"
    [ -n "$found" ] || { echo "[$svc] $exename not found inside archive" >&2; exit 5; }
    cp "$found" "$abspath"
    chmod +x "$abspath"

    if [ "$svc" = "grafana" ]; then
        graf_root="$(find "$staging" -maxdepth 1 -type d -name 'grafana-*' -print -quit || true)"
        if [ -n "$graf_root" ]; then
            cp -R "$graf_root"/* "$REPO_ROOT/tools/native-otel/grafana/"
        fi
    fi

    rm -f "$tmp"
    rm -rf "$staging"
    echo "[$svc] installed"
done < <(read_binaries)

echo "Rendering runtime configs ..."
$PYTHON -m helix_context.launcher.observability_render render-all
echo "Native observability install complete."
```

`chmod +x scripts/install-native-observability.sh` once written.

- [ ] **Step 8: Run unit tests**

```
py -3 -m pytest tests/test_install_observability.py -v
```

Expected: still 6 PASS.

- [ ] **Step 9: Commit**

```bash
git add scripts/install-native-observability.ps1 scripts/install-native-observability.sh helix_context/launcher/_install_helpers.py tests/test_install_observability.py
git commit -m "feat(launcher): bootstrap script for native observability binaries

Adds:
- helix_context/launcher/_install_helpers.py — Python helpers for
  download + SHA256 verify (with explicit timeout per global pref).
  Both shell scripts delegate the cross-platform-tricky bits to this
  module so the testable surface is one place.
- scripts/install-native-observability.ps1 — Windows bootstrap.
- scripts/install-native-observability.sh — Linux/macOS bootstrap;
  capable, untested per spec §3 non-goal — refuses placeholder hashes.

Mismatched downloads + placeholder hashes raise distinct exceptions so
the supervisor can present a useful error to the tray menu.

See docs/specs/2026-05-04-native-observability-sidecar-design.md §6."
```

---

## Task 4: State-dir resolution helper (`observability_paths.py`)

**Files:**
- Create: `helix_context/launcher/observability_paths.py`
- Create: `tests/test_observability_paths.py`

Pure utility — used by Task 5's render module + Task 7's supervisor + Task 8's tray "Open log directory" menu item.

@superpowers:test-driven-development

- [ ] **Step 1: Failing tests**

Create `tests/test_observability_paths.py`:

```python
"""Tests for helix_context.launcher.observability_paths."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


def test_state_dir_returns_absolute_path():
    from helix_context.launcher.observability_paths import state_dir
    p = state_dir()
    assert p.is_absolute()
    # Always ends with .../observability
    assert p.name == "observability"


def test_state_dir_includes_helix_context_segment():
    """Sanity: helix-context segment appears so we don't accidentally
    hit a different app's data dir on a shared host."""
    from helix_context.launcher.observability_paths import state_dir
    p = state_dir()
    assert "helix-context" in str(p) or "helix_context" in str(p)


def test_per_service_state_dir():
    from helix_context.launcher.observability_paths import service_state_dir
    p = service_state_dir("prometheus")
    assert p.name == "prometheus"
    assert p.parent.name == "observability"


def test_logs_dir_is_under_state_dir():
    from helix_context.launcher.observability_paths import (
        logs_dir,
        state_dir,
    )
    assert logs_dir().parent == state_dir() or logs_dir() == state_dir()


def test_configs_dir_is_under_repo_tools_native_otel():
    from helix_context.launcher.observability_paths import configs_dir
    assert configs_dir().parts[-2:] == ("native-otel", "configs")


def test_binary_path_returns_per_service_path():
    from helix_context.launcher.observability_paths import binary_path
    p = binary_path("prometheus")
    assert p.parent.name == "prometheus"
    # Windows-only on this dev box; verify .exe suffix on Windows.
    if sys.platform == "win32":
        assert p.suffix == ".exe"


def test_state_dir_creates_on_request(tmp_path, monkeypatch):
    """state_dir(create=True) makes the dir if missing."""
    from helix_context.launcher import observability_paths as ops
    monkeypatch.setattr(ops, "_user_data_dir", lambda: tmp_path)
    p = ops.state_dir(create=True)
    assert p.exists()
```

- [ ] **Step 2: Run, verify fail**

```
py -3 -m pytest tests/test_observability_paths.py -v
```

Expected: 7 FAIL — module missing.

- [ ] **Step 3: Create `helix_context/launcher/observability_paths.py`**

```python
"""Path resolution for native observability state, configs, binaries.

Single source of truth for "where does X live" — used by:
  - observability_render.py to render container paths into rendered configs
  - observability_supervisor.py to spawn binaries with the right --data-dir
  - the install script to land the binaries
  - the tray "Open log directory" menu

State dir uses platformdirs.user_data_dir, so:
  Windows: %LOCALAPPDATA%\\helix-context\\observability\\<service>\\
  Linux:   ~/.local/share/helix-context/observability/<service>/
  macOS:   ~/Library/Application Support/helix-context/observability/<service>/

Binaries live in the repo at tools/native-otel/<service>/<exe>, NOT in
state-dir, so the install script can hash-verify them and so a uninstall
is `rm -rf tools/native-otel/<service>`.
"""

from __future__ import annotations

import sys
from pathlib import Path


_APP_NAME = "helix-context"
_BINARY_LAYOUT = {
    "collector": ("collector", "otelcol-contrib"),
    "prometheus": ("prometheus", "prometheus"),
    "tempo": ("tempo", "tempo"),
    "loki": ("loki", "loki"),
    "grafana": ("grafana", "bin/grafana-server"),
}


def _user_data_dir() -> Path:
    """Wrap platformdirs.user_data_dir; isolated for monkeypatching in tests."""
    try:
        from platformdirs import user_data_dir
    except ImportError as exc:
        raise RuntimeError(
            "platformdirs is required. "
            "Install with: pip install helix-context[launcher]"
        ) from exc
    # appauthor=False on Windows omits the appauthor folder; without it,
    # platformdirs reuses appname for both, producing a doubled
    # ...\helix-context\helix-context\... segment. Spec §5 documents the
    # single-segment form, so opt out.
    return Path(user_data_dir(_APP_NAME, appauthor=False))


def _repo_root() -> Path:
    # observability_paths.py lives at helix_context/launcher/observability_paths.py
    # so root is two parents up.
    return Path(__file__).resolve().parent.parent.parent


def state_dir(create: bool = False) -> Path:
    """Return the per-user observability state directory."""
    p = _user_data_dir() / "observability"
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def service_state_dir(service: str, create: bool = False) -> Path:
    """Return the per-service state directory under state_dir()."""
    p = state_dir() / service
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir(create: bool = False) -> Path:
    """Return the directory holding rotated <service>.log files.

    Co-located with state_dir() so the user can `Open log directory` from
    the tray and see everything in one place.
    """
    return state_dir(create=create)


def configs_dir(create: bool = False) -> Path:
    """Return tools/native-otel/configs in the repo (rendered configs)."""
    p = _repo_root() / "tools" / "native-otel" / "configs"
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def binary_path(service: str) -> Path:
    """Return the absolute path to the binary for <service>.

    service ∈ {"collector", "prometheus", "tempo", "loki", "grafana"}.
    """
    if service not in _BINARY_LAYOUT:
        raise ValueError(f"unknown service {service!r}")
    folder, exe_rel = _BINARY_LAYOUT[service]
    if sys.platform == "win32":
        # Append .exe to the leaf name only.
        head, _, leaf = exe_rel.rpartition("/")
        exe_rel = (head + "/" if head else "") + leaf + ".exe"
    return _repo_root() / "tools" / "native-otel" / folder / exe_rel
```

- [ ] **Step 4: Run, verify pass**

```
py -3 -m pytest tests/test_observability_paths.py -v
```

Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add helix_context/launcher/observability_paths.py tests/test_observability_paths.py
git commit -m "feat(launcher): observability_paths — state/configs/binary path helpers

Single source of truth for: per-user state dir (via platformdirs),
per-service state subdir, per-service binary path, rendered-configs dir,
log dir. Imported by the render module, supervisor, install script,
and tray 'Open log directory' menu item.

Tests cover absolute-path return, helix-context segment present, .exe
suffix on Windows, and the create=True idempotent dir-make.

See docs/specs/2026-05-04-native-observability-sidecar-design.md §5."
```

---

## Task 5: Config-render module (`observability_render.py`)

**Files:**
- Create: `helix_context/launcher/observability_render.py`
- Create: `tests/fixtures/observability/` with copies of source YAMLs (or fixture-by-load)
- Create: `tests/test_observability_render.py`

Depends on Task 4 (paths helper). Reads the source YAMLs from `deploy/otel/`, substitutes hostnames + container paths, writes rendered output to `tools/native-otel/configs/`. Pure file-I/O + string substitution — no network, no subprocess.

@superpowers:test-driven-development

- [ ] **Step 1: Failing tests**

Create `tests/test_observability_render.py`:

```python
"""Tests for observability_render — feeds each deploy/otel source YAML
through the render step and asserts the substitutions per spec §6.3 + §9.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy" / "otel"


@pytest.fixture
def rendered(tmp_path, monkeypatch):
    """Render every source YAML into tmp_path and return the dir."""
    from helix_context.launcher import observability_paths as ops
    from helix_context.launcher import observability_render as rnd

    # Redirect state_dir AND configs_dir into tmp_path so the test
    # doesn't write to the real repo or AppData.
    monkeypatch.setattr(ops, "_user_data_dir", lambda: tmp_path / "appdata")
    monkeypatch.setattr(
        rnd, "configs_dir",
        lambda create=False: (tmp_path / "configs"),
    )
    (tmp_path / "configs").mkdir(parents=True, exist_ok=True)

    rnd.render_all()
    return tmp_path / "configs"


def test_all_five_configs_rendered(rendered):
    """Each source has a corresponding rendered file."""
    expected = {
        "otel-collector-config.yaml",
        "prometheus.yml",
        "tempo.yaml",
        "loki-config.yaml",
        "datasources.yml",
    }
    actual = {p.name for p in rendered.iterdir() if p.is_file()}
    assert expected.issubset(actual), (
        f"missing rendered files: {expected - actual}"
    )


def test_collector_hostnames_rewritten_to_localhost(rendered):
    text = (rendered / "otel-collector-config.yaml").read_text()
    spec = yaml.safe_load(text)
    # tempo:4317 → localhost:4317
    assert spec["exporters"]["otlp/tempo"]["endpoint"] == "localhost:4317"
    # http://prometheus:9090/api/v1/write → http://localhost:9090/api/v1/write
    assert spec["exporters"]["prometheusremotewrite"]["endpoint"] == \
        "http://localhost:9090/api/v1/write"
    # http://loki:3100/otlp → http://localhost:3100/otlp
    assert spec["exporters"]["otlphttp/loki"]["endpoint"] == \
        "http://localhost:3100/otlp"


def test_prometheus_scrape_target_rewritten(rendered):
    spec = yaml.safe_load((rendered / "prometheus.yml").read_text())
    # otel-collector:8889 → localhost:8889
    targets = spec["scrape_configs"][0]["static_configs"][0]["targets"]
    assert targets == ["localhost:8889"]


def test_tempo_paths_rewritten_to_state_dir(rendered, tmp_path):
    spec = yaml.safe_load((rendered / "tempo.yaml").read_text())
    appdata = tmp_path / "appdata"

    storage = spec["storage"]["trace"]
    # /var/tempo/traces → <state>/tempo/traces
    assert "/var/tempo" not in storage["local"]["path"]
    assert storage["local"]["path"].startswith(str(appdata).replace("\\", "/")) \
        or str(appdata) in storage["local"]["path"]
    assert storage["local"]["path"].endswith("tempo/traces") or \
           storage["local"]["path"].endswith("tempo\\traces")
    # /var/tempo/wal
    assert "/var/tempo" not in storage["wal"]["path"]
    # /var/tempo/generator/wal
    gen = spec["metrics_generator"]["storage"]
    assert "/var/tempo" not in gen["path"]
    # tempo's metrics_generator remote_write hostname
    rw = gen["remote_write"][0]["url"]
    assert rw == "http://localhost:9090/api/v1/write"


def test_loki_paths_rewritten(rendered, tmp_path):
    spec = yaml.safe_load((rendered / "loki-config.yaml").read_text())
    # /loki/chunks → <state>/loki/chunks
    chunks = spec["common"]["storage"]["filesystem"]["chunks_directory"]
    assert chunks.startswith(str(tmp_path / "appdata").replace("\\", "/")) \
        or str(tmp_path / "appdata") in chunks
    assert "/loki/chunks" not in chunks or chunks.endswith("loki/chunks")


def test_grafana_datasources_use_localhost(rendered):
    spec = yaml.safe_load((rendered / "datasources.yml").read_text())
    by_name = {d["name"]: d for d in spec["datasources"]}
    assert by_name["Prometheus"]["url"] == "http://localhost:9090"
    assert by_name["Tempo"]["url"] == "http://localhost:3200"
    assert by_name["Loki"]["url"] == "http://localhost:3100"


def test_no_docker_dns_hostnames_remain_in_any_render(rendered):
    """Cross-cutting check: no rendered file mentions a Docker DNS name.

    Catches accidental drift if a future config-source adds another
    container hostname that the render module didn't know about.
    """
    docker_hosts = ["tempo:", "prometheus:", "loki:", "otel-collector:"]
    for f in rendered.iterdir():
        if not f.is_file() or f.name == ".gitkeep":
            continue
        text = f.read_text()
        for host in docker_hosts:
            assert host not in text, (
                f"{f.name}: still mentions Docker DNS hostname {host!r} "
                f"after render — render module needs an extra rule."
            )


def test_structural_diff_is_only_hostnames_and_paths(rendered):
    """Ingest source + rendered, normalize hostnames+paths to <SUB>,
    assert remaining structural diff is empty. Catches accidental
    structural drift between Docker and native runtimes."""
    pairs = [
        ("otel-collector-config.yaml", "otel-collector-config.yaml"),
        ("prometheus.yml", "prometheus.yml"),
        ("tempo.yaml", "tempo.yaml"),
        ("loki-config.yaml", "loki-config.yaml"),
        ("grafana/provisioning/datasources/datasources.yml", "datasources.yml"),
    ]
    sub_re = re.compile(
        r"(localhost|tempo|prometheus|loki|otel-collector)(:\d+)?"
        r"|(?:[A-Za-z]:)?[\\/]\S+(?:tempo|loki|prometheus|grafana)\S*"
    )
    for src_rel, dst_name in pairs:
        src = (DEPLOY / src_rel).read_text()
        dst = (rendered / dst_name).read_text()
        src_norm = sub_re.sub("<SUB>", src)
        dst_norm = sub_re.sub("<SUB>", dst)
        assert src_norm == dst_norm, (
            f"{dst_name}: structural diff is more than hostnames+paths.\n"
            f"--- src normalized ---\n{src_norm}\n"
            f"--- dst normalized ---\n{dst_norm}"
        )
```

- [ ] **Step 2: Run, verify fail**

```
py -3 -m pytest tests/test_observability_render.py -v
```

Expected: 8 FAIL — module missing.

- [ ] **Step 3: Create `helix_context/launcher/observability_render.py`**

```python
"""Render runtime configs from deploy/otel sources for the native sidecar.

The deploy/otel YAMLs bake in Docker-Compose service-DNS hostnames
(`tempo:4317`, `http://prometheus:9090/...`, `http://loki:3100/otlp`,
`otel-collector:8889`) and Linux container paths (`/var/tempo/...`,
`/var/loki/...`, `/loki`). For the native runtime we substitute these
to `localhost:*` and `platformdirs.user_data_dir(...)` paths.

No structural changes. The diff between source and render is hostnames
+ paths only, enforced by tests/test_observability_render.py.

Usage:
    python -m helix_context.launcher.observability_render render-all
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

from .observability_paths import configs_dir, service_state_dir, state_dir

log = logging.getLogger("helix.launcher.render")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _deploy_dir() -> Path:
    return _repo_root() / "deploy" / "otel"


# ── substitution rules ───────────────────────────────────────────────
# Each rule: (compiled regex, replacement fn). Replacement fn returns the
# substituted string given the match.

def _path_for(service: str, *parts: str) -> str:
    """Return a forward-slashed absolute path string under the per-service
    state dir. We use forward slashes uniformly because Tempo/Loki/Grafana
    accept them on Windows AND yaml-load doesn't choke on backslashes.
    """
    base = service_state_dir(service)
    full = base.joinpath(*parts) if parts else base
    return str(full).replace("\\", "/")


def _sub_collector(text: str) -> str:
    text = text.replace("endpoint: tempo:4317", "endpoint: localhost:4317")
    text = text.replace(
        "endpoint: http://prometheus:9090/api/v1/write",
        "endpoint: http://localhost:9090/api/v1/write",
    )
    text = text.replace(
        "endpoint: http://loki:3100/otlp",
        "endpoint: http://localhost:3100/otlp",
    )
    return text


def _sub_prometheus(text: str) -> str:
    return text.replace("'otel-collector:8889'", "'localhost:8889'")


def _sub_tempo(text: str) -> str:
    text = text.replace("/var/tempo/traces", _path_for("tempo", "traces"))
    text = text.replace("/var/tempo/wal", _path_for("tempo", "wal"))
    text = text.replace(
        "/var/tempo/generator/wal",
        _path_for("tempo", "generator", "wal"),
    )
    text = text.replace(
        "url: http://prometheus:9090/api/v1/write",
        "url: http://localhost:9090/api/v1/write",
    )
    return text


def _sub_loki(text: str) -> str:
    """Loki's source uses `/loki` as path_prefix; we redirect to
    state_dir/loki and keep the chunks_directory + rules_directory etc.
    relative to that prefix.

    Loki resolves chunks_directory and rules_directory as ABSOLUTE paths
    if they start with '/', so we substitute the full absolute path
    rather than relying on path_prefix interpolation.
    """
    text = text.replace("path_prefix: /loki", f"path_prefix: {_path_for('loki')}")
    text = text.replace(
        "chunks_directory: /loki/chunks",
        f"chunks_directory: {_path_for('loki', 'chunks')}",
    )
    text = text.replace(
        "rules_directory: /loki/rules",
        f"rules_directory: {_path_for('loki', 'rules')}",
    )
    return text


def _sub_datasources(text: str) -> str:
    text = text.replace("url: http://prometheus:9090", "url: http://localhost:9090")
    text = text.replace("url: http://tempo:3200", "url: http://localhost:3200")
    text = text.replace("url: http://loki:3100", "url: http://localhost:3100")
    return text


_RULES = [
    # (source path relative to deploy/otel, rendered name, sub fn)
    ("otel-collector-config.yaml", "otel-collector-config.yaml", _sub_collector),
    ("prometheus.yml", "prometheus.yml", _sub_prometheus),
    ("tempo.yaml", "tempo.yaml", _sub_tempo),
    ("loki-config.yaml", "loki-config.yaml", _sub_loki),
    (
        "grafana/provisioning/datasources/datasources.yml",
        "datasources.yml",
        _sub_datasources,
    ),
]


def _wire_grafana_provisioning() -> None:
    """Copy the rendered datasources + source dashboards into Grafana's
    conf/provisioning tree so Grafana auto-loads them at startup.

    Grafana resolves provisioning relative to its --homepath, not relative
    to a CLI flag, so the rendered datasources.yml has to physically land
    at <graf_home>/conf/provisioning/datasources/datasources.yml.

    Best-effort: skips silently if Grafana isn't installed (the config
    render runs before the binary may have been extracted in some flows).
    """
    import shutil

    from .observability_paths import binary_path

    graf_bin = binary_path("grafana")
    if not graf_bin.exists():
        log.info("grafana binary absent — skipping provisioning wire-up")
        return
    graf_home = graf_bin.parent.parent  # tools/native-otel/grafana

    rendered_ds = configs_dir() / "datasources.yml"
    target_ds_dir = graf_home / "conf" / "provisioning" / "datasources"
    target_ds_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rendered_ds, target_ds_dir / "datasources.yml")

    deploy = _deploy_dir()
    src_dash_prov = deploy / "grafana" / "provisioning" / "dashboards"
    if src_dash_prov.exists():
        target_dash_prov = graf_home / "conf" / "provisioning" / "dashboards"
        target_dash_prov.mkdir(parents=True, exist_ok=True)
        for f in src_dash_prov.iterdir():
            if f.is_file():
                shutil.copy2(f, target_dash_prov / f.name)

    src_dash = deploy / "grafana" / "dashboards"
    if src_dash.exists():
        target_dash = graf_home / "conf" / "provisioning" / "dashboards-content"
        target_dash.mkdir(parents=True, exist_ok=True)
        for f in src_dash.iterdir():
            if f.is_file():
                shutil.copy2(f, target_dash / f.name)


def render_all() -> list[Path]:
    """Render every source into configs_dir(); return list of written paths.

    Creates state_dir() (and per-service subdirs touched in the rendered
    output) so binaries can write to them at first launch. Also wires the
    rendered datasources into Grafana's provisioning tree.
    """
    out_dir = configs_dir(create=True)
    state_dir(create=True)
    written: list[Path] = []
    for src_rel, dst_name, sub_fn in _RULES:
        src = _deploy_dir() / src_rel
        if not src.exists():
            raise FileNotFoundError(f"render: source missing: {src}")
        rendered = sub_fn(src.read_text(encoding="utf-8"))
        dst = out_dir / dst_name
        dst.write_text(rendered, encoding="utf-8")
        written.append(dst)
        log.info("render: wrote %s (%d bytes)", dst, len(rendered))

    # Ensure per-service state dirs exist (binaries need to write here).
    for svc in ("prometheus", "tempo", "loki", "grafana"):
        service_state_dir(svc, create=True)

    try:
        _wire_grafana_provisioning()
    except Exception:
        log.warning("grafana provisioning wire-up failed", exc_info=True)

    return written


def _cli(argv=None) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("render-all")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    )
    if args.cmd == "render-all":
        for p in render_all():
            print(str(p))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
```

- [ ] **Step 4: Run, verify pass**

```
py -3 -m pytest tests/test_observability_render.py -v
```

Expected: 8 PASS.

> If `test_no_docker_dns_hostnames_remain_in_any_render` still flags a hostname, find it in the source YAML and add a `_sub_*` rule for that file. The cross-cutting test exists exactly to catch hostnames the implementer missed.

- [ ] **Step 5: Commit**

```bash
git add helix_context/launcher/observability_render.py tests/test_observability_render.py
git commit -m "feat(launcher): observability_render — Docker-source → native config

Reads deploy/otel/{otel-collector-config.yaml,prometheus.yml,tempo.yaml,
loki-config.yaml,grafana/provisioning/datasources/datasources.yml},
substitutes Docker-DNS hostnames → localhost and Linux container paths
→ platformdirs user-state paths, writes the result to
tools/native-otel/configs/.

Pure string substitution — no structural changes to any config. The
diff between source and render is exactly hostnames + paths, enforced
by an end-to-end test that strips both classes from each pair and
asserts the remainder is byte-equal.

See docs/specs/2026-05-04-native-observability-sidecar-design.md §6.3."
```

---

## Task 6: Health module (`observability_health.py`)

**Files:**
- Create: `helix_context/launcher/observability_health.py`
- Create: `tests/test_observability_health.py`

Standalone — used by Task 7's supervisor for both startup readiness gating and the 30s health-poll loop.

@superpowers:test-driven-development

- [ ] **Step 1: Failing tests**

Create `tests/test_observability_health.py`:

```python
"""Tests for helix_context.launcher.observability_health."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _bind_port_in_thread(port: int) -> threading.Event:
    """Bind 127.0.0.1:port in a daemon thread; return an Event the caller
    sets to release the port."""
    bound = threading.Event()
    release = threading.Event()

    def _worker():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        bound.set()
        release.wait(timeout=10)
        s.close()

    threading.Thread(target=_worker, daemon=True).start()
    bound.wait(timeout=2)
    return release


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ok":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a, **kw):
        pass


@pytest.fixture
def http_server():
    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), _OkHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port
    srv.shutdown()


# ── port-bind poll ────────────────────────────────────────────────────

def test_wait_for_port_returns_true_when_bound():
    from helix_context.launcher.observability_health import wait_for_port
    port = _free_port()
    release = _bind_port_in_thread(port)
    try:
        assert wait_for_port("127.0.0.1", port, timeout=2.0) is True
    finally:
        release.set()


def test_wait_for_port_returns_false_on_timeout():
    from helix_context.launcher.observability_health import wait_for_port
    port = _free_port()  # nothing bound
    t0 = time.monotonic()
    assert wait_for_port("127.0.0.1", port, timeout=0.4) is False
    elapsed = time.monotonic() - t0
    # Within 1.5x the timeout — proves we honor the deadline.
    assert elapsed < 0.4 * 1.5


# ── HTTP poll ─────────────────────────────────────────────────────────

def test_wait_for_http_ok_returns_true_on_200(http_server):
    from helix_context.launcher.observability_health import wait_for_http_ok
    assert wait_for_http_ok(
        f"http://127.0.0.1:{http_server}/ok", timeout=3.0
    ) is True


def test_wait_for_http_ok_returns_false_on_404(http_server):
    from helix_context.launcher.observability_health import wait_for_http_ok
    assert wait_for_http_ok(
        f"http://127.0.0.1:{http_server}/missing", timeout=0.4
    ) is False


def test_wait_for_http_ok_returns_false_on_unreachable():
    from helix_context.launcher.observability_health import wait_for_http_ok
    port = _free_port()  # nobody bound
    t0 = time.monotonic()
    assert wait_for_http_ok(
        f"http://127.0.0.1:{port}/anything", timeout=0.4
    ) is False
    assert time.monotonic() - t0 < 0.4 * 1.5
```

- [ ] **Step 2: Run, verify fail**

```
py -3 -m pytest tests/test_observability_health.py -v
```

Expected: 5 FAIL — module missing.

- [ ] **Step 3: Create `helix_context/launcher/observability_health.py`**

```python
"""Health probes for the observability subprocesses.

Two primitives:
    - wait_for_port(host, port, timeout): TCP-connect poll until bound or timeout
    - wait_for_http_ok(url, timeout): HTTP GET poll until 2xx or timeout

Used by the supervisor to gate spawn-order phases (port-bind ready)
and to drive the 30-second health-loop tray indicator (HTTP /-/healthy etc).

Per global preference: every HTTP call has an explicit timeout=.
"""

from __future__ import annotations

import logging
import socket
import time
from typing import Optional

import httpx

log = logging.getLogger("helix.launcher.observability_health")

_POLL_INTERVAL_S = 0.5


def wait_for_port(host: str, port: int, *, timeout: float = 30.0) -> bool:
    """TCP-connect poll until bound or timeout. Never raises."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect((host, port))
            return True
        except (ConnectionRefusedError, OSError):
            time.sleep(_POLL_INTERVAL_S)
    return False


def is_port_bound(host: str, port: int) -> bool:
    """Single-shot variant — used for the port-collision pre-flight check."""
    return wait_for_port(host, port, timeout=0.2)


def wait_for_http_ok(
    url: str,
    *,
    timeout: float = 30.0,
    expect_status: int = 200,
) -> bool:
    """HTTP GET poll until expect_status or timeout. Never raises."""
    deadline = time.monotonic() + timeout
    last_error: Optional[str] = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=2.0)
            if resp.status_code == expect_status:
                return True
            last_error = f"HTTP {resp.status_code}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(_POLL_INTERVAL_S)
    log.debug("wait_for_http_ok timeout: %s (last=%s)", url, last_error)
    return False


# ── Per-service health-endpoint registry ──────────────────────────────
# spec §7.7 — used by the supervisor's 30s polling loop.

HEALTH_ENDPOINTS: dict[str, str] = {
    "collector":  "http://localhost:13133/",            # health_check ext default
    "prometheus": "http://localhost:9090/-/healthy",
    "tempo":      "http://localhost:3200/ready",
    "loki":       "http://localhost:3100/ready",
    "grafana":    "http://localhost:3000/api/health",
}

# Ports a healthy instance binds. Used both for the spawn-order port-bind
# poll (§7.3) and the port-collision pre-flight check (§7.2).
SERVICE_PORTS: dict[str, list[int]] = {
    "collector":  [4317, 4318, 8889],
    "prometheus": [9090],
    "tempo":      [3200],
    "loki":       [3100],
    "grafana":    [3000],
}
```

- [ ] **Step 4: Run, verify pass**

```
py -3 -m pytest tests/test_observability_health.py -v
```

Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add helix_context/launcher/observability_health.py tests/test_observability_health.py
git commit -m "feat(launcher): observability_health — port + HTTP poll primitives

wait_for_port / wait_for_http_ok with explicit timeout, plus a registry
of per-service health endpoints (collector :13133/, prom /-/healthy,
tempo /ready, loki /ready, grafana /api/health) and a registry of
expected listen ports for the supervisor's spawn-order gating and
port-collision pre-flight.

See docs/specs/2026-05-04-native-observability-sidecar-design.md §7.2 §7.3 §7.7."
```

---

## Task 7: ObservabilitySupervisor (subprocess lifecycle owner)

**Files:**
- Create: `helix_context/launcher/observability_supervisor.py`
- Create: `tests/test_observability_supervisor.py`

The biggest task in the plan. Owns 5 child PIDs, sequences spawn-order per spec §7.3 (Phase 1: prom+tempo+loki parallel, Phase 2: wait, Phase 3: collector, Phase 4: wait, Phase 5: grafana), wires Job Object on Windows / setsid on POSIX, drives the 30s health loop, exposes restart-by-service.

@superpowers:test-driven-development

- [ ] **Step 1: Tests for the spawn-order + cleanup paths**

Create `tests/test_observability_supervisor.py`:

```python
"""Tests for ObservabilitySupervisor.

All subprocess.Popen calls are mocked — no real binaries spawn. Tests
cover spawn-order, port pre-flight, refusal-without-rendered-config,
Job Object setup on Windows, cleanup cascade.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


SERVICES = ("collector", "prometheus", "tempo", "loki", "grafana")


@pytest.fixture
def fake_paths(tmp_path, monkeypatch):
    """Redirect state + configs into tmp_path; pretend binaries + configs exist."""
    from helix_context.launcher import observability_paths as ops

    monkeypatch.setattr(ops, "_user_data_dir", lambda: tmp_path / "appdata")
    monkeypatch.setattr(
        ops, "_repo_root",
        lambda: tmp_path / "repo",
    )

    # Pretend each binary + each rendered config exists.
    (tmp_path / "appdata" / "observability").mkdir(parents=True, exist_ok=True)
    cfg_dir = tmp_path / "repo" / "tools" / "native-otel" / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "otel-collector-config.yaml",
        "prometheus.yml",
        "tempo.yaml",
        "loki-config.yaml",
        "datasources.yml",
    ):
        (cfg_dir / name).write_text("# stub\n", encoding="utf-8")

    for svc in SERVICES:
        bp = ops.binary_path(svc)
        bp.parent.mkdir(parents=True, exist_ok=True)
        bp.write_bytes(b"\x7fELF")  # placeholder
    return tmp_path


def _supervisor(fake_paths):
    from helix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )
    return ObservabilitySupervisor()


# ── refusal-without-rendered-config ─────────────────────────────────

def test_refuses_to_spawn_when_rendered_config_missing(fake_paths):
    from helix_context.launcher import observability_paths as ops
    from helix_context.launcher.observability_supervisor import (
        ConfigsMissing,
        ObservabilitySupervisor,
    )
    # Remove one rendered config.
    (ops.configs_dir() / "prometheus.yml").unlink()

    sup = ObservabilitySupervisor()
    with pytest.raises(ConfigsMissing):
        sup.start_all()


# ── port pre-flight ─────────────────────────────────────────────────

def test_port_already_bound_skips_spawn_and_marks_external(fake_paths):
    from helix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )

    def _make_proc(*a, **kw):
        m = MagicMock()
        m.pid = 22000
        m.poll.return_value = None
        return m

    with patch(
        "helix_context.launcher.observability_supervisor.is_port_bound",
        side_effect=lambda host, port: port == 9090,
    ), patch(
        "helix_context.launcher.observability_supervisor.subprocess.Popen",
        side_effect=_make_proc,
    ) as popen, patch(
        "helix_context.launcher.observability_supervisor.wait_for_port",
        return_value=True,
    ):
        sup = ObservabilitySupervisor()
        sup.start_all()
        # Each Popen call's first positional arg is the cmd list; the
        # binary path is its first element.
        spawned_cmds = [call.args[0] for call in popen.call_args_list]
        spawned_bin_paths = [str(cmd[0]) for cmd in spawned_cmds]
        # No prometheus binary in the spawn list.
        assert not any("prometheus" in p for p in spawned_bin_paths), (
            f"prometheus should be skipped when :9090 is bound; got {spawned_bin_paths}"
        )
        assert sup.status("prometheus") == "external"


# ── spawn order ─────────────────────────────────────────────────────

def test_spawn_order_phase1_then_collector_then_grafana(fake_paths):
    """Phase 1: prom+tempo+loki spawn first; collector after they're ready;
    grafana last. We assert by recording the order of Popen calls."""
    from helix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )

    spawn_order = []
    def _record(*args, **kwargs):
        cmd = args[0]
        for svc in SERVICES:
            if any(svc in str(p) for p in cmd):
                spawn_order.append(svc)
                break
        m = MagicMock()
        m.pid = 12345
        m.poll.return_value = None
        return m

    with patch(
        "helix_context.launcher.observability_supervisor.is_port_bound",
        return_value=False,
    ), patch(
        "helix_context.launcher.observability_supervisor.subprocess.Popen",
        side_effect=_record,
    ), patch(
        "helix_context.launcher.observability_supervisor.wait_for_port",
        return_value=True,
    ):
        sup = ObservabilitySupervisor()
        sup.start_all()

    # Phase 1 services come before collector; collector before grafana.
    p1 = {"prometheus", "tempo", "loki"}
    collector_idx = spawn_order.index("collector")
    grafana_idx = spawn_order.index("grafana")
    for s in p1:
        assert spawn_order.index(s) < collector_idx, (
            f"{s} must spawn before collector; got order={spawn_order}"
        )
    assert collector_idx < grafana_idx


# ── shutdown cascade ────────────────────────────────────────────────

def test_shutdown_terminates_all_children(fake_paths):
    from helix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )
    procs = []
    def _make(*a, **kw):
        m = MagicMock()
        m.pid = 22000 + len(procs)
        m.poll.return_value = None
        procs.append(m)
        return m

    with patch(
        "helix_context.launcher.observability_supervisor.is_port_bound",
        return_value=False,
    ), patch(
        "helix_context.launcher.observability_supervisor.subprocess.Popen",
        side_effect=_make,
    ), patch(
        "helix_context.launcher.observability_supervisor.wait_for_port",
        return_value=True,
    ):
        sup = ObservabilitySupervisor()
        sup.start_all()
        sup.shutdown()

    for m in procs:
        assert m.terminate.called or m.kill.called, (
            "every child must receive terminate or kill on shutdown"
        )


# ── Windows Job Object setup ────────────────────────────────────────

@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_job_object_created_on_windows(fake_paths):
    """When the supervisor starts, it should construct a Job Object with
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE so child PIDs auto-terminate when
    the parent process dies (clean OR force-killed)."""
    from helix_context.launcher import observability_supervisor as os_mod

    fake_job = MagicMock()
    fake_job.handle = 0xCAFE

    with patch.object(
        os_mod,
        "_create_kill_on_close_job",
        return_value=fake_job,
    ) as create_job, patch(
        "helix_context.launcher.observability_supervisor.is_port_bound",
        return_value=False,
    ), patch(
        "helix_context.launcher.observability_supervisor.subprocess.Popen",
    ) as popen, patch(
        "helix_context.launcher.observability_supervisor._assign_to_job",
    ) as assign, patch(
        "helix_context.launcher.observability_supervisor.wait_for_port",
        return_value=True,
    ):
        m = MagicMock()
        m.pid = 9999
        m.poll.return_value = None
        popen.return_value = m

        from helix_context.launcher.observability_supervisor import (
            ObservabilitySupervisor,
        )
        sup = ObservabilitySupervisor()
        sup.start_all()

        # Job created exactly once.
        create_job.assert_called_once()
        # Every child PID added to the job (5 services minus any externally
        # adopted; here none are external).
        assert assign.call_count == 5


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
def test_posix_uses_start_new_session(fake_paths):
    from helix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )
    captured_kwargs = []
    def _capture(*args, **kwargs):
        captured_kwargs.append(kwargs)
        m = MagicMock()
        m.pid = 7777
        m.poll.return_value = None
        return m

    with patch(
        "helix_context.launcher.observability_supervisor.is_port_bound",
        return_value=False,
    ), patch(
        "helix_context.launcher.observability_supervisor.subprocess.Popen",
        side_effect=_capture,
    ), patch(
        "helix_context.launcher.observability_supervisor.wait_for_port",
        return_value=True,
    ):
        sup = ObservabilitySupervisor()
        sup.start_all()

    for kw in captured_kwargs:
        assert kw.get("start_new_session") is True


# ── per-service restart ─────────────────────────────────────────────

def test_restart_service_kills_then_respawns(fake_paths):
    from helix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )

    procs_made = []
    def _make(*a, **kw):
        m = MagicMock()
        m.pid = 30000 + len(procs_made)
        m.poll.return_value = None
        procs_made.append(m)
        return m

    with patch(
        "helix_context.launcher.observability_supervisor.is_port_bound",
        return_value=False,
    ), patch(
        "helix_context.launcher.observability_supervisor.subprocess.Popen",
        side_effect=_make,
    ), patch(
        "helix_context.launcher.observability_supervisor.wait_for_port",
        return_value=True,
    ):
        sup = ObservabilitySupervisor()
        sup.start_all()
        prom_first = sup._procs["prometheus"]
        sup.restart_service("prometheus")
        prom_second = sup._procs["prometheus"]

    assert prom_first is not prom_second, "restart should produce a new Popen"
    assert prom_first.terminate.called or prom_first.kill.called
```

- [ ] **Step 2: Run, verify fail**

```
py -3 -m pytest tests/test_observability_supervisor.py -v
```

Expected: all FAIL — module missing.

- [ ] **Step 3: Create `helix_context/launcher/observability_supervisor.py`**

```python
"""ObservabilitySupervisor — owns the 5 native-binary subprocesses.

Lifecycle:
    1. Validate rendered configs exist (refuse otherwise — spec §7.1).
    2. Port pre-flight: skip-and-mark-external for any port already bound.
    3. Phase 1: parallel-spawn prometheus + tempo + loki.
    4. Wait for those three to bind their ports (30s timeout).
    5. Phase 2: spawn collector. Wait for ready.
    6. Phase 3: spawn grafana.
    7. Background loop: every 30s, HTTP-probe each service. Update status.
    8. shutdown(): SIGTERM-equivalent each child, wait 5s, escalate to KILL.

Cleanup guarantee (Windows): all spawned children join a Windows Job
Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE so an abnormal exit of
the launcher process kills the children at the OS level. POSIX uses
start_new_session for the same effect via process-group signalling.

Spec: docs/specs/2026-05-04-native-observability-sidecar-design.md §7.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .observability_health import (
    HEALTH_ENDPOINTS,
    SERVICE_PORTS,
    is_port_bound,
    wait_for_http_ok,
    wait_for_port,
)
from .observability_paths import (
    binary_path,
    configs_dir,
    logs_dir,
    service_state_dir,
    state_dir,
)

log = logging.getLogger("helix.launcher.observability")

_HEALTH_POLL_INTERVAL_S = 30.0
_TERM_GRACE_S = 5.0


# ── Spawn-order ──────────────────────────────────────────────────────
# Phase 1 spawn together; phase 2 waits for them; collector spawns; wait;
# grafana last. Spec §7.3.
SPAWN_PHASES: List[List[str]] = [
    ["prometheus", "tempo", "loki"],
    ["collector"],
    ["grafana"],
]

ALL_SERVICES: List[str] = [s for phase in SPAWN_PHASES for s in phase]


# ── Status enum (kept as plain strings for tray-menu readability) ────
STATUS_GREEN = "green"        # alive + last health probe ok
STATUS_RED = "red"            # spawned but health probe failed
STATUS_EXTERNAL = "external"  # port bound by something else; we did not spawn
STATUS_PENDING = "pending"    # spawned, awaiting first health probe
STATUS_DOWN = "down"          # not yet started


class ObservabilityError(Exception):
    """Base."""


class ConfigsMissing(ObservabilityError):
    """A rendered config is absent — supervisor refuses to spawn."""


class BinariesMissing(ObservabilityError):
    """A native binary is absent — supervisor refuses to spawn."""


# ── Job Object (Windows) hooks. Real impls in win_job.py if present; ──
# we use a thin shim so test mocks can patch the names.

def _create_kill_on_close_job():
    """Create a Windows Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.

    Returns an opaque handle; raises on non-Windows or when pywin32 is
    missing. Test code patches this function to bypass the real syscall.
    """
    if sys.platform != "win32":
        raise RuntimeError("Job Objects are Windows-only")
    try:
        import win32job  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "pywin32 required for Job Object cleanup. "
            "Install with: pip install helix-context[launcher-tray]"
        ) from exc
    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation,
    )
    info["BasicLimitInformation"]["LimitFlags"] |= (
        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    win32job.SetInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation, info,
    )
    return job


def _assign_to_job(job, pid: int) -> None:
    """Attach pid to the Job Object. Test mocks patch this name."""
    if sys.platform != "win32":
        return
    import win32api  # type: ignore
    import win32con  # type: ignore
    import win32job  # type: ignore
    # Open by PID with rights needed for SetInformation.
    h = win32api.OpenProcess(
        win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
        False,
        pid,
    )
    try:
        win32job.AssignProcessToJobObject(job, h)
    finally:
        win32api.CloseHandle(h)


@dataclass
class _Service:
    name: str
    status: str = STATUS_DOWN
    proc: Optional[subprocess.Popen] = None
    log_path: Optional[Path] = None
    last_health_at: float = 0.0


class ObservabilitySupervisor:
    """Owns lifecycles of all five observability subprocesses."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._services: Dict[str, _Service] = {
            n: _Service(name=n) for n in ALL_SERVICES
        }
        self._job_handle = None  # Windows-only
        self._health_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ── public surface ────────────────────────────────────────────

    def status(self, service: str) -> str:
        with self._lock:
            return self._services[service].status

    def all_statuses(self) -> Dict[str, str]:
        with self._lock:
            return {s.name: s.status for s in self._services.values()}

    @property
    def _procs(self) -> Dict[str, Optional[subprocess.Popen]]:
        # Read-only convenience for tests / tray menu.
        return {s.name: s.proc for s in self._services.values()}

    # ── precondition checks ──────────────────────────────────────

    def _verify_configs(self) -> None:
        cfg = configs_dir()
        required = [
            "otel-collector-config.yaml",
            "prometheus.yml",
            "tempo.yaml",
            "loki-config.yaml",
            "datasources.yml",
        ]
        missing = [n for n in required if not (cfg / n).exists()]
        if missing:
            raise ConfigsMissing(
                f"Rendered configs missing: {missing}. "
                "Re-run scripts/install-native-observability.{ps1,sh}."
            )

    def _verify_binaries(self) -> None:
        missing = [s for s in ALL_SERVICES if not binary_path(s).exists()]
        if missing:
            raise BinariesMissing(
                f"Native binaries missing: {missing}. "
                "Re-run scripts/install-native-observability.{ps1,sh}."
            )

    # ── start sequence ───────────────────────────────────────────

    def start_all(self, *, phase_timeout: float = 30.0) -> None:
        """Run the spawn-order sequence per spec §7.3."""
        self._verify_configs()
        self._verify_binaries()

        # Create per-user state dirs (binaries write here).
        # Collector has no on-disk state, but creating an empty dir is harmless
        # and keeps the loop one line.
        state_dir(create=True)
        for s in ALL_SERVICES:
            service_state_dir(s, create=True)

        # Job Object on Windows.
        if sys.platform == "win32" and self._job_handle is None:
            try:
                self._job_handle = _create_kill_on_close_job()
            except Exception:
                log.warning(
                    "Could not create Job Object — falling back to atexit-only "
                    "cleanup (children may survive abnormal launcher exit)",
                    exc_info=True,
                )
                self._job_handle = None

        # Run each phase.
        for phase in SPAWN_PHASES:
            spawned: List[str] = []
            for svc in phase:
                if self._maybe_external(svc):
                    continue
                self._spawn(svc)
                spawned.append(svc)
            for svc in spawned:
                self._wait_phase_ready(svc, timeout=phase_timeout)

        self._start_health_loop()

    def _maybe_external(self, svc: str) -> bool:
        """If any of svc's ports are already bound, mark external + skip."""
        for port in SERVICE_PORTS[svc]:
            if is_port_bound("127.0.0.1", port):
                log.info(
                    "[%s] external instance detected on :%d — not spawning",
                    svc, port,
                )
                with self._lock:
                    self._services[svc].status = STATUS_EXTERNAL
                return True
        return False

    def _spawn(self, svc: str) -> None:
        cmd = self._command_for(svc)
        log.info("[%s] spawn %s", svc, " ".join(str(p) for p in cmd))

        log_path = logs_dir(create=True) / f"{svc}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        creationflags = 0
        start_new_session = False
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            start_new_session = True

        with open(log_path, "ab") as logf:
            proc = subprocess.Popen(
                cmd,
                stdout=logf,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                start_new_session=start_new_session,
                close_fds=True,
            )

        # Attach to Job Object (Windows only).
        if sys.platform == "win32" and self._job_handle is not None:
            try:
                _assign_to_job(self._job_handle, proc.pid)
            except Exception:
                log.warning("[%s] Job Object assignment failed", svc, exc_info=True)

        with self._lock:
            self._services[svc].proc = proc
            self._services[svc].log_path = log_path
            self._services[svc].status = STATUS_PENDING

    def _command_for(self, svc: str) -> List[str]:
        bin_p = str(binary_path(svc))
        cfg = configs_dir()
        state = service_state_dir(svc, create=True)

        if svc == "collector":
            return [bin_p, f"--config={cfg / 'otel-collector-config.yaml'}"]
        if svc == "prometheus":
            return [
                bin_p,
                f"--config.file={cfg / 'prometheus.yml'}",
                f"--storage.tsdb.path={state}",
                "--storage.tsdb.retention.time=14d",
                "--storage.tsdb.retention.size=4GB",
                "--web.enable-remote-write-receiver",
            ]
        if svc == "tempo":
            return [bin_p, f"-config.file={cfg / 'tempo.yaml'}"]
        if svc == "loki":
            return [bin_p, f"-config.file={cfg / 'loki-config.yaml'}"]
        if svc == "grafana":
            # Grafana finds its conf/provisioning via working dir.
            graf_home = binary_path("grafana").parent.parent  # tools/native-otel/grafana
            # Provisioning lives in repo deploy/otel/grafana/provisioning,
            # but datasources MUST be the rendered (localhost) variant.
            # The render module + bootstrap together copy:
            #   configs/datasources.yml ──► graf_home/conf/provisioning/datasources/datasources.yml
            #   deploy/otel/grafana/provisioning/dashboards/* ──► graf_home/conf/provisioning/dashboards/
            #   deploy/otel/grafana/dashboards/* ──► graf_home/conf/provisioning/dashboards-content/
            # This wiring lives in observability_render.render_all (Task 5)
            # under _wire_grafana_provisioning helper. See spec §6.3.
            return [
                bin_p,
                f"--homepath={graf_home}",
                f"--config={graf_home / 'conf' / 'defaults.ini'}",
            ]
        raise ValueError(svc)

    def _wait_phase_ready(self, svc: str, *, timeout: float) -> None:
        primary_port = SERVICE_PORTS[svc][0]
        ok = wait_for_port("127.0.0.1", primary_port, timeout=timeout)
        with self._lock:
            self._services[svc].status = STATUS_GREEN if ok else STATUS_RED
        if not ok:
            log.warning("[%s] did not bind :%d within %.1fs",
                        svc, primary_port, timeout)

    # ── health loop ──────────────────────────────────────────────

    def _start_health_loop(self) -> None:
        if self._health_thread is not None:
            return
        self._health_thread = threading.Thread(
            target=self._health_loop, name="obs-health", daemon=True,
        )
        self._health_thread.start()

    def _health_loop(self) -> None:
        while not self._stop_event.is_set():
            for svc in ALL_SERVICES:
                with self._lock:
                    status = self._services[svc].status
                if status == STATUS_EXTERNAL:
                    continue
                if status == STATUS_DOWN:
                    continue
                ok = wait_for_http_ok(
                    HEALTH_ENDPOINTS[svc], timeout=4.0,
                )
                with self._lock:
                    self._services[svc].status = (
                        STATUS_GREEN if ok else STATUS_RED
                    )
                    self._services[svc].last_health_at = time.time()
            self._stop_event.wait(timeout=_HEALTH_POLL_INTERVAL_S)

    # ── per-service control ──────────────────────────────────────

    def restart_service(self, svc: str) -> None:
        log.info("[%s] restart", svc)
        self._kill(svc)
        self._spawn(svc)
        self._wait_phase_ready(svc, timeout=30.0)

    def _kill(self, svc: str) -> None:
        with self._lock:
            proc = self._services[svc].proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except Exception:
            log.warning("[%s] terminate failed", svc, exc_info=True)
        deadline = time.monotonic() + _TERM_GRACE_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                log.warning("[%s] kill failed", svc, exc_info=True)
        with self._lock:
            self._services[svc].status = STATUS_DOWN
            self._services[svc].proc = None

    # ── shutdown ─────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Terminate all spawned children. Job Object would do this anyway
        on Windows when the parent dies, but this path runs on the clean
        exit + tray-Quit path so the OS has nothing to clean up."""
        log.info("ObservabilitySupervisor: shutdown")
        self._stop_event.set()
        for svc in reversed(ALL_SERVICES):
            self._kill(svc)
        # Releasing the Job Object handle triggers the kill-on-close
        # cleanup for any child we missed. Closing it is implicit when
        # the Python object is GC'd, but explicit is cheap insurance.
        if self._job_handle is not None and sys.platform == "win32":
            try:
                import win32api  # type: ignore
                win32api.CloseHandle(self._job_handle)
            except Exception:
                pass
            self._job_handle = None
```

- [ ] **Step 4: Run, verify pass**

```
py -3 -m pytest tests/test_observability_supervisor.py -v
```

Expected: all PASS on Windows; the Windows-skipped test is xfail-equivalent on POSIX, the POSIX-only test is xfail-equivalent on Windows.

If `test_job_object_created_on_windows` fails because pywin32 is missing in the dev venv, install: `py -3 -m pip install pywin32` (or the [launcher-tray] extra after Task 10 lands).

- [ ] **Step 5: Commit**

```bash
git add helix_context/launcher/observability_supervisor.py tests/test_observability_supervisor.py
git commit -m "feat(launcher): ObservabilitySupervisor — 5-child lifecycle owner

Sequences spawn-order per spec §7.3 (Phase 1: prom+tempo+loki parallel,
Phase 2: collector, Phase 3: grafana), pre-flights ports for external
instances, attaches children to a Windows Job Object with
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE so abnormal launcher exit cleans up
the OS-level (POSIX uses start_new_session for the same guarantee).
30s health-poll loop updates per-service status. Refuses to spawn when
any rendered config is absent (spec §7.1 + spec-review tightening).

See docs/specs/2026-05-04-native-observability-sidecar-design.md §7."
```

---

## Task 8: Tray menu integration (Observability submenu + balloon)

**Files:**
- Modify: `helix_context/launcher/tray.py`

Adds an Observability submenu to the existing tray menu: per-service status indicators, "Restart [service]" actions, "Open log directory", and a balloon notification for the first-launch case (per spec §11.4 locked decision).

- [ ] **Step 1: Test the menu construction**

Append to `tests/test_launcher_tray.py`:

```python
def _menu_titles(menu) -> list:
    """Robust extraction of item.text strings from a pystray.Menu.

    pystray.Menu exposes its items via .items in 0.19+; older versions
    expose ._items. Either way we want the list of MenuItem.text values
    (or None for separators). This helper exists so tests don't break
    when pystray bumps minor versions.
    """
    raw = getattr(menu, "items", None)
    if raw is None:
        raw = getattr(menu, "_items", [])
    out = []
    for it in raw:
        out.append(getattr(it, "text", None))
    return out


def test_tray_observability_submenu_built_when_supervisor_present(tmp_path):
    """When an ObservabilitySupervisor is wired, the tray menu gains an
    Observability submenu with per-service status entries."""
    pytest.importorskip("pystray")  # only meaningful if [launcher-tray] installed
    from helix_context.launcher.tray import HelixTrayIcon
    from helix_context.launcher.observability_supervisor import (
        ObservabilitySupervisor,
    )
    from helix_context.launcher.state import StateStore
    from helix_context.launcher.supervisor import HelixSupervisor

    store = StateStore(path=tmp_path / "state.json")
    helix_sup = HelixSupervisor(
        store=store, helix_host="127.0.0.1", helix_port=11999,
        helix_log_path=tmp_path / "h.log",
    )
    obs_sup = ObservabilitySupervisor()
    icon = HelixTrayIcon(
        supervisor=helix_sup,
        dashboard_url="http://127.0.0.1:11438",
        observability_supervisor=obs_sup,
    )
    # The Observability submenu lives as a single item titled "Observability";
    # the per-service status content is rendered when the submenu is opened.
    titles = _menu_titles(icon._build_menu())
    assert "Observability" in titles


def test_tray_observability_submenu_omitted_without_supervisor(tmp_path):
    """No supervisor wired → no Observability submenu (clean menu for
    users who opted out)."""
    pytest.importorskip("pystray")
    from helix_context.launcher.tray import HelixTrayIcon
    from helix_context.launcher.state import StateStore
    from helix_context.launcher.supervisor import HelixSupervisor

    store = StateStore(path=tmp_path / "state.json")
    helix_sup = HelixSupervisor(
        store=store, helix_host="127.0.0.1", helix_port=11999,
        helix_log_path=tmp_path / "h.log",
    )
    icon = HelixTrayIcon(
        supervisor=helix_sup,
        dashboard_url="http://127.0.0.1:11438",
        observability_supervisor=None,
    )
    titles = _menu_titles(icon._build_menu())
    assert "Observability" not in titles
```

- [ ] **Step 2: Run, verify fail**

```
py -3 -m pytest tests/test_launcher_tray.py -k observability -v
```

Expected: 2 FAIL — `HelixTrayIcon` does not accept an `observability_supervisor` keyword.

- [ ] **Step 3: Modify `helix_context/launcher/tray.py`**

In `__init__`, after the existing `headroom_dashboard_url` parameter, accept the new parameter and stash:

```python
def __init__(
    self,
    supervisor: HelixSupervisor,
    dashboard_url: str,
    name: str = "helix-launcher",
    tooltip: str = "Helix Launcher",
    on_quit: Optional[Callable[[], None]] = None,
    grafana_url: Optional[str] = None,
    prometheus_url: Optional[str] = None,
    headroom_supervisor=None,
    headroom_dashboard_url: Optional[str] = None,
    observability_supervisor=None,    # NEW
) -> None:
    # ... existing body ...
    self.observability = observability_supervisor
```

Add handler methods near the headroom handlers:

```python
def _restart_obs_service(self, service: str):
    def _h(icon, item):  # noqa: ARG001 — pystray API
        if self.observability is None:
            return
        log.info("Tray: restart observability/%s", service)
        try:
            self.observability.restart_service(service)
        except Exception:
            log.warning("Tray restart obs/%s failed", service, exc_info=True)
        finally:
            self._refresh_menu()
    return _h

def _open_obs_log_dir(self, icon, item):  # noqa: ARG002
    from .observability_paths import logs_dir
    p = logs_dir(create=True)
    log.info("Tray: open log dir %s", p)
    try:
        if os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
        else:
            import subprocess
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen(
                [opener, str(p)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                start_new_session=(sys.platform != "win32"),
            )
    except Exception:
        log.warning("Tray: failed to open log dir", exc_info=True)
```

(Add `import sys` at the top of the module if not already present.)

In `_build_menu`, after the existing items but before the final `Quit` separator + item, append (only when `self.observability is not None`):

```python
if self.observability is not None:
    import pystray
    obs_services = ["collector", "prometheus", "tempo", "loki", "grafana"]

    def _status_label(svc: str):
        return lambda item: f"{svc.capitalize()}: {self.observability.status(svc)}"  # noqa: ARG005

    obs_items = [
        pystray.MenuItem(
            _status_label(svc), None, enabled=False,
        )
        for svc in obs_services
    ]
    obs_items.append(pystray.Menu.SEPARATOR)
    for svc in obs_services:
        obs_items.append(pystray.MenuItem(
            f"Restart {svc}", self._restart_obs_service(svc),
        ))
    obs_items.append(pystray.Menu.SEPARATOR)
    obs_items.append(pystray.MenuItem(
        "Open log directory", self._open_obs_log_dir,
    ))
    items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem(
        "Observability", pystray.Menu(*obs_items),
    ))
```

Add a `notify_install_needed` method to surface the spec §11.4 balloon:

```python
def notify_install_needed(self) -> None:
    """Show a Windows balloon prompting the user to install native
    observability binaries. Called by app.py after detecting the
    bootstrap is missing or incomplete (spec §11.4)."""
    if self._icon is None:
        return
    try:
        # pystray's notify is a no-op on backends that don't support it.
        self._icon.notify(
            "Native observability not installed — "
            "right-click the tray icon, choose Observability ▸ "
            "Install, or run scripts/install-native-observability.ps1",
            title="Helix Launcher",
        )
    except Exception:
        log.warning("notify_install_needed failed", exc_info=True)
```

- [ ] **Step 4: Run, verify pass**

```
py -3 -m pytest tests/test_launcher_tray.py -k observability -v
```

Expected: 2 PASS.

- [ ] **Step 5: Run the full tray test suite**

```
py -3 -m pytest tests/test_launcher_tray.py -v
```

Expected: all PASS — existing tests should not regress.

- [ ] **Step 6: Commit**

```bash
git add helix_context/launcher/tray.py tests/test_launcher_tray.py
git commit -m "feat(tray): Observability submenu with per-service status + restart

Wires ObservabilitySupervisor into the existing tray menu. New menu
section 'Observability' shows live status (green/red/external/pending/
down) per service, exposes 'Restart <service>' for manual recovery,
and 'Open log directory' opens the per-user state dir.

Adds notify_install_needed() for the spec §11.4 balloon-notification
first-launch UX (locked: no blocking prompt; balloon + tray pulse).

See docs/specs/2026-05-04-native-observability-sidecar-design.md §7.5 §11.4."
```

---

## Task 9: App-level wiring (`HELIX_OBSERVABILITY` env + supervisor build)

**Files:**
- Modify: `helix_context/launcher/app.py`
- Modify: `Start-helix-tray.bat`

Connects the new pieces into the launcher's startup path. Reads `HELIX_OBSERVABILITY`, decides skip/run, builds `ObservabilitySupervisor`, calls `start_all()` after the tray is up so balloon notifications can fire.

- [ ] **Step 1: Test the env-gating**

Append to `tests/test_launcher_app.py`:

```python
def test_observability_disabled_via_env(monkeypatch, tmp_path):
    """HELIX_OBSERVABILITY=0 → no supervisor built."""
    monkeypatch.setenv("HELIX_OBSERVABILITY", "0")
    from helix_context.launcher.app import _maybe_build_observability
    sup = _maybe_build_observability()
    assert sup is None


def test_observability_enabled_when_unset(monkeypatch, tmp_path):
    """Default — env unset → returns a supervisor (or None if configs
    haven't been rendered yet; either way, not silently disabled)."""
    monkeypatch.delenv("HELIX_OBSERVABILITY", raising=False)
    monkeypatch.setattr(
        "helix_context.launcher.app._observability_install_complete",
        lambda: True,
    )
    from helix_context.launcher.app import _maybe_build_observability
    sup = _maybe_build_observability()
    assert sup is not None


def test_observability_skipped_when_install_incomplete(monkeypatch):
    """Install incomplete → supervisor not built, tray notification queued
    via _set_observability_install_pending."""
    monkeypatch.delenv("HELIX_OBSERVABILITY", raising=False)
    monkeypatch.setattr(
        "helix_context.launcher.app._observability_install_complete",
        lambda: False,
    )
    pending = []
    monkeypatch.setattr(
        "helix_context.launcher.app._set_observability_install_pending",
        lambda v: pending.append(v),
    )
    from helix_context.launcher.app import _maybe_build_observability
    sup = _maybe_build_observability()
    assert sup is None
    assert pending == [True]
```

- [ ] **Step 2: Run, verify fail**

```
py -3 -m pytest tests/test_launcher_app.py -k observability -v
```

Expected: 3 FAIL — helpers missing.

- [ ] **Step 3: Add helpers + wire into `main()`**

In `helix_context/launcher/app.py`:

Add after the imports:

```python
from .observability_paths import binary_path, configs_dir
```

Add after `_maybe_build_headroom`:

```python
_OBS_INSTALL_PENDING = False


def _set_observability_install_pending(v: bool) -> None:
    global _OBS_INSTALL_PENDING
    _OBS_INSTALL_PENDING = bool(v)


def _observability_install_complete() -> bool:
    """True iff every binary AND every rendered config is present."""
    services = ("collector", "prometheus", "tempo", "loki", "grafana")
    rendered = (
        "otel-collector-config.yaml",
        "prometheus.yml",
        "tempo.yaml",
        "loki-config.yaml",
        "datasources.yml",
    )
    if not all(binary_path(s).exists() for s in services):
        return False
    cfg = configs_dir()
    return all((cfg / r).exists() for r in rendered)


def _maybe_build_observability():
    """Return an ObservabilitySupervisor, or None when:
        - HELIX_OBSERVABILITY=0 (opt-out)
        - install is incomplete (sets pending flag for tray balloon)
        - import error (extras not installed)
    """
    if os.environ.get("HELIX_OBSERVABILITY", "1").strip() in ("0", "false", "no", "off"):
        log.info("Observability skipped: HELIX_OBSERVABILITY=0")
        return None

    if not _observability_install_complete():
        log.info(
            "Observability install incomplete; tray will surface a balloon. "
            "Run scripts/install-native-observability.ps1 to enable."
        )
        _set_observability_install_pending(True)
        return None

    try:
        from .observability_supervisor import ObservabilitySupervisor
        return ObservabilitySupervisor()
    except ImportError:
        log.warning(
            "Observability deps missing — install with "
            "pip install helix-context[launcher-tray]",
            exc_info=True,
        )
        return None
```

In the `--tray` branch of `main()`, after the existing `tray_icon = HelixTrayIcon(...)` construction, add the observability wiring **before** `tray_icon.run()`:

```python
        observability_sup = _maybe_build_observability()

        tray_icon = HelixTrayIcon(
            supervisor=supervisor,
            dashboard_url=url,
            grafana_url=args.grafana_url,
            prometheus_url=args.prometheus_url,
            headroom_supervisor=headroom_supervisor,
            headroom_dashboard_url=headroom_dashboard_url,
            observability_supervisor=observability_sup,
        )

        # Start observability subprocesses BEFORE tray_icon.run() blocks.
        # If start_all raises (configs missing despite the pre-check, or a
        # binary fails to spawn), log + continue — helix-context itself
        # must not be blocked on observability.
        if observability_sup is not None:
            try:
                observability_sup.start_all()
            except Exception:
                log.warning(
                    "ObservabilitySupervisor.start_all failed; "
                    "tray will indicate via per-service red status",
                    exc_info=True,
                )

        # Surface the install-needed balloon if we set the flag earlier.
        if _OBS_INSTALL_PENDING:
            try:
                # Defer one tick so the icon is fully constructed.
                threading.Timer(1.0, tray_icon.notify_install_needed).start()
            except Exception:
                log.warning("install-needed balloon scheduling failed", exc_info=True)

        log.info("Tray mode active — dashboard at %s", url)
        log.info("Click the tray icon to open the dashboard; Quit from its menu to exit.")
        tray_icon.run()  # blocks until Quit

        # Tray exited — shut down observability (Job Object would do this on
        # Windows even on hard exit, but the clean path is courteous).
        if observability_sup is not None:
            try:
                observability_sup.shutdown()
            except Exception:
                log.warning("ObservabilitySupervisor.shutdown failed", exc_info=True)
        return 0
```

- [ ] **Step 4: Update `Start-helix-tray.bat`**

After the existing `set "HELIX_OTEL_*"` block (around line 17-20), add:

```bat
REM ── Native observability sidecar (default ON) ───────────────────
REM The tray launcher manages 5 native binaries (Prometheus, Tempo,
REM Loki, Grafana, OTel Collector). First launch prompts to install
REM if scripts/install-native-observability.ps1 hasn't been run yet.
REM
REM Set HELIX_OBSERVABILITY=0 to skip — useful when you're using the
REM Docker compose stack at deploy/otel/ instead, or want no obs at all.
REM set "HELIX_OBSERVABILITY=0"
```

- [ ] **Step 5: Run, verify pass**

```
py -3 -m pytest tests/test_launcher_app.py -k observability -v
py -3 -m pytest tests/test_launcher_app.py -v
```

Expected: 3 new PASS; existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add helix_context/launcher/app.py Start-helix-tray.bat tests/test_launcher_app.py
git commit -m "feat(launcher): wire HELIX_OBSERVABILITY env + supervisor hand-off

main() in --tray mode now:
  - Reads HELIX_OBSERVABILITY (default ON; '0' opts out)
  - Detects incomplete install, queues balloon notification, skips
    spawning until install finishes (spec §11.4 locked: no blocking prompt)
  - Builds ObservabilitySupervisor; passes it to HelixTrayIcon
  - Calls start_all() before tray_icon.run() blocks
  - Calls shutdown() after Quit returns

Helix-context itself is never blocked on observability — any failure
in start_all is logged and surfaces in the tray submenu's red status.

Start-helix-tray.bat documents the opt-out env var inline.

See docs/specs/2026-05-04-native-observability-sidecar-design.md §7 §11.4."
```

---

## Task 10: `pyproject.toml` deps update

**Files:**
- Modify: `pyproject.toml`

Adds `platformdirs` to `[launcher]` (cross-platform, used always when launcher is installed) and `pywin32` to `[launcher-tray]` with a `sys_platform=='win32'` marker so non-Windows installs don't fail.

- [ ] **Step 1: Failing test**

Append to `tests/test_packaging.py` (create the file if it doesn't exist):

```python
"""Tests for pyproject.toml extras-matrix invariants."""

from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore


REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"


def _extras() -> dict:
    with PYPROJECT.open("rb") as f:
        spec = tomllib.load(f)
    return spec["project"]["optional-dependencies"]


def test_launcher_extra_includes_platformdirs():
    """platformdirs is required for state-dir resolution; bundled in
    [launcher] because every launcher mode (UI, native, tray) uses it.
    Lock the dep so a future drop is caught."""
    extras = _extras()
    assert any("platformdirs" in d for d in extras["launcher"]), (
        "platformdirs must be in [launcher]"
    )


def test_launcher_tray_extra_includes_pywin32_with_windows_marker():
    """pywin32 (Job Object APIs) is Windows-only. Must carry an
    environment marker so pip on Linux/macOS doesn't try to install it."""
    extras = _extras()
    pyw = [d for d in extras["launcher-tray"] if "pywin32" in d]
    assert pyw, "pywin32 must be in [launcher-tray]"
    line = pyw[0]
    assert "sys_platform" in line and "win32" in line, (
        f"pywin32 entry must carry sys_platform marker; got {line!r}"
    )


def test_all_extra_includes_platformdirs():
    """[all] (the meta-extra) keeps the extras-matrix sensible by
    including everything from individual extras."""
    extras = _extras()
    assert any("platformdirs" in d for d in extras["all"])
```

- [ ] **Step 2: Run, verify fail**

```
py -3 -m pytest tests/test_packaging.py -v
```

Expected: 3 FAIL — deps not in pyproject yet.

- [ ] **Step 3: Modify `pyproject.toml`**

```toml
launcher = ["jinja2>=3.1", "psutil>=5.9", "platformdirs>=4.0"]
launcher-native = [
    "jinja2>=3.1", "psutil>=5.9", "platformdirs>=4.0",
    "pywebview>=5.0",
]
launcher-tray = [
    "jinja2>=3.1", "psutil>=5.9", "platformdirs>=4.0",
    "pystray>=0.19", "Pillow>=10",
    "pywin32>=306; sys_platform == 'win32'",
]
```

In the `all` extra (around line 82-101), add `"platformdirs>=4.0"` alongside the other `[launcher]` deps.

- [ ] **Step 4: Run, verify pass**

```
py -3 -m pytest tests/test_packaging.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: Verify install dry-run**

On Windows:
```
py -3 -m pip install -e ".[launcher-tray]" --dry-run
```

Expected: pip resolves pywin32 because the marker matches. On a Linux dev box (skip if you're Windows-only): same command should resolve everything except pywin32 (marker excludes it).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml tests/test_packaging.py
git commit -m "feat(packaging): add platformdirs + pywin32 to launcher extras

[launcher] gains platformdirs (cross-platform state-dir resolution,
used always when launcher is installed). [launcher-tray] gains pywin32
with a sys_platform=='win32' marker so non-Windows installs don't fail
on the Job Object dep that doesn't apply there.

Tracked in issue #8 (extras matrix). Tests pin the deps + marker so a
future drop is caught at CI.

See docs/specs/2026-05-04-native-observability-sidecar-design.md §11.5."
```

---

## Task 11: README updates + new doc files

**Files:**
- Modify: `README.md`
- Create: `deploy/otel/README.md`
- Already created in Task 2: `tools/native-otel/README.md`

Per spec §8: insert the "Native observability (default)" section under Quick Start ▸ Launch and demote existing Docker instructions to an "Advanced — Docker stack" footnote pointing at `deploy/otel/README.md`.

- [ ] **Step 1: Read current README quick-start section**

```
py -3 -c "import sys; sys.stdout.reconfigure(encoding='utf-8'); print(open('README.md', encoding='utf-8').read())" | findstr /n "Quick Start" /c:"docker-compose" /c:"otel"
```

Locate the relevant lines, then plan the patch.

- [ ] **Step 2: Patch README.md**

Under the Quick Start ▸ Launch heading, insert:

```markdown
#### Native observability (default)

First launch prompts to install ~500MB of native binaries (Prometheus,
Tempo, Loki, Grafana, OTel Collector) into `tools/native-otel/`. The
tray manages their lifecycle — quit the tray to stop everything.
Right-click the tray icon → Observability ▸ Status to see per-service
health.

To skip: `set HELIX_OBSERVABILITY=0` before running `Start-helix-tray.bat`.

> **Advanced — Docker stack.** For production-shape deployment, multi-host
> setups, or environments where native binaries don't fit, `docker-compose
> up -d` in `deploy/otel/` runs the same stack containerized. Wire format,
> ports, and dashboard provisioning are identical. See
> [deploy/otel/README.md](deploy/otel/README.md) for details.
```

(The exact insertion point is whatever line currently introduces the OTel/Docker quick-start block — replace the prior "to enable observability, run docker-compose up -d in deploy/otel/" sentence with the block above.)

- [ ] **Step 3: Create `deploy/otel/README.md`**

```markdown
# deploy/otel/ — Docker observability stack (advanced)

The default helix install ships native observability binaries managed
by the tray launcher. This Docker Compose stack is the alternate path —
useful for:

- Production-shape deployment (containerized, declarative)
- Environments where native binaries don't fit (locked-down user dirs,
  multi-host shared observability, etc.)
- Fallback testing against a known-good runtime

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
`docs/specs/2026-05-04-native-observability-sidecar-design.md §6.3`.

## Stop

```bash
docker-compose down
```
```

- [ ] **Step 4: Smoke check**

Verify `deploy/otel/README.md` renders sensibly:

```
py -3 -c "import sys; sys.stdout.reconfigure(encoding='utf-8'); print(open('deploy/otel/README.md', encoding='utf-8').read())"
```

- [ ] **Step 5: Commit**

```bash
git add README.md deploy/otel/README.md
git commit -m "docs: README native-obs section + deploy/otel advanced footnote

Top-level README now leads with the native observability sidecar (the
default install path) and demotes the docker-compose path to an
advanced footnote, matching spec §8. New deploy/otel/README.md
documents the alternate Docker runtime + the shared-source-config
contract with the native sidecar.

See docs/specs/2026-05-04-native-observability-sidecar-design.md §8."
```

---

## Task 12: Issue #8 update (extras-matrix comment)

**Files:** none — GitHub issue comment + the matrix doc the issue points at (if any).

- [ ] **Step 1: Verify the matrix doc location**

```
gh issue view 8 --repo SwiftWing21/helix-context
```

Look for an extras-matrix link inside the issue body — if it points at a doc in the repo (e.g. `docs/EXTRAS.md`), capture the path. If the issue body itself IS the matrix, the comment-only path below is enough.

- [ ] **Step 2: If a matrix doc exists, patch it**

Add two rows to the matrix:

| Extra            | Dep            | Version  | Why                                                       |
| ---------------- | -------------- | -------- | --------------------------------------------------------- |
| `[launcher]`     | `platformdirs` | `>=4.0`  | Cross-platform user-data dir for native observability state. |
| `[launcher-tray]`| `pywin32`      | `>=306`  | Windows Job Object cleanup for tray-managed children. Marker: `sys_platform == 'win32'`. |

Commit with: `git commit -m "docs: extras-matrix — platformdirs + pywin32 for native-obs sidecar"`.

- [ ] **Step 3: Post the comment on issue #8**

```
gh issue comment 8 --repo SwiftWing21/helix-context --body "$(cat <<'EOF'
Updating the extras matrix for the native observability sidecar (PR forthcoming on `feat/native-observability-sidecar`):

| Extra | Dep | Version | Why |
|---|---|---|---|
| `[launcher]` | `platformdirs` | `>=4.0` | Cross-platform user-data dir for native observability state (`%LOCALAPPDATA%\helix-context\observability` on Windows; equivalents on Linux/macOS). |
| `[launcher-tray]` | `pywin32` | `>=306; sys_platform == 'win32'` | Windows Job Object APIs (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) so tray-managed observability children auto-terminate when the launcher exits — clean OR force-killed. Linux/macOS use `start_new_session` (POSIX process group) for the same guarantee, no dep needed. |

Both deps land in `pyproject.toml` as part of the native-observability-sidecar PR. The `pywin32` entry carries a `sys_platform == 'win32'` environment marker so non-Windows installs don't try to install it.

Spec: `docs/specs/2026-05-04-native-observability-sidecar-design.md` §11.5.
EOF
)"
```

> **Plan note:** No code change in this task; it's a docs/process step. Independent of Tasks 13 + 14, so can run any time after Task 10 (which is what determines the matrix entries). Placed here so the comment lands while the PR diff is still in flight.

- [ ] **Step 4: Verify the comment posted**

```
gh issue view 8 --repo SwiftWing21/helix-context --comments | tail -40
```

No commit needed if no doc was edited; otherwise:

```bash
git add docs/EXTRAS.md   # whatever the matrix doc path is
git commit -m "docs: extras-matrix — platformdirs + pywin32"
```

---

## Task 13: Integration test pass (manual, Windows)

**Files:**
- Create: `docs/plans/2026-05-04-native-observability-sidecar-integration-results.md`

Walk through spec §9 integration scenarios on the dev Windows box. Document each result; the file is the artifact.

@superpowers:verification-before-completion

- [ ] **Step 1: Scenario 1 — clean-machine first launch**

```
git stash               # only if you have local edits
Remove-Item -Recurse -Force tools/native-otel/{collector,prometheus,tempo,loki,grafana,configs}
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\helix-context\observability"
.\Start-helix-tray.bat
```

Expected:
- Tray icon appears.
- Balloon notification: "Native observability not installed — ..."
- Right-click tray → Observability submenu shows all "down".
- Run `scripts/install-native-observability.ps1` from a separate terminal.
- After install completes, restart the tray (Quit + relaunch). All five services come up green within ~30s.
- Trigger one helix `/context` request; verify a metric lands at `http://localhost:8889/metrics`.
- Open Grafana at `http://localhost:3000`, helix-overview dashboard renders.

Capture: screenshot of tray menu showing all-green; Grafana panel showing the metric.

- [ ] **Step 2: Scenario 2 — re-launch**

Quit the tray, re-run `Start-helix-tray.bat`. Verify all five services come back up green within 10s and no download happens.

- [ ] **Step 3: Scenario 3 — port-collision**

```
# In separate terminal:
docker run --rm -p 9090:9090 prom/prometheus:v2.54.1
# Or just bind 9090 with: python -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',9090)); s.listen(); input()"
```

Run `Start-helix-tray.bat`. Tray Observability submenu should show Prometheus: external. Other services come up green. Launcher log line: `[prometheus] external instance detected on :9090 — not spawning`.

- [ ] **Step 4: Scenario 4 — per-service failure**

```
# Replace one binary with a script that exits 1 (deterministic-failure mode
# per spec §11.6 — script exits 1 chosen over zero-byte file because
# zero-byte triggers an opaque OS error on Windows that the supervisor
# can't classify cleanly).
Move-Item tools/native-otel/loki/loki.exe tools/native-otel/loki/loki.exe.bak
@'
@echo off
exit /b 1
'@ | Out-File -FilePath tools/native-otel/loki/loki.exe.bat -Encoding ascii
Rename-Item tools/native-otel/loki/loki.exe.bat tools/native-otel/loki/loki.exe -Force
.\Start-helix-tray.bat
```

Tray Observability submenu shows Loki: red. Other services green. helix-context starts normally (verify `/stats` works on :11437).

After verifying, restore: `Move-Item tools/native-otel/loki/loki.exe.bak tools/native-otel/loki/loki.exe -Force`.

- [ ] **Step 5: Scenario 5 — opt-out**

```
$env:HELIX_OBSERVABILITY="0"
.\Start-helix-tray.bat
```

Tray menu has no Observability submenu. No subprocess for any of the five binaries (verify via `Get-Process otelcol-contrib,prometheus,tempo,loki,grafana-server -ErrorAction SilentlyContinue` returning empty).

- [ ] **Step 6: Scenario 6 — Docker compose path still works**

```
cd deploy/otel
docker-compose up -d
docker-compose ps  # all 5 healthy
docker-compose down
cd ../..
```

Verify: helix-overview dashboard at `http://localhost:3000` renders the same panels as in Scenario 1.

- [ ] **Step 7: Document results**

Create `docs/plans/2026-05-04-native-observability-sidecar-integration-results.md` with one section per scenario. Each section: command run, observed output (snippet from the launcher log + tray screenshot if relevant), pass/fail.

```bash
git add docs/plans/2026-05-04-native-observability-sidecar-integration-results.md
git commit -m "docs(plan): integration scenarios passed for native-obs sidecar

All 6 scenarios from spec §9 verified on Windows 11:
  1. Clean-machine first launch — install prompt + green status
  2. Re-launch — no download, all green
  3. Port-collision — external indicator + others green
  4. Per-service failure — red dot, helix-context unaffected
  5. Opt-out — HELIX_OBSERVABILITY=0 → no subprocesses
  6. Docker-compose still works — identical dashboards

See docs/specs/2026-05-04-native-observability-sidecar-design.md §9."
```

---

## Task 14: Bench regression (gate — short GPQA, n=20)

**Files:**
- Create: `docs/plans/2026-05-04-native-observability-sidecar-bench-report.md`

Per spec §9 bench-regression gate: run a short GPQA suite native vs Docker, confirm p95 latency delta ≤ 5s. This is the final gate before merge.

> **Plan note re: time estimate.** Spec §13 lists this as a 30-min check, but realistically the Docker side may need a fresh `docker-compose up -d` + warm-up, two GPQA runs at n=20 take ~10-20 min each, and report writing is another 15-30 min. Budget 90 min total. If the bench is slower than expected, it's acceptable to defer the report-write to a follow-up (commit results JSON only) — the merge gate is the *delta*, not the report polish.

- [ ] **Step 1: Run the native-side bench**

```
.\Start-helix-tray.bat
# Wait for green status across all 5 services
py -3 benchmarks/bench_aa_suite.py --bench gpqa_diamond --n 20 --timeout 240 --out benchmarks/results/native_obs_baseline_2026-05-04_native.json
# Stop the tray (Quit from tray menu)
```

- [ ] **Step 2: Run the Docker-side bench**

```
$env:HELIX_OBSERVABILITY="0"
cd deploy/otel; docker-compose up -d; cd ..\..
# Verify: http://localhost:8889/metrics is reachable
.\Start-helix-tray.bat
py -3 benchmarks/bench_aa_suite.py --bench gpqa_diamond --n 20 --timeout 240 --out benchmarks/results/native_obs_baseline_2026-05-04_docker.json
# Stop tray, then:
cd deploy/otel; docker-compose down; cd ..\..
```

- [ ] **Step 3: Compare**

```
py -3 benchmarks/compare_ab.py benchmarks/results/native_obs_baseline_2026-05-04_docker.json benchmarks/results/native_obs_baseline_2026-05-04_native.json
```

Capture mean and p95 latency for both runs. Compute deltas.

**Pass criteria:** `p95_native - p95_docker <= 5s` AND `accuracy_native >= accuracy_docker` (no regression in correctness).

- [ ] **Step 4: Document results**

Create `docs/plans/2026-05-04-native-observability-sidecar-bench-report.md`:

```markdown
# Native observability bench regression — n=20 GPQA Diamond

| Metric | Docker compose | Native sidecar | Delta | Gate |
| ------ | -------------- | -------------- | ----- | ---- |
| Mean latency  | <fill> | <fill> | <fill> | n/a |
| p95 latency   | <fill> | <fill> | <fill> | <= 5s ✅/❌ |
| Accuracy      | <fill> | <fill> | <fill> | >= 0 ✅/❌ |

## Result

<PASS / FAIL — paste the compare_ab summary>

## Notes

<any warm-up effects, network noise, etc.>

See docs/specs/2026-05-04-native-observability-sidecar-design.md §9 (bench regression gate).
```

- [ ] **Step 5: Commit (only if PASS)**

```bash
git add benchmarks/results/native_obs_baseline_2026-05-04_*.json docs/plans/2026-05-04-native-observability-sidecar-bench-report.md
git commit -m "bench: native-obs vs docker-compose — p95 delta <fill>s (PASS)

n=20 GPQA Diamond run on the dev Windows box. Native sidecar p95
landed within the 5s gate vs the Docker compose baseline. Accuracy
within noise. Confirms the runtime-receiver swap doesn't introduce
latency in helix-context's hot path.

Closes the spec §9 bench-regression gate."
```

If **FAIL** (delta > 5s): do **not** commit a passing claim. Open a follow-up issue capturing the regression + raw JSON. The merge of this PR is gated on this check.

---

## Out of scope (do NOT do in this PR)

Spec §3 + §11 explicitly fence off:

- Cross-platform validation. Linux/macOS hashes stay TODO; the install scripts ship "capable, untested." Follow-up PR.
- Auto-update of native binaries. Re-run install after bumping `.versions`.
- Windows service registration. Separate scope.
- Migration of user-edited Grafana state. Spec §3 confirms dashboards are file-based.
- Loki Windows reliability degrade-path. Open follow-up risk per §11 — if integration scenario 1/2 reveals flakiness, a follow-up issue degrades to opt-in.

---

## Inter-task dependency graph

```
Task 1 ── (deploy/otel/loki-config.yaml exists)
   └─→ Task 5 (render reads loki-config.yaml)

Task 2 ── (.versions schema)
   └─→ Task 3 (bootstrap reads .versions)

Task 4 ── (observability_paths)
   ├─→ Task 5 (render uses paths)
   ├─→ Task 6 (health uses paths indirectly via service-port registry)
   └─→ Task 7 (supervisor uses paths)

Task 5 ── (rendered configs)
   └─→ Task 7 (supervisor refuses without rendered configs — spec §7.1)

Task 6 ── (health primitives)
   └─→ Task 7 (supervisor uses wait_for_port + health endpoints)

Task 7 ── (ObservabilitySupervisor)
   ├─→ Task 8 (tray menu reads supervisor.status())
   └─→ Task 9 (app builds + hands off supervisor)

Task 8 + Task 9 ── (full launcher integration)
   └─→ Task 13 (manual integration scenarios)

Task 10 ── (deps in pyproject)
   ├─→ Task 7 (pywin32 needed at runtime — but module-level import is
   │            guarded so unit tests run without it)
   └─→ Task 13 (`.[launcher-tray]` install pulls deps)

Task 11 ── (docs)        — independent of code
Task 12 ── (issue #8)    — independent of code
Task 14 ── (bench gate)  — depends on Task 13 success
```

Tasks 1, 2, 4, 6 are independently testable from day 1. Task 5 depends on 1+4. Task 7 needs 4+5+6. The ordering above respects all edges and keeps the branch mergeable mid-plan: after each task's commit lands, the previous tasks' tests still pass.

## Total task count + estimated hours

14 tasks. Hours estimate:

| Task | Title                                          | Hours |
| ---- | ---------------------------------------------- | ----- |
| 1    | loki-config.yaml + docker mount                | 1.5   |
| 2    | .versions schema + Windows hashes              | 2.0   |
| 3    | Bootstrap scripts (PS + bash) + helpers        | 4.5   |
| 4    | observability_paths.py                         | 1.5   |
| 5    | observability_render.py + tests                | 4.0   |
| 6    | observability_health.py                        | 2.0   |
| 7    | ObservabilitySupervisor                        | 5.0   |
| 8    | Tray menu integration                          | 3.0   |
| 9    | App-level wiring                               | 2.5   |
| 10   | pyproject.toml deps                            | 1.0   |
| 11   | README + new doc files                         | 1.5   |
| 12   | Issue #8 comment + matrix doc                  | 0.5   |
| 13   | Integration scenarios (manual)                 | 3.0   |
| 14   | Bench regression                               | 1.5   |
|      | **Total**                                      | **33.5** |
