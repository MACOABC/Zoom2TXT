"""Canal de avance que viaja junto al callback de log.

Las funciones del pipeline reciben un ``log(texto)``. Para informar un avance
real (porcentaje de descarga, bytes subidos, ronda de subtítulos) sin cambiar
ninguna firma, el callback puede exponer además un método ``progress``::

    log = ProgressLog(emit_text, emit_progress)
    log("[*] Descargando…")
    report(log, stage=0, fraction=0.42, detail="120 de 286 MB · 4.1 MB/s")

``report`` no hace nada si el log es un ``print`` o una lista de pruebas.
"""

from __future__ import annotations

from collections.abc import Callable

STAGE_DOWNLOAD = 0
STAGE_UPLOAD = 1
STAGE_CAPTIONS = 2
STAGE_VERIFY = 3


class ProgressLog:
    """Callback de log con canal de avance opcional."""

    def __init__(
        self,
        log: Callable[[str], None],
        progress: Callable[[int, float, str], None] | None = None,
    ):
        self._log = log
        self._progress = progress

    def __call__(self, text: str) -> None:
        self._log(text)

    def progress(self, stage: int, fraction: float | None = None, detail: str = "") -> None:
        if self._progress is not None:
            # -1.0 representa «sin porcentaje» al cruzar señales tipadas de Qt.
            self._progress(int(stage), -1.0 if fraction is None else float(fraction), str(detail))


def report(log, stage: int, fraction: float | None = None, detail: str = "") -> None:
    """Informa avance si el callback lo admite; nunca interrumpe el trabajo."""
    channel = getattr(log, "progress", None)
    if not callable(channel):
        return
    try:
        channel(stage, fraction, detail)
    except Exception:  # noqa: BLE001, S110 — el avance es decorativo
        pass


def human_bytes(value: float) -> str:
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def human_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds > 48 * 3600:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {rest:02d} s" if minutes < 10 else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"
