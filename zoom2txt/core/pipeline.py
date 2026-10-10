"""Recoverable desktop pipeline with faithful consolidated captions."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from zoom2txt.config import CLIENT_SECRETS_FILE, DATA_DIR, TOKEN_FILE, Settings
from zoom2txt.download.zoom_downloader import descargar_zoom
from zoom2txt.security import read_private_json, redact, write_private_json
from zoom2txt.storage import StateStore, digest, file_ok, safe_name
from zoom2txt.transcription.youtube_engine import (
    descargar_subtitulos,
    eliminar_video,
    get_authenticated_service,
    subir_a_youtube,
)
from zoom2txt.transcription.youtube_recovery import (
    find_existing_upload,
    owned_channel,
    video_details,
)

Log = Callable[[str], None]
Stage = Callable[[int], None]
STAGES = [
    "Descarga de Zoom",
    "Preparando transcripción",
    "Procesando transcripción",
    "Verificando texto y limpieza",
]


def run_pipeline(
    zoom_url: str,
    zoom_password: str,
    nombre_clase: str,
    settings: Settings,
    log: Log,
    on_stage: Stage,
    cancel: threading.Event,
    cookies_file=None,
    job_key=None,
    auto_recover=False,
    _restarted=False,
    defer_captions=False,
    caption_attempts=105,
):
    store = StateStore(settings)
    key = job_key or hashlib.sha256(zoom_url.encode()).hexdigest()[:24]
    row = store.get("job", key)
    name = row.get("name") or safe_name(nombre_clase, 105) + (
        "" if nombre_clase.endswith(key[:8]) else "_" + key[:8]
    )
    private_state = DATA_DIR / "jobs" / store.namespace / (key + ".json")
    private = read_private_json(private_state) if private_state.exists() else {}
    private["zoom_url"] = zoom_url
    write_private_json(private_state, private)
    store.put(
        "job",
        key,
        name=name,
        display_name=nombre_clase,
        status=row.get("status", "pending"),
    )
    txt = Path(row["txt"]) if row.get("txt") else None
    video_id = row.get("video_id")
    mp4 = Path(row["video"]) if row.get("video") else None
    youtube = None
    try:
        if not (txt and file_ok(txt, row.get("txt_hash", ""))):
            if auto_recover and video_id:
                youtube = get_authenticated_service(CLIENT_SECRETS_FILE, TOKEN_FILE)
                saved_video = video_details(youtube, video_id)
                if saved_video is not None:
                    current_channel = owned_channel(youtube)
                    owner = saved_video.get("snippet", {}).get("channelId")
                    if (
                        row.get("youtube_channel_id")
                        and row["youtube_channel_id"] != current_channel
                    ) or (owner and owner != current_channel):
                        raise RuntimeError(
                            "El video pertenece a otro canal. Conecta su cuenta en Ajustes y continúa la tanda."
                        )
                if saved_video is None:
                    log(
                        "[*] El ID guardado no está disponible. Revisando el canal antes de recuperar esta clase…"
                    )
                    video_id = _reconcile_retry(youtube, row, key, nombre_clase, store, cancel, log)
                    store.put(
                        "job",
                        key,
                        video_id=video_id or "",
                        status="uploaded" if video_id else "downloaded",
                    )
                    private["upload_uri"] = ""
                    write_private_json(private_state, private)
            if (
                (
                    row.get("status") == "upload_unknown"
                    or (row.get("status") == "uploading" and not private.get("upload_uri"))
                )
                and not video_id
                and not private.get("upload_uri")
            ):
                log(
                    "[*] Comprobando si esta clase ya existe en tu canal; no se inicia otra subida…"
                )
                youtube = get_authenticated_service(CLIENT_SECRETS_FILE, TOKEN_FILE)
                if auto_recover:
                    video_id = _reconcile_retry(youtube, row, key, nombre_clase, store, cancel, log)
                else:
                    video_id = find_existing_upload(
                        youtube,
                        row.get("youtube_title") or row.get("display_name") or nombre_clase,
                        key,
                        cancel,
                    )
                if not video_id and not auto_recover:
                    raise RuntimeError(
                        "La subida anterior quedó incierta y no se encontró una coincidencia segura en tu canal. Si borraste ESTA clase de YouTube, selecciona su fila en Historial → Trabajos → Revisar subida fallida para autorizar otro intento. Si el video todavía existe, usa Vincular ID. No se subirá otra copia automáticamente."
                    )
                store.put(
                    "job",
                    key,
                    video_id=video_id or "",
                    status="uploaded" if video_id else "downloaded",
                    error="",
                )
                if video_id:
                    log(
                        f"[+] Video existente identificado: https://youtu.be/{video_id}. Se omite la subida."
                    )
            if not video_id:
                from zoom2txt.workflow import DailyLimitReached, WorkLedger

                if cancel.is_set():
                    return None, None
                ledger = WorkLedger(settings, store)
                budget = ledger.daily()
                if (
                    not private.get("upload_uri")
                    and key not in budget["jobs"]
                    and not budget["remaining"]
                ):
                    next_day = ledger.daily()["next_day"]
                    store.put("job", key, status="daily_limit", deferred_until=next_day)
                    raise DailyLimitReached(
                        f"Alcanzaste las 6 clases nuevas de hoy. Esta clase queda pendiente para {next_day}, a partir de las 00:00 de Lima. Puedes continuar videos ya subidos sin iniciar otra subida."
                    )
                # Validate/renew YouTube before spending a daily slot or
                # downloading hundreds of MB. Shared auth errors stop QueueWorker.
                log("[*] Comprobando autorización de YouTube antes de continuar…")
                youtube = youtube or get_authenticated_service(CLIENT_SECRETS_FILE, TOKEN_FILE)
                if auto_recover:
                    channel = owned_channel(youtube)
                    if row.get("youtube_channel_id") and row["youtube_channel_id"] != channel:
                        raise RuntimeError(
                            "La cuenta conectada corresponde a otro canal. Conecta la cuenta original en Ajustes y continúa esta tanda."
                        )
                    store.put("job", key, youtube_channel_id=channel)
                if not private.get("upload_uri") and not ledger.admit(key):
                    raise DailyLimitReached(
                        "Otra tarea ocupó la última plaza de hoy. Esta clase sigue pendiente."
                    )
                on_stage(0)
                if private.get("upload_uri") and not (
                    mp4 and file_ok(mp4, row.get("video_hash", ""))
                ):
                    raise RuntimeError(
                        "No está el archivo íntegro de la subida interrumpida. Restáuralo antes de continuar; no se enviará otro archivo a esa sesión."
                    )
                if not (mp4 and file_ok(mp4, row.get("video_hash", ""))):
                    if not zoom_url:
                        raise RuntimeError(
                            "Falta el video local y su enlace guardado. Pulsa Buscar clases nuevas para recuperar el acceso a Zoom."
                        )
                    store.put("job", key, status="downloading")
                    workspace = settings.videos_dir / store.namespace / key
                    mp4 = descargar_zoom(
                        zoom_url,
                        zoom_password,
                        name,
                        workspace,
                        log,
                        cookies_file,
                        cancel=cancel,
                    )
                    store.put(
                        "job",
                        key,
                        status="downloaded",
                        video=str(mp4),
                        video_hash=digest(mp4),
                    )
                else:
                    log("[+] Video local verificado. Se reutiliza la descarga guardada.")
                if cancel.is_set():
                    return None, None
                on_stage(1)
                store.put("job", key, status="uploading", youtube_title=nombre_clase[:100])
                if not private.get("upload_uri"):
                    store.put(
                        "job",
                        key,
                        requested_private=settings.youtube_private,
                        upload_attempt_at=time.time(),
                    )

                def checkpoint(**values):
                    upload_uri = values.pop("upload_uri", None)
                    # Commit the ID first. A crash must never leave neither an
                    # ID nor the resumable session after a successful upload.
                    if values:
                        store.put("job", key, **values)
                    if upload_uri is not None:
                        private["upload_uri"] = upload_uri
                        write_private_json(private_state, private)

                try:
                    video_id = subir_a_youtube(
                        youtube,
                        mp4,
                        nombre_clase,
                        log,
                        settings.sub_lang,
                        private=settings.youtube_private,
                        cancel=cancel,
                        checkpoint={
                            "upload_uri": private.get("upload_uri"),
                            "save": checkpoint,
                            "job_key": key,
                        },
                    )
                except BaseException as exc:
                    store.put(
                        "job",
                        key,
                        status="uploading" if private.get("upload_uri") else "upload_unknown",
                    )
                    from googleapiclient.errors import HttpError

                    cause = exc.__cause__
                    if (
                        auto_recover
                        and not _restarted
                        and private.get("upload_uri")
                        and isinstance(cause, HttpError)
                        and cause.resp.status in (404, 410)
                    ):
                        log("[*] La sesión de subida caducó. Revisando el canal para continuar…")
                        latest = store.get("job", key)
                        recovered_id = _reconcile_retry(
                            youtube, latest, key, nombre_clase, store, cancel, log
                        )
                        store.put(
                            "job",
                            key,
                            video_id=recovered_id or "",
                            status="uploaded" if recovered_id else "downloaded",
                        )
                        private["upload_uri"] = ""
                        write_private_json(private_state, private)
                        return run_pipeline(
                            zoom_url,
                            zoom_password,
                            nombre_clase,
                            settings,
                            log,
                            on_stage,
                            cancel,
                            cookies_file,
                            job_key=key,
                            auto_recover=True,
                            _restarted=True,
                            defer_captions=defer_captions,
                            caption_attempts=caption_attempts,
                        )
                    raise
                store.put("job", key, status="uploaded", video_id=video_id)
            if cancel.is_set():
                return None, video_id
            if defer_captions:
                store.put("job", key, status="waiting_captions", error="")
                log("[i] Clase preparada. Se continúa con las demás mientras se genera el texto.")
                return None, video_id
            on_stage(2)
            youtube = youtube or get_authenticated_service(CLIENT_SECRETS_FILE, TOKEN_FILE)
            video = (
                youtube.videos()
                .list(part="status,processingDetails", id=video_id)
                .execute()
                .get("items", [])
            )
            if not video:
                raise RuntimeError(
                    "El video subido ya no está disponible en esta cuenta. Revisa el historial antes de volver a subir."
                )
            is_private = video[0].get("status", {}).get("privacyStatus") == "private"
            status = video[0].get("status", {})
            processing = video[0].get("processingDetails", {}).get("processingStatus")
            if status.get("uploadStatus") in (
                "failed",
                "rejected",
                "deleted",
            ) or processing in ("failed", "terminated"):
                raise RuntimeError(
                    "YouTube informa que este video falló o fue rechazado. No se repetirá la subida: revisa su estado en YouTube Studio."
                )
            log(
                "[i] Video ya identificado en YouTube: se continúa con sus subtítulos, sin subir otra copia."
            )
            if processing == "processing" or status.get("uploadStatus") == "uploaded":
                log(
                    "[i] YouTube aún está procesando el video. Se esperarán los subtítulos; que aparezca en Studio no garantiza que estén listos."
                )
            if is_private and not settings.youtube_private:
                log(
                    "[!] YouTube informa visibilidad Privado, aunque ahora elegiste No listado. Puede ser una sesión anterior privada o una restricción del proyecto API. No se cambia la privacidad de videos existentes automáticamente; se usarán consultas autorizadas de subtítulos."
                )
            store.put("job", key, status="waiting_captions", private=is_private)
            txt = descargar_subtitulos(
                video_id,
                name,
                settings.transcripts_dir / store.namespace,
                settings.sub_lang,
                log,
                cancel,
                youtube=youtube,
                private=is_private,
                store=store,
                max_attempts=caption_attempts,
            )
            if txt is None:
                return None, video_id
            store.put(
                "job",
                key,
                status="cleanup_pending",
                txt=str(txt),
                txt_hash=digest(txt),
                cleanup_pending=bool(video_id),
            )
        on_stage(3)
        if not file_ok(txt, store.get("job", key).get("txt_hash", "")):
            raise ValueError("No hay un TXT íntegro verificado. No se eliminará el video remoto.")
        if txt and row.get("txt"):
            log(
                "[+] TXT existente verificado. Se completa el registro sin descargar ni subir otra vez."
            )
        # A verified local TXT is enough to remove the remote recording.
        # An unavailable optional copy must never extend remote retention.
        if video_id and not store.get("job", key).get("youtube_deleted"):
            store.put("job", key, status="cleanup_pending", cleanup_pending=True)
            try:
                youtube = youtube or get_authenticated_service(CLIENT_SECRETS_FILE, TOKEN_FILE)
                eliminar_video(youtube, video_id, log)
            except Exception as exc:
                store.put("job", key, error=redact(str(exc)))
                log(
                    "[!] Texto guardado; eliminación remota pendiente. Se reintentará automáticamente."
                )
                return txt, video_id
            store.put("job", key, cleanup_pending=False, youtube_deleted=True, error="")
        synced = _post_proceso(txt, mp4, settings, log)
        store.put(
            "job",
            key,
            status="done" if synced else "sync_pending",
            sync_local=synced,
            error="",
        )
        return txt, video_id
    except BaseException as exc:
        store.put("job", key, error=redact(str(exc)))
        raise


def _reconcile_retry(youtube, row, key, title, store, cancel, log):
    """Automatic retry authorized from Clases, after complete channel inspection."""
    channel = owned_channel(youtube)
    if row.get("youtube_channel_id") and channel != row["youtube_channel_id"]:
        raise RuntimeError(
            "La cuenta conectada corresponde a otro canal. Conecta la cuenta original en Ajustes y continúa esta tanda."
        )
    title = row.get("youtube_title") or row.get("display_name") or title
    found = find_existing_upload(youtube, title, key, cancel, max_pages=100, strict=True)
    if cancel.is_set():
        raise InterruptedError("Recuperación pausada; se conserva el trabajo.")
    if found:
        return found
    if row.get("upload_attempt_at", 0) > time.time() - 15 * 60:
        raise RuntimeError(
            "El intento es reciente y YouTube aún puede estar registrándolo. Vuelve a continuar esta tanda en 15 minutos; no se duplicará ahora."
        )
    if not (len(key) == 24 and title.endswith("_" + key[:8])):
        raise RuntimeError(
            "El nombre antiguo no permite descartar una copia renombrada. Esta clase requiere vincular su video; las demás pueden continuar."
        )
    store.put("job", key, youtube_channel_id=channel, reconciled_absent_at=time.time())
    log(
        "[i] Revisión completa: la clase no aparece en el canal conectado. Se recuperará desde el video local si está íntegro."
    )
    return None


def _post_proceso(txt: Path, archivo_mp4: Path | None, settings: Settings, log: Log) -> bool:
    if not file_ok(txt):
        raise ValueError("No hay un TXT válido. No se eliminará ningún video.")
    copied = not settings.drive_dir
    if settings.drive_dir:
        try:
            drive = Path(settings.drive_dir)
            if not drive.is_dir():
                raise OSError(
                    "La carpeta de copia no está disponible. Conecta la unidad o corrige la ruta."
                )
            destination = drive / txt.name
            if destination.resolve() != txt.resolve():
                fd, name = tempfile.mkstemp(prefix=".zoom2txt-", dir=drive)
                os.close(fd)
                temporary = Path(name)
                try:
                    shutil.copy2(txt, temporary)
                    if digest(temporary) != digest(txt):
                        raise OSError("La copia no coincide con la transcripción.")
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            copied = True
            log(
                "[+] Copia local verificada en la carpeta elegida. La subida a la nube depende de su cliente de sincronización."
            )
        except OSError as exc:
            log("[!] Copia pendiente: " + redact(exc) + ". Se conservan TXT y video.")
    if (
        settings.delete_local_video
        and copied
        and archivo_mp4
        and archivo_mp4.is_file()
        and archivo_mp4.resolve().is_relative_to(settings.videos_dir.resolve())
    ):
        archivo_mp4.unlink()
        log("[+] Video local eliminado tras verificar el TXT y la copia local solicitada.")
    return copied


def retry_pending_copies(settings, log, cancel):
    store = StateStore(settings)
    for row in store.list("job"):
        if cancel.is_set():
            break
        if row.get("status") != "sync_pending":
            continue
        txt = Path(row.get("txt", ""))
        if not file_ok(txt, row.get("txt_hash", "")):
            log("[!] Falta la transcripción local de " + row.get("display_name", "clase"))
            continue
        video = Path(row["video"]) if row.get("video") else None
        if _post_proceso(txt, video, settings, log):
            store.put("job", row["id"], status="done", sync_local=True, error="")


def retry_pending_cleanup(settings, log, cancel):
    """Retry only app-owned jobs with a verified transcript and pending deletion."""
    store = StateStore(settings)
    for row in store.list("job"):
        if cancel.is_set():
            break
        if not row.get("cleanup_pending") or row.get("youtube_deleted"):
            continue
        txt = Path(row.get("txt", ""))
        if not file_ok(txt, row.get("txt_hash", "")):
            log("[!] No se puede completar la limpieza: falta el TXT íntegro de esta clase.")
            continue
        try:
            # Reuse the normal completion path: it never reuploads a verified TXT.
            result, _ = run_pipeline(
                "",
                "",
                row.get("display_name", row.get("name", "Clase")),
                settings,
                log,
                lambda _: None,
                cancel,
                job_key=row["id"],
            )
            if (
                result
                and not store.get("job", row["id"]).get("cleanup_pending")
                and row.get("course_id")
            ):
                from zoom2txt.blackboard.registry import Registry

                Registry(settings).mark_done(
                    row["course_id"],
                    row["meeting_id"],
                    row.get("display_name", row.get("name", "Clase")),
                    result,
                )
        except Exception as exc:
            log("[!] Limpieza pendiente: " + redact(exc))
