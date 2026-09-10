"""Explicit, portable JS runtime for yt-dlp; never install on the user's PC."""

import shutil
import sys
from pathlib import Path


def deno_path() -> Path | None:
    name = "deno.exe" if sys.platform == "win32" else "deno"
    if getattr(sys, "frozen", False):
        # PyInstaller extracts this private copy with the application. Do not
        # depend on a developer's PATH, CWD or globally installed runtime.
        root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidate = root / "runtime" / name
        return candidate if candidate.is_file() else None
    project = Path(__file__).resolve().parents[2]
    platform = "win-x64" if sys.platform == "win32" else "linux-x64"
    candidate = project / "assets" / "runtime" / platform / name
    if candidate.is_file():
        return candidate
    external = shutil.which("deno")
    return Path(external) if external else None


def youtube_js_args() -> list[str]:
    runtime = deno_path()
    if runtime is None:
        raise RuntimeError(
            "Falta el motor JavaScript para consultar YouTube. Vuelve a extraer el "
            "ZIP completo de Zoom2TXT o revisa si el antivirus bloqueó Deno. "
            "No se volverá a subir el video."
        )
    return ["--no-js-runtimes", "--js-runtimes", f"deno:{runtime}"]
