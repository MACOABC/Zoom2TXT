"""Registro de actividad en disco, con datos sensibles ocultos.

Cada línea que aparece en la consola de la app se guarda también en
``<datos>/debug/actividad.log`` para poder diagnosticar un problema después de
cerrar la ventana. No mantiene el archivo abierto (abre, añade y cierra), rota
por tamaño y nunca interrumpe a quien escribe.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from zoom2txt.security import redact

MAX_BYTES = 1_000_000
KEEP = 3
_lock = threading.Lock()


def log_path() -> Path:
    from zoom2txt.config import DEBUG_DIR

    return DEBUG_DIR / "actividad.log"


def _rotate(path: Path) -> None:
    try:
        if path.stat().st_size < MAX_BYTES:
            return
    except OSError:
        return
    for index in range(KEEP - 1, 0, -1):
        older = path.with_name(f"{path.name}.{index}")
        if older.exists():
            older.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(path.name + ".1"))


def write(text: str, source: str = "app") -> None:
    """Añade una línea al registro. Silencioso ante cualquier error de disco."""
    line = redact(str(text)).rstrip()
    if not line:
        return
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # noqa: DTZ005 — hora local legible
    try:
        path = log_path()
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate(path)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(f"{stamp} [{source}] {line}\n")
    except OSError:
        pass


def tail(lines: int = 200) -> str:
    """Últimas líneas del registro actual (para el informe de Estado)."""
    try:
        content = log_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])
