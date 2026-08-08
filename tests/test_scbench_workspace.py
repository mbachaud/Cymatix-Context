"""Safe SCBench workspace manifest and delta tests."""

from __future__ import annotations

from pathlib import Path

from cymatix_context.integrations.scbench.config import SourcePolicy
from cymatix_context.integrations.scbench.workspace import (
    SourceRecord,
    WorkspaceManifest,
    diff_workspace,
    resolve_source_id,
    scan_workspace,
)


def _try_symlink(link: Path, target: Path) -> bool:
    try:
        link.symlink_to(target)
    except OSError:
        return False
    return True


def _record(source_id: str, content: str) -> SourceRecord:
    import hashlib

    raw = content.encode("utf-8")
    return SourceRecord(
        source_id=source_id,
        relative_path=source_id.split("/", 3)[-1],
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        content_type="code",
        content=content,
    )


def _manifest(files: dict[str, str]) -> WorkspaceManifest:
    records = {
        f"repo://p/{path}": _record(f"repo://p/{path}", content)
        for path, content in files.items()
    }
    return WorkspaceManifest(problem="p", root=Path("p"), sources=records)


def test_scan_excludes_eval_secrets_binary_oversize_and_symlink_escape(tmp_path):
    root = tmp_path / "file-backup"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("VALUE = 1", encoding="utf-8")
    (root / "notes.md").write_text("public notes", encoding="utf-8")
    (root / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (root / "evaluation.json").write_text("{}", encoding="utf-8")
    (root / "binary.py").write_bytes(b"safe\x00secret")
    (root / "large.py").write_text("x" * 20, encoding="utf-8")
    (root / "ignored.bin").write_bytes(b"binary")
    outside = tmp_path / "outside.py"
    outside.write_text("LEAK = True", encoding="utf-8")
    linked = _try_symlink(root / "src" / "escape.py", outside)
    policy = SourcePolicy.default().model_copy(update={"max_file_bytes": 16})

    manifest = scan_workspace(root, "file-backup", policy)

    assert tuple(manifest.sources) == (
        "repo://file-backup/notes.md",
        "repo://file-backup/src/app.py",
    )
    if linked:
        assert "repo://file-backup/src/escape.py" not in manifest.sources
    assert manifest.sources["repo://file-backup/src/app.py"].content_type == "code"
    assert manifest.sources["repo://file-backup/notes.md"].content_type == "text"


def test_diff_reports_changed_created_deleted_and_unchanged():
    before = _manifest({"a.py": "one", "gone.py": "old", "same.py": "same"})
    after = _manifest({"a.py": "two", "new.py": "new", "same.py": "same"})

    delta = diff_workspace(before, after)

    assert delta.changed == ("repo://p/a.py",)
    assert delta.created == ("repo://p/new.py",)
    assert delta.deleted == ("repo://p/gone.py",)
    assert delta.unchanged == ("repo://p/same.py",)


def test_resolve_source_id_normalizes_windows_separators_and_blocks_escape(tmp_path):
    root = tmp_path / "p"
    (root / "src").mkdir(parents=True)
    live = root / "src" / "app.py"
    live.write_text("VALUE = 1", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("LEAK = True", encoding="utf-8")
    linked = _try_symlink(root / "src" / "escape.py", outside)

    assert resolve_source_id(root, r"repo://p/src\app.py") == live.resolve()
    assert resolve_source_id(root, "repo://other/src/app.py") is None
    assert resolve_source_id(root, "repo://p/../outside.py") is None
    assert resolve_source_id(root, "repo://p/src") is None
    assert resolve_source_id(root, "file:///hidden/tests.py") is None
    if linked:
        assert resolve_source_id(root, "repo://p/src/escape.py") is None


def test_manifest_hash_is_stable_across_record_insertion_order():
    left = _manifest({"a.py": "one", "b.py": "two"})
    right = _manifest({"b.py": "two", "a.py": "one"})

    assert left.manifest_hash() == right.manifest_hash()
