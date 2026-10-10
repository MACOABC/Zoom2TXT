"""YouTube uploads and faithful Studio-style captions, with safe recovery."""

from __future__ import annotations

import json
import re
import tempfile
import threading
from pathlib import Path

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, build_http

from zoom2txt.cleaning.subtitle_cleaner import vtt_a_texto
from zoom2txt.download.zoom_downloader import yt_dlp_path
from zoom2txt.processes import run_process
from zoom2txt.security import read_private_json, write_private_json
from zoom2txt.storage import atomic_write, safe_name
from zoom2txt.transcription.js_runtime import youtube_js_args

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]


class AuthorizationRequired(RuntimeError):
    pass


def token_is_valid(token_file: Path) -> bool:
    try:
        saved = read_private_json(token_file)
        if not isinstance(saved, dict) or not set(SCOPES).issubset(set(saved.get("scopes", []))):
            return False
        if not all(
            isinstance(saved.get(k), str) and saved[k]
            for k in ("refresh_token", "client_id", "client_secret")
        ):
            return False
        if saved.get("token_uri") != "https://oauth2.googleapis.com/token":
            return False
        Credentials.from_authorized_user_info(saved, SCOPES)
        return True
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError):
        return False


def run_oauth_flow(client_secrets_file: Path, token_file: Path):
    from zoom2txt.config import validate_client_secrets

    valid, message = validate_client_secrets(client_secrets_file)
    if not valid:
        raise ValueError(message)
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_file), SCOPES)
    credentials = flow.run_local_server(
        port=0,
        timeout_seconds=180,
        success_message="Autorización recibida. Puedes volver a Zoom2TXT.",
    )
    write_private_json(token_file, json.loads(credentials.to_json()))
    return credentials


def get_authenticated_service(client_secrets_file: Path, token_file: Path):
    if not token_is_valid(token_file):
        raise AuthorizationRequired(
            "Autoriza YouTube desde Ajustes → Asistente. No se abrirá un navegador mientras se procesa una clase."
        )
    credentials = Credentials.from_authorized_user_info(read_private_json(token_file), SCOPES)
    expected = json.loads(client_secrets_file.read_text(encoding="utf-8"))["installed"]["client_id"]
    if credentials.client_id != expected:
        raise AuthorizationRequired(
            "La autorización pertenece a otro proyecto. Conecta YouTube desde Ajustes."
        )
    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            raise AuthorizationRequired(
                "Google rechazó renovar la autorización de YouTube. Puede haber caducado o sido revocada. Ve a Ajustes → Abrir asistente de cuentas → Conectar con YouTube."
            ) from exc
        except (TransportError, OSError) as exc:
            raise AuthorizationRequired(
                "No se pudo conectar con Google para renovar YouTube. Comprueba Internet y vuelve a intentar; no se iniciará ninguna descarga."
            ) from exc
        try:
            write_private_json(token_file, json.loads(credentials.to_json()))
        except OSError as exc:
            raise AuthorizationRequired(
                "Google renovó YouTube, pero no se pudo guardar la sesión en esta PC. Revisa el espacio y los permisos de la carpeta de datos."
            ) from exc
    if not credentials.valid:
        raise AuthorizationRequired(
            "La sesión de YouTube no es válida. Conecta de nuevo desde Ajustes."
        )
    import google_auth_httplib2

    # The official factory excludes HTTP 308 from redirects: for YouTube it
    # means Resume Incomplete, without a required Location header.
    transport = build_http()
    transport.timeout = 60
    http = google_auth_httplib2.AuthorizedHttp(credentials, http=transport)
    return build("youtube", "v3", http=http, cache_discovery=False)


def subir_a_youtube(
    youtube,
    file_name: Path,
    title: str,
    log=print,
    lang="es",
    private=False,
    cancel=None,
    checkpoint=None,
) -> str:
    resuming = bool(checkpoint and checkpoint.get("upload_uri"))
    log(
        "[*] Recuperando la sesión de subida existente; no se crea otro video…"
        if resuming
        else "[*] Preparando la transcripción de la clase…"
    )
    body = {
        "snippet": {
            "title": title[:100],
            "description": "Clase para transcripción personal",
            "categoryId": "27",
            "defaultLanguage": lang,
            "defaultAudioLanguage": lang,
        },
        "status": {
            "privacyStatus": "private" if private else "unlisted",
            "selfDeclaredMadeForKids": False,
        },
    }
    if checkpoint and checkpoint.get("job_key"):
        body["snippet"]["description"] += "\nZoom2TXT: " + checkpoint["job_key"]
    media = MediaFileUpload(str(file_name), chunksize=8 * 1024 * 1024, resumable=True)
    try:
        request = youtube.videos().insert(
            part="snippet,status", body=body, notifySubscribers=False, media_body=media
        )
        return _complete_resumable_upload(request, log, cancel, checkpoint)
    finally:
        # Release Windows' file handle immediately, including on failure/pause.
        media.stream().close()


