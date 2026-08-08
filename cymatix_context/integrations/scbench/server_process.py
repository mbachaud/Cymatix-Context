"""Fresh-genome sidecar lifecycle for deterministic SCBench treatment arms."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from .client import CymatixClient, CymatixProtocolError

log = logging.getLogger("cymatix.scbench.server_process")


class SidecarStartupError(RuntimeError):
    """The benchmark-owned sidecar could not establish a clean identity."""


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class CymatixServerProcess:
    run_dir: Path
    host: str
    port: int
    timeout_s: float = 20.0
    spawn_token: str = field(default_factory=lambda: uuid.uuid4().hex)
    process: subprocess.Popen | None = field(default=None, init=False)
    _stdout_handle: BinaryIO | None = field(default=None, init=False, repr=False)
    _stderr_handle: BinaryIO | None = field(default=None, init=False, repr=False)

    @classmethod
    def for_run(
        cls,
        run_dir: Path,
        *,
        port: int | None = None,
        timeout_s: float = 20.0,
    ) -> "CymatixServerProcess":
        return cls(
            run_dir=run_dir.resolve(),
            host="127.0.0.1",
            port=port if port is not None else _reserve_port(),
            timeout_s=timeout_s,
        )

    @property
    def genome_path(self) -> Path:
        return self.run_dir / "cymatix.genome.db"

    @property
    def config_path(self) -> Path:
        return self.run_dir / "cymatix-scbench.toml"

    @property
    def stdout_path(self) -> Path:
        return self.run_dir / "cymatix-server.stdout.log"

    @property
    def stderr_path(self) -> Path:
        return self.run_dir / "cymatix-server.stderr.log"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def render_config(self) -> str:
        genome_path = self.genome_path.as_posix().replace('"', '\\"')
        return (
            "[ribosome]\n"
            "enabled = false\n"
            "query_expansion_enabled = false\n"
            "query_decomposition_enabled = false\n\n"
            "[genome]\n"
            f'path = "{genome_path}"\n\n'
            "[server]\n"
            f'host = "{self.host}"\n'
            f"port = {self.port}\n\n"
            "[budget]\n"
            "max_genes_per_turn = 8\n"
            "session_delivery_enabled = true\n\n"
            "[ingestion]\n"
            "splade_enabled = false\n"
            "dense_embed_on_ingest = false\n"
            "sema_embed_on_ingest = false\n\n"
            "[retrieval]\n"
            "dense_embedding_enabled = false\n"
        )

    def start(
        self,
        *,
        wait_ready: bool = True,
        deadline_s: float = 30.0,
    ) -> "CymatixServerProcess":
        if self.process is not None and self.process.poll() is None:
            raise SidecarStartupError("sidecar is already running")
        if self.genome_path.exists() and self.genome_path.stat().st_size > 0:
            raise SidecarStartupError(
                f"fresh genome required; refusing existing data: {self.genome_path}"
            )

        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(self.render_config(), encoding="utf-8")
        self._stdout_handle = self.stdout_path.open("wb")
        self._stderr_handle = self.stderr_path.open("wb")
        environment = os.environ.copy()
        environment.update(
            {
                "CYMATIX_CONFIG": str(self.config_path),
                "CYMATIX_GENOME_PATH": str(self.genome_path),
                "CYMATIX_BENCH_SPAWN_TOKEN": self.spawn_token,
            }
        )
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "cymatix_context._asgi:app",
            "--host",
            self.host,
            "--port",
            str(self.port),
        ]
        repository_root = Path(__file__).resolve().parents[3]
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(repository_root),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=self._stdout_handle,
                stderr=self._stderr_handle,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            log.warning("failed to spawn Cymatix benchmark sidecar", exc_info=True)
            self._close_logs()
            raise

        if not wait_ready:
            return self

        try:
            with CymatixClient(
                self.base_url,
                timeout_s=self.timeout_s,
            ) as client:
                health = client.wait_ready(deadline_s=deadline_s)
            if health.get("bench_spawn_token") != self.spawn_token:
                raise SidecarStartupError(
                    "stale responder answered the reserved sidecar port"
                )
            if not isinstance(health.get("pid"), int):
                raise SidecarStartupError("sidecar health response omitted pid")
            if (
                sys.platform != "win32"
                and self.process is not None
                and health["pid"] != self.process.pid
            ):
                raise SidecarStartupError(
                    "stale responder pid does not match spawned sidecar"
                )
        except Exception as exc:
            self.stop()
            if isinstance(exc, SidecarStartupError):
                raise
            if isinstance(exc, CymatixProtocolError):
                raise SidecarStartupError(str(exc)) from exc
            raise
        return self

    def stop(self) -> None:
        process = self.process
        try:
            if process is not None and process.poll() is None:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        creationflags=getattr(
                            subprocess,
                            "CREATE_NO_WINDOW",
                            0,
                        ),
                    )
                else:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except Exception:
                    log.warning("sidecar did not terminate cleanly", exc_info=True)
                    process.kill()
                    process.wait(timeout=5)
        finally:
            self.process = None
            self._close_logs()

    def _close_logs(self) -> None:
        for handle in (self._stdout_handle, self._stderr_handle):
            if handle is not None:
                handle.close()
        self._stdout_handle = None
        self._stderr_handle = None

    def __enter__(self) -> "CymatixServerProcess":
        return self.start()

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()
