"""Descarga de grabaciones de Zoom.

Dos caminos, en este orden:

1. **Reproductor nuevo de Zoom («unified player», noviembre de 2026).** La
   página ``/rec/play/…`` ya no incrusta los datos de la grabación: publica una
   configuración ``__zm_unified_player__`` con una API (``/nws/recording/2.0``)
   que entrega el enlace firmado del MP4. La app la consulta con la misma
   sesión y descarga el archivo directamente, con reanudación y avance real.
2. **yt-dlp**, como siempre, para el reproductor clásico, enlaces con
   contraseña y cualquier página que no sea la nueva.

Si ninguno funciona, el error incluye el motivo real en vez de suponer que la
contraseña es incorrecta.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from zoom2txt.processes import run_process
from zoom2txt.progress import STAGE_DOWNLOAD, human_bytes, human_eta, report
from zoom2txt.security import read_private_json, redact
from zoom2txt.storage import file_ok, safe_name

Log = Callable[[str], None]

# En Windows, evita que cada subproceso abra una ventana de consola
# cuando la app corre empaquetada sin consola (PyInstaller --windowed).
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


_PROGRESS_PREFIX = "[z2t]"
_PROGRESS_TEMPLATE = (
    _PROGRESS_PREFIX
    + "|%(progress.downloaded_bytes)s|%(progress.total_bytes)s"
    + "|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s"
)

ZOOM_DOMAINS = ("zoom.us", "zoom.com")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
_PLAYER_CONFIG = re.compile(r"__zm_unified_player__\s*=\s*\{(.*?)\}\s*;", re.S)
_CHUNK = 1024 * 1024
_MAX_RETRIES = 6
_MAX_REFRESH = 3


class ZoomPlayerError(RuntimeError):
    """La descarga directa del reproductor nuevo no pudo completarse."""


class _Interrupted(ConnectionError):
    """La transferencia se cortó antes de recibir el archivo completo."""


def _number(value: str) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def parse_progress(line: str) -> tuple[float | None, str] | None:
    """Convierte una línea de avance de yt-dlp en (fracción 0-1, detalle legible).
    Devuelve None para cualquier otra línea."""
    if not line.startswith(_PROGRESS_PREFIX):
        return None
    parts = line.split("|")
    if len(parts) < 6:
        return None
    done = _number(parts[1])
    total = _number(parts[2]) or _number(parts[3])
    speed = _number(parts[4])
    eta = _number(parts[5])
    if done is None:
        return None
    fraction = min(1.0, done / total) if total else None
    detail = human_bytes(done) + (f" de {human_bytes(total)}" if total else "")
    if speed:
        detail += f" · {human_bytes(speed)}/s"
    remaining = human_eta(eta)
    if remaining:
        detail += f" · faltan {remaining}"
    return fraction, detail


def yt_dlp_path() -> str | None:
    """Localiza yt-dlp: junto al ejecutable empaquetado o en el PATH."""
    exe = "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp"
    if getattr(sys, "frozen", False):
        bundled = Path(sys.executable).parent / exe
        if bundled.exists():
            return str(bundled)
    return shutil.which("yt-dlp")


# ── Reproductor nuevo de Zoom ─────────────────────────────────────
def is_zoom_url(url: str) -> bool:
    """HTTPS hacia un dominio de Zoom, sin credenciales incrustadas."""
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and any(host == domain or host.endswith("." + domain) for domain in ZOOM_DOMAINS)
    )


def _config_value(block: str, name: str) -> str:
    match = re.search(name + r'\s*:\s*"((?:[^"\\]|\\.)*)"', block)
    return match.group(1).replace("\\/", "/") if match else ""


def parse_player_config(html: str) -> dict | None:
    """Extrae ``apiDomain`` y ``playInfoApi`` de la página del reproductor nuevo.
    None si la página es de otro tipo (reproductor clásico, contraseña, error)."""
    match = _PLAYER_CONFIG.search(html or "")
    if not match:
        return None
    block = match.group(1)
    api_domain = _config_value(block, "apiDomain")
    play_info = _config_value(block, "playInfoApi")
    if not api_domain or not play_info:
        return None
    return {"api_domain": api_domain, "play_info": play_info}


def _zoom_session(cookie_text: str = "") -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = _USER_AGENT
    if cookie_text:
        with tempfile.TemporaryDirectory(prefix="zoom2txt-cookies-") as temp:
            plain = Path(temp) / "cookies.txt"
            plain.write_text(cookie_text, encoding="utf-8")
            jar = MozillaCookieJar(str(plain))
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
            except (OSError, ValueError):
                jar = None
        if jar is not None:
            for cookie in jar:
                session.cookies.set_cookie(cookie)
    return session


def resolve_unified_player(session: requests.Session, zoom_url: str) -> dict:
    """Devuelve el enlace firmado del MP4 de una grabación del reproductor nuevo.

    Visitar la página deja en la sesión las cookies que después exige el
    servidor de medios; por eso todo ocurre en la misma ``session``."""
    if not is_zoom_url(zoom_url):
        raise ZoomPlayerError("El enlace no pertenece a Zoom.")
    page = session.get(zoom_url, timeout=(30, 60))
    if page.status_code != 200:
        raise ZoomPlayerError(f"Zoom respondió HTTP {page.status_code} al abrir la grabación.")
    config = parse_player_config(page.text)
    if config is None:
        raise ZoomPlayerError("La página no usa el reproductor nuevo de Zoom.")
    # Un enlace para compartir (/rec/share/…) redirige al reproductor: la
    # referencia válida es la página donde realmente se terminó.
    final_url = getattr(page, "url", "")
    if not isinstance(final_url, str) or not is_zoom_url(final_url):
        final_url = zoom_url
    api_url = urljoin(config["api_domain"], config["play_info"])
    if not is_zoom_url(api_url):
        raise ZoomPlayerError("Zoom indicó un servidor de datos no reconocido.")
    response = session.get(
        api_url,
        headers={"Referer": final_url, "Accept": "application/json, text/plain, */*"},
        timeout=(30, 60),
    )
    if response.status_code != 200:
        raise ZoomPlayerError(f"Zoom respondió HTTP {response.status_code} al pedir la grabación.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ZoomPlayerError("Zoom no devolvió los datos de la grabación.") from exc
    result = payload.get("result") if isinstance(payload, dict) else None
    media = result.get("mediaInfo") if isinstance(result, dict) else None
    mp4_url = str(media.get("playUrl") or "") if isinstance(media, dict) else ""
    if not mp4_url:
        message = payload.get("errorMessage") if isinstance(payload, dict) else ""
        code = payload.get("errorCode") if isinstance(payload, dict) else ""
        raise ZoomPlayerError(
            "Zoom no entregó el video de esta grabación"
            + (f" ({message})" if message else f" (código {code})" if code else "")
            + "."
        )
    if not is_zoom_url(mp4_url):
        raise ZoomPlayerError("Zoom indicó un servidor de video no reconocido.")
    origin = urlparse(final_url)
    return {
        "mp4_url": mp4_url,
        "referer": f"{origin.scheme}://{origin.netloc}/",
        "topic": str(media.get("mediaTopic") or ""),
        "duration": media.get("mediaDuration") or 0,
    }


def _total_size(response, already: int) -> int:
    content_range = response.headers.get("Content-Range", "")
    if "/" in content_range:
        tail = content_range.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    length = response.headers.get("Content-Length", "")
    if str(length).isdigit():
        return int(length) + (already if response.status_code == 206 else 0)
    return 0


def _cancelled(cancel) -> bool:
    return cancel is not None and cancel.is_set()


def _download_direct(zoom_url: str, destino: Path, log: Log, cookie_text: str, cancel) -> Path:
    """Descarga el MP4 del reproductor nuevo con reanudación y reintentos.

    El archivo parcial (``.z2tpart``) se conserva al pausar o si se corta la
    conexión, y se retoma desde el último byte recibido. El enlace firmado se
    renueva si Zoom lo deja de aceptar a mitad de la descarga."""
    session = _zoom_session(cookie_text)
    try:
        info = resolve_unified_player(session, zoom_url)
        part = destino.with_name(destino.name + ".z2tpart")
        retries = refreshes = failure_mark = 0
        started = time.monotonic()
        first_bytes: int | None = None
        last_report = 0.0
        announced = False
        while True:
            if _cancelled(cancel):
                raise InterruptedError("Descarga pausada. Se conserva lo ya descargado.")
            have = part.stat().st_size if part.exists() else 0
            headers = {"Referer": info["referer"], "Accept": "*/*"}
            if have:
                headers["Range"] = f"bytes={have}-"
            try:
                with session.get(
                    info["mp4_url"], headers=headers, stream=True, timeout=(30, 90)
                ) as response:
                    status = response.status_code
                    if status == 416 and have:
                        # Parte guardada incoherente con el archivo actual: empezar de cero.
                        part.unlink(missing_ok=True)
                        continue
                    if status in (401, 403):
                        if refreshes >= _MAX_REFRESH:
                            raise ZoomPlayerError(
                                f"Zoom rechazó la descarga del video (HTTP {status})."
                            )
                        refreshes += 1
                        info = resolve_unified_player(session, zoom_url)
                        continue
                    if status not in (200, 206):
                        raise ZoomPlayerError(f"Zoom respondió HTTP {status} al descargar el video.")
                    content_type = response.headers.get("Content-Type", "").lower()
                    if "video" not in content_type and "octet-stream" not in content_type:
                        raise ZoomPlayerError("Zoom devolvió una página en lugar del video.")
                    if status == 200 and have:
                        have = 0  # el servidor ignoró el rango: se reescribe completo
                    total = _total_size(response, have)
                    if total and have > total:
                        part.unlink(missing_ok=True)
                        continue
                    if not announced:
                        announced = True
                        size = f" ({human_bytes(total)})" if total else ""
                        log(
                            "[i] Zoom usa su reproductor nuevo: descarga directa"
                            + size
                            + (", retomando lo ya descargado." if have else ".")
                        )
                    if first_bytes is None:
                        first_bytes = have
                    with part.open("ab" if have else "wb") as stream:
                        for chunk in response.iter_content(_CHUNK):
                            if _cancelled(cancel):
                                raise InterruptedError(
                                    "Descarga pausada. Se conserva lo ya descargado."
                                )
                            if not chunk:
                                continue
                            stream.write(chunk)
                            have += len(chunk)
                            now = time.monotonic()
                            if now - last_report >= 0.2:
                                last_report = now
                                elapsed = now - started
                                speed = (have - first_bytes) / elapsed if elapsed > 1 else 0
                                detail = human_bytes(have) + (
                                    f" de {human_bytes(total)}" if total else ""
                                )
                                if speed > 0:
                                    detail += f" · {human_bytes(speed)}/s"
                                    remaining = human_eta((total - have) / speed) if total else ""
                                    if remaining:
                                        detail += f" · faltan {remaining}"
                                report(
                                    log, STAGE_DOWNLOAD, (have / total) if total else None, detail
                                )
                    if total and have < total:
                        raise _Interrupted("La transferencia terminó antes de tiempo.")
                break
            except (
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.ChunkedEncodingError,
                _Interrupted,
            ) as exc:
                saved = part.stat().st_size if part.exists() else 0
                if saved - failure_mark >= _CHUNK:
                    retries = 0  # hubo avance real desde el corte anterior
                failure_mark = saved
                retries += 1
                if retries > _MAX_RETRIES:
                    raise ZoomPlayerError(
                        "La conexión con Zoom se cortó varias veces. Se conserva lo descargado; "
                        "vuelve a continuar la clase para retomarla."
                    ) from exc
                log(f"[i] Conexión interrumpida; retomando la descarga ({retries}/{_MAX_RETRIES})…")
                wait = min(30, 2**retries)
                if cancel is not None:
                    if cancel.wait(wait):
                        raise InterruptedError(
                            "Descarga pausada. Se conserva lo ya descargado."
                        ) from exc
                else:
                    time.sleep(wait)
        with part.open("rb") as stream:
            header = stream.read(12)
        if header[4:8] != b"ftyp":
            part.unlink(missing_ok=True)
            raise ZoomPlayerError("El archivo recibido de Zoom no es un video MP4.")
        os.replace(part, destino)
        report(log, STAGE_DOWNLOAD, 1.0, human_bytes(destino.stat().st_size))
        return destino
    finally:
        session.close()


def _last_error(stderr: str) -> str:
    """Última línea de error de yt-dlp, sin enlaces ni identificadores largos."""
    lines = [line.strip() for line in str(stderr or "").splitlines() if line.strip()]
    errors = [line for line in lines if line.upper().startswith("ERROR")]
    text = (errors or lines or [""])[-1]
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[A-Za-z0-9_.\-]{28,}", "…", text)
    text = text.split("; please report")[0]
    return " ".join(text.split())[:220]


def descargar_zoom(
    zoom_url: str,
    password: str,
    output_name: str,
    videos_dir: Path,
    log: Log = print,
    cookies_file: Path | None = None,
    cancel=None,
) -> Path:
    videos_dir.mkdir(parents=True, exist_ok=True)
    destino = videos_dir / f"{safe_name(output_name, 150)}.mp4"

    log(f"[*] Descargando clase de Zoom: {output_name}...")
    report(log, STAGE_DOWNLOAD, None, "Conectando con Zoom…")
    cookie_text = ""
    if cookies_file is not None:
        cookie_text = str(read_private_json(cookies_file)["netscape"])

    # 1) Reproductor nuevo de Zoom: descarga directa.
    direct_reason = ""
    if is_zoom_url(zoom_url):
        try:
            _download_direct(zoom_url, destino, log, cookie_text, cancel)
            if file_ok(destino):
                log("[+] Descarga completada.")
                return destino
            direct_reason = "el archivo descargado quedó vacío"
        except InterruptedError:
            raise
        except Exception as exc:  # noqa: BLE001 — cualquier fallo aquí cede el paso a yt-dlp
            direct_reason = redact(str(exc))[:240]

    # 2) yt-dlp: reproductor clásico, enlaces con contraseña y otros casos.
    ytdlp = yt_dlp_path()
    if not ytdlp:
        raise RuntimeError(
            "No se encontró yt-dlp. Instálalo con «pip install yt-dlp» o "
            "coloca yt-dlp.exe junto al ejecutable de Zoom2TXT."
            + (f" Además, la descarga directa no fue posible: {direct_reason}" if direct_reason else "")
        )
    comando = [ytdlp, zoom_url, "-o", str(destino)]
    # Solo cambia cómo yt-dlp informa el avance; la descarga es la de siempre.
    comando += ["--newline", "--progress-template", "download:" + _PROGRESS_TEMPLATE]

    last_report = [0.0]

    def on_line(line: str) -> None:
        parsed = parse_progress(line)
        if parsed is None:
            return
        now = time.monotonic()
        if now - last_report[0] >= 0.2 or (parsed[0] is not None and parsed[0] >= 1.0):
            last_report[0] = now
            report(log, STAGE_DOWNLOAD, parsed[0], parsed[1])

    # Modo automático (Blackboard): la sesión autoriza vía cookies.
    # Modo manual: enlace público de Zoom con contraseña.
    with tempfile.TemporaryDirectory(prefix=".session-", dir=videos_dir) as temp:
        if cookie_text:
            from zoom2txt.storage import atomic_write

            plain = Path(temp) / "cookies.txt"
            atomic_write(plain, cookie_text)
            comando += ["--cookies", str(plain)]
        if password:
            comando += ["--video-password", password]
        resultado = run_process(comando, cancel=cancel, timeout=6 * 3600, on_line=on_line)
    if resultado.returncode != 0 or not file_ok(destino):
        detail = _last_error(getattr(resultado, "stderr", ""))
        reasons = [reason for reason in (direct_reason, detail) if reason]
        raise RuntimeError(
            "La descarga de Zoom falló. Verifica la URL de la grabación y "
            "la contraseña (puedes cambiarla en Ajustes)."
            + (" Detalle: " + " | ".join(reasons) if reasons else "")
        )
    log("[+] Descarga completada.")
    return destino
