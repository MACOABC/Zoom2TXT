"""Download the pinned Deno runtime required by the PyInstaller build."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", default="win-x64", choices=("win-x64",))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    runtime_dir = root / "assets" / "runtime" / args.platform
    manifest = json.loads((runtime_dir / "manifest.json").read_text(encoding="utf-8"))
    executable = runtime_dir / "deno.exe"
    if executable.is_file() and sha256(executable) == manifest["executable_sha256"]:
        print(f"Runtime verificado: {executable.relative_to(root)}")
        return 0
    with tempfile.TemporaryDirectory(prefix="zoom2txt-runtime-") as temporary:
        archive = Path(temporary) / "deno.zip"
        request = urllib.request.Request(
            manifest["source"], headers={"User-Agent": "Zoom2TXT-build/2.6.0"}
        )
        with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        if sha256(archive) != manifest["archive_sha256"]:
            raise RuntimeError(
                "El archivo descargado no coincide con el hash publicado en el proyecto."
            )
        with zipfile.ZipFile(archive) as bundle:
            member = next(
                (name for name in bundle.namelist() if Path(name).name == "deno.exe"), None
            )
            if not member:
                raise RuntimeError("El paquete de Deno no contiene deno.exe.")
            runtime_dir.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, executable.open("wb") as output:
                shutil.copyfileobj(source, output)
    if sha256(executable) != manifest["executable_sha256"]:
        executable.unlink(missing_ok=True)
        raise RuntimeError("El ejecutable extraído no coincide con el hash esperado.")
    print(f"Runtime descargado y verificado: {executable.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