def _complete_resumable_upload(request, log, cancel, checkpoint):
    if checkpoint and checkpoint.get("upload_uri"):
        request.resumable_uri = checkpoint["upload_uri"]
        # Ask the server for the acknowledged offset before sending more bytes.
        request._in_error_state = True
    transport = request.http
    if checkpoint is not None:
        # Persist the server's session BEFORE the SDK sends the first block.
        # next_chunk() performs both requests internally; saving only after
        # it returns loses the URI when that first block fails.
        class CheckpointTransport:
            def request(self, uri, method="GET", **kwargs):
                resp, content = transport.request(uri, method=method, **kwargs)
                if (
                    uri == request.uri
                    and method == "POST"
                    and resp.status == 200
                    and resp.get("location")
                ):
                    request.resumable_uri = resp["location"]
                    checkpoint["save"](upload_uri=request.resumable_uri)
                return resp, content

        active_transport = CheckpointTransport()
    else:
        active_transport = transport
    response = None
    last_percent = -1
    while response is None:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Subida pausada. Se conserva su estado.")
        try:
            progress, response = request.next_chunk(http=active_transport, num_retries=0)
        except HttpError as exc:
            if exc.resp.status in (404, 410):
                raise RuntimeError(
                    "La sesión de subida caducó. No se creará otra automáticamente. Revisa YouTube Studio y usa Vincular ID si el video existe; si no existe, usa Revisar subida fallida."
                ) from exc
            raise
        finally:
            if checkpoint is not None and request.resumable_uri:
                checkpoint["save"](upload_uri=request.resumable_uri)
        if progress:
            percent = int(progress.progress() * 100)
            if percent // 10 > last_percent // 10:
                log(f"[*] Preparación: {percent}% confirmado.")
                last_percent = percent
    video_id = response["id"]
    if checkpoint is not None:
        checkpoint["save"](video_id=video_id, status="uploaded", upload_uri="")
    log("[+] Clase preparada. La generación del texto continúa en segundo plano.")
    return video_id


def eliminar_video(youtube, video_id: str, log=print) -> None:
    try:
        youtube.videos().delete(id=video_id).execute()
    except HttpError as exc:
        if exc.resp.status != 404:
            raise
    log("[+] Video de YouTube eliminado.")


def validate_vtt(data: bytes) -> bool:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    if not text.startswith("WEBVTT") or not re.search(
        r"\d{2}:\d{2}(?::\d{2})?\.\d{3}\s+-->\s+", text
    ):
        return False
    for block in re.split(r"\r?\n\s*\r?\n", text):
        lines = block.splitlines()
        for n, line in enumerate(lines):
            if "-->" in line and re.sub(r"<[^>]*>", "", " ".join(lines[n + 1 :])).strip():
                return True
    return False


def _quota(store, cost):
    if store is None:
        raise RuntimeError("Falta el registro local de cuota para la consulta privada.")
    from datetime import datetime
    from zoneinfo import ZoneInfo

    day = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    # Local conservative estimate, never represented as Google's actual balance.
    key = "youtube-other-" + day
    if not store.reserve(key, cost, 9000):
        raise RuntimeError(
            "Presupuesto local de consultas API agotado por hoy. La clase queda pendiente sin volver a subirla. Revisa la cuota real en Google Cloud."
        )


