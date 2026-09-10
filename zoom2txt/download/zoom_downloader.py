"""Descarga de grabaciones de Zoom vía yt-dlp.

La lógica de descarga es la original probada (mismo comando yt-dlp,
mismos argumentos). Solo se parametrizó la carpeta de salida y el
print se volvió un callback para poder mostrarlo en la GUI.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from zoom2txt.processes import run_process
from zoom2txt.security import read_private_json
from zoom2txt.storage import file_ok, safe_name

Log = Callable[[str], None]

# En Windows, evita que cada subproceso abra una ventana de consola
# cuando la app corre empaquetada sin consola (PyInstaller --windowed).
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def yt_dlp_path() -> str | None:
    """Localiza yt-dlp: junto al ejecutable empaquetado o en el PATH."""
    exe = "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp"
    if getattr(sys, "frozen", False):
        bundled = Path(sys.executable).parent / exe
        if bundled.exists():
            return str(bundled)
    return shutil.which("yt-dlp")


def descargar_zoom(
    zoom_url: str,
    password: str,
    output_name: str,
    videos_dir: Path,
    log: Log = print,
    cookies_file: Path | None = None,
    cancel=None,
) -> Path:
    ytdlp = yt_dlp_path()
    if not ytdlp:
        raise RuntimeError(
            "No se encontró yt-dlp. Instálalo con «pip install yt-dlp» o "
            "coloca yt-dlp.exe junto al ejecutable de Zoom2TXT."
        )
    videos_dir.mkdir(parents=True, exist_ok=True)
    destino = videos_dir / f"{safe_name(output_name, 150)}.mp4"

    log(f"[*] Descargando clase de Zoom: {output_name}...")
    comando = [ytdlp, zoom_url, "-o", str(destino)]
    # Modo automático (Blackboard): la sesión autoriza vía cookies.
    # Modo manual: enlace público de Zoom con contraseña.
    with tempfile.TemporaryDirectory(prefix=".session-", dir=videos_dir) as temp:
        if cookies_file is not None:
            from zoom2txt.storage import atomic_write

            cookie_text = read_private_json(cookies_file)["netscape"]
            plain = Path(temp) / "cookies.txt"
            atomic_write(plain, cookie_text)
            comando += ["--cookies", str(plain)]
        if password:
            comando += ["--video-password", password]
        resultado = run_process(comando, cancel=cancel, timeout=6 * 3600)
    if resultado.returncode != 0 or not file_ok(destino):
        raise RuntimeError(
            "La descarga de Zoom falló. Verifica la URL de la grabación y "
            "la contraseña (puedes cambiarla en Ajustes)."
        )
    log("[+] Descarga completada.")
    return destino
