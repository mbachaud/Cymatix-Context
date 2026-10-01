"""Build the bundled Python engine for the desktop app.

    pip install -e ".[launcher]" pyinstaller
    python desktop/engine/build_engine.py

Produces desktop/dist-engine/ (PyInstaller one-folder build: the
``cymatix-engine`` executable plus its runtime), which electron-builder ships
as resources/engine. One-folder rather than one-file: a one-file build
unpacks itself to a temp dir on every launch, which is slow and trips some
antivirus heuristics.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DESKTOP = HERE.parent
REPO = DESKTOP.parent
WORK = HERE / "build"
OUT = DESKTOP / "dist-engine"

HIDDEN_IMPORTS = [
    # uvicorn picks its loop/protocol implementations by name at runtime.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]


def main() -> int:
    if shutil.which("pyinstaller") is None and not _module_available("PyInstaller"):
        print("PyInstaller is not installed: pip install pyinstaller", file=sys.stderr)
        return 1
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir",
        "--name", "cymatix-engine",
        "--distpath", str(WORK / "dist"),
        "--workpath", str(WORK / "work"),
        "--specpath", str(WORK),
        "--paths", str(REPO),
        # Launcher templates, static files and icons are package data.
        "--collect-data", "cymatix_context",
        "--collect-submodules", "cymatix_context",
    ]
    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]
    cmd.append(str(HERE / "entry.py"))
    print(" ".join(cmd))
    result = subprocess.run(
        cmd, cwd=str(REPO),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        return result.returncode
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(WORK / "dist" / "cymatix-engine", OUT)
    print(f"engine bundle: {OUT}")
    return 0


def _module_available(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


if __name__ == "__main__":
    sys.exit(main())
