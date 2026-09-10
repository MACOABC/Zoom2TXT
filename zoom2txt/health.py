"""Preflight and health checks that never expose user credentials."""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from zoom2txt.config import BB_COOKIES_FILE, Settings, credentials_status
from zoom2txt.download.zoom_downloader import yt_dlp_path
from zoom2txt.processes import run_process
from zoom2txt.security import redact
from zoom2txt.storage import StateStore
from zoom2txt.transcription.js_runtime import deno_path


@dataclass(frozen=True)
class HealthResult:
    key: str
    title: str
    status: str  # ok | warning | error
    summary: str
    detail: str = ""

    def public(self) -> dict:
        value = asdict(self)
        home = str(Path.home())
        value["summary"] = redact(value["summary"]).replace(home, "%USERPROFILE%")
        value["detail"] = redact(value["detail"]).replace(home, "%USERPROFILE%")
        return value


def _directory_check(key: str, title: str, path: Path) -> HealthResult:
    path = Path(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise OSError("la ruta no es una carpeta")
        with tempfile.NamedTemporaryFile(prefix=".zoom2txt-check-", dir=path):
            pass
        return HealthResult(key, title, "ok", "Disponible y con permiso de escritura")
    except OSError as exc:
        return HealthResult(
            key,
            title,
            "error",
            "No se puede escribir en esta carpeta",
            f"{path}: {exc}",
        )


def quick_health(settings: Settings) -> list[HealthResult]:
    """Fast, local-only checks safe to run before starting a pipeline."""

    results: list[HealthResult] = []
    try:
        missing = credentials_status(settings)
        if missing:
            labels = {
                "zoom": "cuenta o contraseña de Zoom",
                "client_secrets": "credencial de escritorio de Google",
                "token": "autorización de YouTube",
            }
            detail = ", ".join(labels.get(item, item) for item in missing)
            results.append(
                HealthResult(
                    "accounts",
                    "Cuentas",
                    "error",
                    "La configuración de cuentas está incompleta",
                    detail,
                )
            )
        else:
            results.append(
                HealthResult("accounts", "Cuentas", "ok", "Credenciales locales disponibles")
            )
    except Exception as exc:  # noqa: BLE001 — keyring backends vary by OS
        results.append(
            HealthResult(
                "accounts",
                "Cuentas",
                "error",
                "No se pudo abrir el almacén seguro de credenciales",
                str(exc),
            )
        )

    results.extend(
        (
            _directory_check("videos", "Videos temporales", settings.videos_dir),
            _directory_check("transcripts", "Transcripciones", settings.transcripts_dir),
            _directory_check("materials", "Material", settings.materials_dir),
        )
    )

    try:
        free = shutil.disk_usage(settings.videos_dir).free
        free_gb = free / (1024**3)
        if free_gb < 2:
            status = "error"
            summary = f"Solo quedan {free_gb:.1f} GB libres"
            detail = "Libera al menos 2 GB antes de descargar otra clase."
        elif free_gb < 10:
            status = "warning"
            summary = f"Quedan {free_gb:.1f} GB libres"
            detail = "Procesa tandas pequeñas o activa el borrado local verificado."
        else:
            status = "ok"
            summary = f"{free_gb:.1f} GB libres para trabajar"
            detail = ""
        results.append(HealthResult("disk", "Espacio en disco", status, summary, detail))
    except OSError as exc:
        results.append(
            HealthResult("disk", "Espacio en disco", "error", "No se pudo medir", str(exc))
        )

    extractor = yt_dlp_path()
    runtime = deno_path()
    missing_engine = []
    if not extractor or not Path(extractor).is_file():
        missing_engine.append("yt-dlp")
    if not runtime or not Path(runtime).is_file():
        missing_engine.append("Deno")
    results.append(
        HealthResult(
            "engines",
            "Motores de procesamiento",
            "error" if missing_engine else "ok",
            "Falta " + " y ".join(missing_engine)
            if missing_engine
            else "yt-dlp y Deno están disponibles",
            "Vuelve a extraer los tres archivos del ZIP." if missing_engine else "",
        )
    )

    try:
        StateStore(settings).list("job")
        results.append(HealthResult("database", "Historial local", "ok", "Base de datos accesible"))
    except Exception as exc:  # noqa: BLE001 — SQLite/storage boundary
        results.append(
            HealthResult(
                "database",
                "Historial local",
                "error",
                "La base de datos necesita revisión",
                str(exc),
            )
        )

    if settings.drive_dir:
        results.append(_directory_check("drive", "Carpeta de copia", Path(settings.drive_dir)))
    else:
        results.append(
            HealthResult(
                "drive",
                "Segunda copia",
                "warning",
                "No configurada",
                "Es opcional; el TXT principal seguirá guardándose localmente.",
            )
        )
    return results


def _version_date(value: str) -> date | None:
    match = re.fullmatch(r"(\d{4})\.(\d{1,2})\.(\d{1,2})(?:\..*)?", value.strip())
    try:
        return date(*map(int, match.groups())) if match else None
    except ValueError:
        return None


def full_health(settings: Settings, cancelled=lambda: False) -> list[HealthResult]:
    """Detailed checks. Network/subprocess failures degrade to clear statuses."""

    results = quick_health(settings)
    by_key = {result.key: result for result in results}
    if cancelled():
        return results

    extractor = yt_dlp_path()
    runtime = deno_path()
    if extractor and Path(extractor).is_file() and runtime and Path(runtime).is_file():
        try:
            ytdlp = run_process([extractor, "--version"], timeout=20)
            deno = run_process([str(runtime), "--version"], timeout=20)
            if ytdlp.returncode or deno.returncode:
                raise RuntimeError("uno de los motores no respondió correctamente")
            yt_version = ytdlp.stdout.decode("utf-8", "replace").strip().splitlines()[0]
            deno_version = deno.stdout.decode("utf-8", "replace").strip().splitlines()[0]
            released = _version_date(yt_version)
            age = (datetime.now(UTC).date() - released).days if released else None
            stale = age is not None and age > 120
            by_key["engines"] = HealthResult(
                "engines",
                "Motores de procesamiento",
                "warning" if stale else "ok",
                f"yt-dlp {yt_version} · {deno_version}",
                "yt-dlp tiene más de 120 días; instala un paquete oficial actualizado."
                if stale
                else "Versiones ejecutables verificadas localmente.",
            )
        except Exception as exc:  # noqa: BLE001 — subprocess/runtime boundary
            by_key["engines"] = HealthResult(
                "engines",
                "Motores de procesamiento",
                "error",
                "Los archivos existen, pero no se pueden ejecutar",
                str(exc),
            )

    if cancelled():
        return list(by_key.values())
    try:
        from zoom2txt.blackboard.client import BlackboardClient

        if not BB_COOKIES_FILE.is_file():
            by_key["blackboard"] = HealthResult(
                "blackboard",
                "Blackboard",
                "warning",
                "Todavía no hay una sesión guardada",
                "La app abrirá el inicio institucional cuando busques.",
            )
        elif BlackboardClient(settings.blackboard_url, lambda _: None).is_logged_in():
            by_key["blackboard"] = HealthResult(
                "blackboard", "Blackboard", "ok", "Sesión institucional activa"
            )
        else:
            by_key["blackboard"] = HealthResult(
                "blackboard",
                "Blackboard",
                "warning",
                "La sesión venció o Blackboard no respondió",
                "Pulsa Buscar para iniciar sesión de nuevo.",
            )
    except Exception as exc:  # noqa: BLE001 — Blackboard client boundary
        by_key["blackboard"] = HealthResult(
            "blackboard",
            "Blackboard",
            "warning",
            "No se pudo comprobar ahora",
            str(exc),
        )

    if cancelled():
        return list(by_key.values())
    try:
        from zoom2txt.config import CLIENT_SECRETS_FILE, TOKEN_FILE
        from zoom2txt.transcription.youtube_engine import get_authenticated_service

        get_authenticated_service(CLIENT_SECRETS_FILE, TOKEN_FILE)
        by_key["youtube"] = HealthResult(
            "youtube", "YouTube", "ok", "Autorización disponible para la app"
        )
    except Exception as exc:  # noqa: BLE001 — OAuth backend boundary
        by_key["youtube"] = HealthResult(
            "youtube",
            "YouTube",
            "error",
            "La autorización necesita atención",
            str(exc),
        )

    order = [
        "accounts",
        "blackboard",
        "youtube",
        "engines",
        "videos",
        "transcripts",
        "materials",
        "disk",
        "database",
        "drive",
    ]
    return [by_key[key] for key in order if key in by_key]


def pipeline_blockers(settings: Settings) -> list[HealthResult]:
    required = {"accounts", "videos", "transcripts", "disk", "engines", "database"}
    return [
        result
        for result in quick_health(settings)
        if result.key in required and result.status == "error"
    ]
