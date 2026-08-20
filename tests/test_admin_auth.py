"""Opt-in admin bearer token + swap-db allowlist (2026-08-08 audit, Task 13).

Default-inert contract:

- ``[server] admin_token = ""`` (default): no route demands auth — behavior
  is byte-identical to before the knob existed.
- ``[server] admin_token = "secret"``: every ``/admin/*`` route plus
  ``/ingest`` and ``/consolidate`` demands ``Authorization: Bearer secret``
  (401 otherwise); non-admin routes (``/stats``, ``/health``, ...) stay open.
- ``[server] swap_db_roots = []`` (default): ``/admin/swap-db`` opens any path.
- ``[server] swap_db_roots = [root]``: swap targets resolving outside every
  root are rejected 403 before any store is opened.
"""

from __future__ import annotations


from fastapi.routing import APIRoute

from cymatix_context.config import GenomeConfig, ServerConfig
from cymatix_context.knowledge_store import KnowledgeStore

from tests.conftest import (
    make_client,
    make_cymatix_config,
    requires_spacy_model,
)


def _server(**overrides) -> ServerConfig:
    return ServerConfig(upstream="http://localhost:11434", **overrides)


def _make_db(path: str) -> None:
    """Create an empty (schema-only) knowledge store at *path*."""
    KnowledgeStore(path=path).close()


def _admin_paths(app) -> list[tuple[str, str]]:
    """Every (method, path) registered under /admin — the guarded surface."""
    out: list[tuple[str, str]] = []
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/admin"):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                out.append((method, route.path))
    return out


# -- Default config: byte-identical, no auth anywhere ----------------------


class TestDefaultInert:
    def test_admin_routes_open_without_header(self):
        client = make_client()
        assert client.post("/admin/refresh").status_code == 200
        assert client.get("/admin/ribosome/status").status_code == 200

    @requires_spacy_model
    def test_ingest_and_consolidate_open_without_header(self):
        client = make_client()
        resp = client.post(
            "/ingest", json={"content": "admin auth default-inert probe"}
        )
        assert resp.status_code == 200
        # Empty session buffer consolidates to zero facts — still 200.
        assert client.post("/consolidate").status_code == 200


# -- admin_token set: bearer demanded on the admin surface only ------------


class TestBearerToken:
    def _client(self):
        return make_client(
            config=make_cymatix_config(server=_server(admin_token="secret"))
        )

    def test_every_admin_route_401_without_header(self):
        client = self._client()
        paths = _admin_paths(client.app)
        # Sanity: the walk actually found the admin surface (17 routes today).
        assert len(paths) >= 15
        for method, path in paths:
            resp = client.request(method, path)
            assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"

    def test_ingest_and_consolidate_401_without_header(self):
        client = self._client()
        resp = client.post("/ingest", json={"content": "should be rejected"})
        assert resp.status_code == 401
        assert client.post("/consolidate").status_code == 401

    def test_wrong_token_401(self):
        client = self._client()
        resp = client.post(
            "/admin/refresh", headers={"Authorization": "Bearer wrong"}
        )
        assert resp.status_code == 401

    def test_correct_token_admits(self):
        client = self._client()
        headers = {"Authorization": "Bearer secret"}
        assert client.post("/admin/refresh", headers=headers).status_code == 200
        assert (
            client.get("/admin/ribosome/status", headers=headers).status_code == 200
        )

    @requires_spacy_model
    def test_correct_token_admits_ingest(self):
        client = self._client()
        headers = {"Authorization": "Bearer secret"}
        resp = client.post(
            "/ingest", json={"content": "admitted with bearer"}, headers=headers
        )
        assert resp.status_code == 200

    def test_non_admin_routes_stay_open(self):
        """The token guards only /admin/*, /ingest, /consolidate."""
        client = self._client()
        assert client.get("/stats").status_code == 200
        assert client.get("/health").status_code == 200


# -- swap_db_roots allowlist ----------------------------------------------


class TestSwapDbAllowlist:
    def _client(self, tmp_path, roots: list[str]):
        boot_db = str(tmp_path / "boot.db")
        _make_db(boot_db)
        config = make_cymatix_config(
            genome=GenomeConfig(path=boot_db, cold_start_threshold=5),
            server=_server(swap_db_roots=roots),
        )
        return make_client(config=config)

    def test_default_empty_allowlist_unrestricted(self, tmp_path):
        client = self._client(tmp_path, [])
        target_dir = tmp_path / "anywhere"
        target_dir.mkdir()
        target = str(target_dir / "t.db")
        _make_db(target)
        resp = client.post("/admin/swap-db", json={"path": target})
        assert resp.status_code == 200
        assert resp.json()["swapped"] is True

    def test_outside_root_403(self, tmp_path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        target = str(outside / "t.db")
        _make_db(target)
        client = self._client(tmp_path, [str(allowed)])
        boot_path = client.app.state.cymatix.genome.path

        resp = client.post("/admin/swap-db", json={"path": target})
        assert resp.status_code == 403
        assert "swap_db_roots" in resp.json()["error"]
        # The boot store was never swapped out.
        assert client.app.state.cymatix.genome.path == boot_path

    def test_inside_root_allowed(self, tmp_path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        target = str(allowed / "t.db")
        _make_db(target)
        client = self._client(tmp_path, [str(allowed)])
        resp = client.post("/admin/swap-db", json={"path": target})
        assert resp.status_code == 200
        assert resp.json()["swapped"] is True

    def test_prefix_sibling_directory_rejected(self, tmp_path):
        """commonpath semantics: root "allowed" must not admit
        "allowed-evil/t.db" the way a naive startswith would."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        evil = tmp_path / "allowed-evil"
        evil.mkdir()
        target = str(evil / "t.db")
        _make_db(target)
        client = self._client(tmp_path, [str(allowed)])
        resp = client.post("/admin/swap-db", json={"path": target})
        assert resp.status_code == 403

    def test_root_on_other_drive_rejects_without_500(self, tmp_path):
        """A root on a different drive (Windows: os.path.commonpath raises
        ValueError) is simply "no match" — 403, never a 500."""
        outside = tmp_path / "outside"
        outside.mkdir()
        target = str(outside / "t.db")
        _make_db(target)
        client = self._client(tmp_path, ["Q:/allowed"])
        resp = client.post("/admin/swap-db", json={"path": target})
        assert resp.status_code == 403

    def test_missing_path_still_400_inside_root(self, tmp_path):
        """The allowlist gate does not shadow the existing 400 for
        nonexistent paths."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        client = self._client(tmp_path, [str(allowed)])
        resp = client.post(
            "/admin/swap-db", json={"path": str(allowed / "nope.db")}
        )
        assert resp.status_code == 400