def descargar_subtitulos(
    video_id: str,
    output_name: str,
    transcripts_dir: Path,
    sub_lang="es",
    log=print,
    cancel=None,
    youtube=None,
    private=False,
    store=None,
    max_attempts=105,
    interval_seconds=120,
) -> Path | None:
    cancel = cancel or threading.Event()
    if cancel.is_set():
        return None
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    output_name = safe_name(output_name, 150)
    target = transcripts_dir / f"{output_name}_Para_NotebookLM.txt"
    ytdlp = yt_dlp_path() if not private else None
    if not private and not ytdlp:
        raise RuntimeError("No se encontró yt-dlp junto a la aplicación.")
    js_args = youtube_js_args() if not private else []
    if private:
        log(
            "[i] Video privado: se consultarán pistas autorizadas de tu cuenta. Cada consulta API consume cuota; puedes pausar y retomar."
        )
    with tempfile.TemporaryDirectory(prefix=".captions-", dir=transcripts_dir) as temp:
        base = Path(temp) / "captions"
        vtt = Path(f"{base}.{sub_lang}.vtt")
        for attempt in range(max_attempts):
            if cancel.is_set():
                return None
            log(f"[*] Consultando subtítulos ({attempt + 1}/{max_attempts})…")
            data = None
            if private:
                _quota(store, 50)
                tracks = (
                    youtube.captions()
                    .list(part="snippet", videoId=video_id)
                    .execute()
                    .get("items", [])
                )
                for track in tracks:
                    snippet = track.get("snippet", {})
                    if snippet.get("language") == sub_lang and snippet.get("status") == "serving":
                        _quota(store, 200)
                        data = youtube.captions().download(id=track["id"], tfmt="vtt").execute()
                        break
            else:
                vtt.unlink(missing_ok=True)
                result = run_process(
                    [
                        ytdlp,
                        "--ignore-config",
                        *js_args,
                        "--write-auto-sub",
                        "--sub-lang",
                        sub_lang,
                        "--sub-format",
                        "vtt",
                        "--skip-download",
                        "--no-playlist",
                        "-o",
                        str(base),
                        f"https://www.youtube.com/watch?v={video_id}",
                    ],
                    cancel=cancel,
                    timeout=180,
                )
                if result.returncode == 0 and vtt.is_file():
                    data = vtt.read_bytes()
                elif result.returncode:
                    error = result.stderr.lower()
                    if any(
                        message in error
                        for message in (
                            "no supported javascript runtime",
                            "javascript runtime is unavailable",
                            "javascript runtime is unsupported",
                            "challenge solver script distribution",
                        )
                    ):
                        raise RuntimeError(
                            "El motor JavaScript de YouTube no está disponible o no es compatible. "
                            "Reextrae el ZIP actualizado y revisa el antivirus. "
                            "Se conserva el video; no se repetirá su subida."
                        )
                    if "we're processing this video" in error:
                        next_check = (
                            f"Nueva consulta en {interval_seconds} segundos; puedes pausar."
                            if attempt + 1 < max_attempts
                            else "Puedes retomarlo después desde Historial."
                        )
                        log(
                            f"[i] YouTube todavía está procesando el video {video_id}; "
                            f"aún no permite extraer subtítulos. {next_check}"
                        )
                    elif any(
                        message in error
                        for message in (
                            "video has been removed",
                            "video has been deleted",
                            "this video is private",
                            "private video",
                            "video unavailable",
                            "sign in to confirm",
                            "video is not available",
                        )
                    ):
                        raise RuntimeError(
                            f"YouTube no permite consultar el video {video_id}. Revisa su enlace "
                            "y visibilidad en Studio. Si lo borraste, revisa esa clase en Historial; "
                            "no se iniciará otra subida automáticamente."
                        )
                    else:
                        log(
                            f"[!] No se pudieron consultar los subtítulos de {video_id}: "
                            + result.stderr[-500:]
                        )
                else:
                    log(
                        f"[i] El video {video_id} todavía no ofrece subtítulos automáticos en {sub_lang}."
                    )
            if data is not None:
                if not isinstance(data, bytes) or not validate_vtt(data):
                    raise RuntimeError(
                        "El subtítulo recibido está vacío o no es válido. Se conservan el video y el TXT anterior."
                    )
                if cancel.is_set():
                    return None
                texto_bytes = vtt_a_texto(data).encode("utf-8")
                if target.exists() and target.read_bytes() != texto_bytes:
                    backup = target.with_name(target.name + ".previous")
                    atomic_write(backup, target.read_bytes())
                atomic_write(target, texto_bytes)
                log("[+] Transcripción consolidada y guardada: " + target.name)
                return target
            if attempt + 1 < max_attempts and cancel.wait(interval_seconds):
                return None
    log("[i] Subtítulos aún no disponibles. La subida se conserva para continuar después.")
    return None
