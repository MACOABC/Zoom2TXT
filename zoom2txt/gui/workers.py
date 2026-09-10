"""Hilos de trabajo: mantienen la GUI viva mientras corre el pipeline
o el flujo OAuth, comunicándose solo por señales Qt."""

from __future__ import annotations

import threading
from datetime import UTC
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from zoom2txt.blackboard import weeks as wk
from zoom2txt.config import CLIENT_SECRETS_FILE, TOKEN_FILE, Settings
from zoom2txt.core.pipeline import run_pipeline
from zoom2txt.transcription.youtube_engine import AuthorizationRequired
from zoom2txt.workflow import DAILY_LIMIT, DailyLimitReached, WorkLedger


class PipelineWorker(QThread):
    log = Signal(str)
    stage = Signal(int)  # índice del paso que inicia (0-3)
    done = Signal(str, str)  # (ruta del .txt final o "", id del video o "")
    failed = Signal(str)  # mensaje de error legible

    def __init__(
        self,
        zoom_url: str,
        zoom_password: str,
        nombre_clase: str,
        settings: Settings,
        parent=None,
    ):
        super().__init__(parent)
        self._url = zoom_url
        self._password = zoom_password
        self._nombre = nombre_clase
        self._settings = settings
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            txt, video_id = run_pipeline(
                self._url,
                self._password,
                self._nombre,
                self._settings,
                self.log.emit,
                self.stage.emit,
                self.cancel_event,
            )
            self.done.emit(str(txt) if txt else "", video_id or "")
        except Exception as exc:  # noqa: BLE001 — la GUI muestra el mensaje
            self.failed.emit(str(exc))


class DeleteVideoWorker(QThread):
    """Elimina el video de YouTube tras la transcripción, sin congelar la GUI."""

    log = Signal(str)
    finished_ok = Signal(bool)  # True si se eliminó, False si falló

    def __init__(self, video_id: str, parent=None):
        super().__init__(parent)
        self._video_id = video_id

    def run(self) -> None:
        try:
            from zoom2txt.transcription.youtube_engine import (
                eliminar_video,
                get_authenticated_service,
            )

            youtube = get_authenticated_service(CLIENT_SECRETS_FILE, TOKEN_FILE)
            from zoom2txt.storage import StateStore

            state = StateStore()
            affected = [j for j in state.list("job") if j.get("video_id") == self._video_id]
            for job in affected:
                state.put("job", job["id"], cleanup_pending=True)
            eliminar_video(youtube, self._video_id, self.log.emit)
            for job in affected:
                state.put("job", job["id"], cleanup_pending=False, youtube_deleted=True)
            self.finished_ok.emit(True)
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"[✕] No se pudo eliminar el video: {exc}")
            self.finished_ok.emit(False)


class ScanWorker(QThread):
    log = Signal(str)
    needs_login = Signal()
    found = Signal(object)
    capped = Signal(int, int)
    failed = Signal(str)

    def __init__(self, settings, ranges=None, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._ranges = ranges or []
        self.all_pending = []
        self.partial_errors = []
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from zoom2txt.blackboard.client import BlackboardClient
        from zoom2txt.blackboard.registry import Registry, recording_key, short_name_for
        from zoom2txt.config import DATA_DIR

        clients = []
        try:
            client = BlackboardClient(self._settings.blackboard_url, self.log.emit)
            clients.append(client)
            if not client.is_logged_in():
                self.needs_login.emit()
                return
            registry = Registry(self._settings)
            courses = client.get_current_courses()
            ledger = WorkLedger(self._settings)
            ledger.remember_courses(courses)
            self.log.emit(
                f"[+] {len(courses)} cursos del ciclo detectado. Revisa las fechas elegidas."
            )
            items = []
            for course in courses:
                if self.cancel_event.is_set():
                    self.failed.emit("Búsqueda cancelada. No se modificaron tus omisiones.")
                    return
                cid, cname = course["id"], course["name"]
                try:
                    # Each LTI launch changes Zoom's course cookies. A shared
                    # Session would invalidate the earlier courses' headers.
                    client = BlackboardClient(self._settings.blackboard_url, self.log.emit)
                    clients.append(client)
                    content_id = client.find_zoom_content(cid)
                    if not content_id:
                        self.partial_errors.append(f"{cname}: módulo Zoom no disponible")
                        ledger.course_error(cid, cname, "Módulo Zoom no disponible")
                        continue
                    origin, scid, headers = client.open_zoom_portal(cid, content_id)
                    records = client.list_recordings(origin, scid, headers, cid, cname)
                    dated = []
                    observed = []
                    monday = wk.parse_start(self._settings.term_start)
                    for r in records:
                        raw = str(r.extra.get("startTime", ""))
                        try:
                            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                            # Zoom LTI's raw startTime is UTC even without a Z.
                            # Its listStartTime is the localized display string.
                            if stamp.tzinfo is None:
                                stamp = stamp.replace(tzinfo=UTC)
                            stamp = stamp.astimezone(ZoneInfo("America/Lima"))
                            ts = stamp.strftime("%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            self.partial_errors.append(cname + ": grabación sin fecha válida")
                            continue
                        try:
                            duration = float(r.extra.get("duration") or 0)
                        except (TypeError, ValueError):
                            duration = 0
                        r.extra["duration"] = duration
                        key = recording_key(cid, r.meeting_id)
                        week = wk.week_of_str(monday, ts) if monday else None
                        label = f"Semana{week:02d}" if week else "Clase"
                        observed.append(
                            dict(
                                meeting_id=r.meeting_id,
                                start_time=ts,
                                duration=duration,
                                name=f"{short_name_for(cname)}_{label}_{ts[:10]}_{key[:8]}",
                            )
                        )
                        # Without a week filter, still stay inside the configured
                        # academic term; older recordings are not this backlog.
                        if monday and week is None:
                            continue
                        if self._ranges and not any(lo <= ts[:10] <= hi for lo, hi in self._ranges):
                            continue
                        dated.append((r, ts))
                    if len(observed) == len(records):
                        ledger.remember_course(cid, cname, observed)
                    else:
                        ledger.course_error(
                            cid,
                            cname,
                            "Hay grabaciones sin fecha válida; inventario incompleto",
                        )
                    if self._settings.longest_per_day:
                        groups = {}
                        for r, ts in dated:
                            groups.setdefault(ts[:10], []).append((r, ts))
                        selected = []
                        for group in groups.values():
                            group.sort(
                                key=lambda pair: (
                                    -float(pair[0].extra.get("duration") or 0),
                                    recording_key(cid, pair[0].meeting_id),
                                )
                            )
                            selected.append(group[0])
                            if len(group) > 1:
                                self.log.emit(
                                    f"[i] {cname}: se propone la más larga del {group[0][1][:10]}. Puedes desactivar el filtro en Ajustes; ninguna alternativa se omite permanentemente."
                                )
                    else:
                        selected = dated
                    monday = wk.parse_start(self._settings.term_start)
                    for r, ts in sorted(selected, key=lambda pair: pair[1]):
                        if registry.is_processed(cid, r.meeting_id):
                            continue
                        key = recording_key(cid, r.meeting_id)
                        week = wk.week_of_str(monday, ts) if monday else None
                        label = f"Semana{week:02d}" if week else "Clase"
                        name = f"{short_name_for(cname)}_{label}_{ts[:10]}_{key[:8]}"
                        items.append(
                            dict(
                                course_id=cid,
                                course_name=cname,
                                meeting_id=r.meeting_id,
                                start_time=ts,
                                sort_key=ts,
                                name=name,
                                play_url="",
                                job_key=key,
                                _origin=origin,
                                _scid=scid,
                                _headers=headers,
                                _rec=r,
                                _client=client,
                            )
                        )
                except Exception as exc:
                    from zoom2txt.security import redact

                    self.partial_errors.append(cname + ": " + redact(exc))
                    ledger.course_error(cid, cname, redact(exc))
            self.all_pending = [
                {k: it[k] for k in ("course_id", "meeting_id", "name")} for it in items
            ]
            items.sort(key=lambda it: (it["sort_key"], it["job_key"]))
            limit = min(DAILY_LIMIT, self._settings.batch_size)
            daily = ledger.daily()
            available = daily["remaining"]
            resolved = []
            for it in items:
                if len(resolved) >= limit:
                    break
                if self.cancel_event.is_set():
                    self.failed.emit("Búsqueda cancelada.")
                    return
                job = ledger.store.get("job", it["job_key"])
                from zoom2txt.work_items import continuation_data

                try:
                    local = continuation_data(self._settings, it["job_key"])
                except (OSError, ValueError):
                    local = None
                if local is not None:
                    resolved.append({k: v for k, v in it.items() if not k.startswith("_")} | local)
                    self.log.emit(
                        f"[i] {it['course_name']}: trabajo guardado; se comprobará y continuará al procesar."
                    )
                    continue
                ongoing = bool(job.get("video_id")) or it["job_key"] in daily["jobs"]
                if not ongoing and available <= 0:
                    continue
                try:
                    client = it.pop("_client")
                    rec = client.recording_detail(
                        it.pop("_origin"),
                        it.pop("_scid"),
                        it.pop("_headers"),
                        it.pop("_rec"),
                    )
                    it["play_url"] = rec.play_url
                    cookies = (
                        DATA_DIR / "sessions" / registry.store.namespace / (it["job_key"] + ".json")
                    )
                    client.export_cookies(cookies)
                    it["cookies_file"] = str(cookies)
                    resolved.append(it)
                    if not ongoing:
                        available -= 1
                    size = rec.extra.get("selected_video_bytes")
                    if size:
                        self.log.emit(
                            f"[+] {it['course_name']}: video MP4 elegido ({size / (1024 * 1024):.0f} MB), audio separado descartado."
                        )
                except Exception as exc:
                    self.partial_errors.append(it["course_name"] + ": " + str(exc))
            for issue in self.partial_errors:
                self.log.emit("[!] Revisión parcial: " + issue)
            if len(items) > len(resolved):
                self.capped.emit(len(resolved), len(items))
            if not resolved and self.partial_errors:
                raise RuntimeError(
                    "No se pudo completar la revisión. Consulta los detalles; no significa que tengas todas las clases."
                )
            self.found.emit(resolved)
        except Exception as exc:
            from zoom2txt.security import redact

            self.failed.emit(redact(exc))
        finally:
            for instance in clients:
                session = getattr(instance, "session", None)
                if session is not None:
                    session.close()


class QueueWorker(QThread):
    """Procesa en serie una lista de grabaciones con el pipeline
    completo; opcionalmente elimina cada video de YouTube al terminar."""

    log = Signal(str)
    stage = Signal(int)
    item_started = Signal(int, str)  # (índice, nombre)
    item_finished = Signal(int, str)  # (índice, ruta txt o "")
    all_done = Signal(int, int)  # (ok, fallidas)

    def __init__(
        self,
        items: list[dict],
        zoom_password: str,
        settings: Settings,
        delete_after: bool,
        parent=None,
    ):
        super().__init__(parent)
        self._items = items
        self._password = zoom_password
        self._settings = settings
        self._delete_after = delete_after
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        from zoom2txt.blackboard.registry import Registry

        registry = Registry(self._settings)
        seen = set()
        ok = failed = 0
        for i, item in enumerate(self._items):
            if self.cancel_event.is_set():
                break
            identity = (item["course_id"], item["meeting_id"])
            if identity in seen or registry.is_processed(*identity):
                continue
            seen.add(identity)
            self.item_started.emit(i, item["name"])
            self.log.emit("")
            self.log.emit(f"══ [{i + 1}/{len(self._items)}] {item['name']} ══")
            try:
                from zoom2txt.blackboard.registry import recording_key
                from zoom2txt.storage import StateStore

                job_key = item.get("job_key") or recording_key(*identity)
                StateStore(self._settings).put(
                    "job", job_key, course_id=identity[0], meeting_id=identity[1]
                )
                cookies = item.get("cookies_file")
                txt, video_id = run_pipeline(
                    item.get("play_url", ""),
                    "",
                    item["name"],
                    self._settings,
                    self.log.emit,
                    self.stage.emit,
                    self.cancel_event,
                    Path(cookies) if cookies else None,
                    job_key=job_key,
                    auto_recover=True,
                )
                if txt:
                    registry.mark_done(
                        item["course_id"], item["meeting_id"], item["name"], Path(txt)
                    )
                    ok += 1
                    from zoom2txt.blackboard.registry import recording_key
                    from zoom2txt.storage import StateStore

                    state = StateStore(self._settings)
                    key = item.get("job_key") or recording_key(*identity)
                    job = state.get("job", key)
                    if self._delete_after and video_id and job.get("status") == "done":
                        state.put("job", key, cleanup_pending=True)
                        if self._delete(video_id):
                            state.put("job", key, cleanup_pending=False, youtube_deleted=True)
                else:
                    failed += 1
                self.item_finished.emit(i, str(txt) if txt else "")
            except DailyLimitReached as exc:
                self.log.emit("[i] " + str(exc))
                self.item_finished.emit(i, "")
                continue
            except AuthorizationRequired as exc:
                failed += 1
                self.log.emit(f"[✕] {item['name']}: {exc}")
                self.item_finished.emit(i, "")
                self.log.emit(
                    "[!] Tanda detenida por autorización de YouTube. Las demás clases siguen pendientes; no se descargarán hasta resolverla."
                )
                break
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self.log.emit(f"[✕] {item['name']}: {exc}")
                self.item_finished.emit(i, "")
        self.all_done.emit(ok, failed)

    def _delete(self, video_id: str) -> bool:
        try:
            from zoom2txt.transcription.youtube_engine import (
                eliminar_video,
                get_authenticated_service,
            )

            youtube = get_authenticated_service(CLIENT_SECRETS_FILE, TOKEN_FILE)
            eliminar_video(youtube, video_id, self.log.emit)
            return True
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"[✕] Limpieza pendiente del video {video_id}: {exc}")
            return False


class MaterialScanWorker(QThread):
    log = Signal(str)
    needs_login = Signal()
    found = Signal(object)
    failed = Signal(str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        from zoom2txt.blackboard.client import BlackboardClient
        from zoom2txt.blackboard.registry import short_name_for
        from zoom2txt.security import redact
        from zoom2txt.storage import StateStore, file_ok

        client = None
        try:
            client = BlackboardClient(self._settings.blackboard_url, self.log.emit)
            if not client.is_logged_in():
                self.needs_login.emit()
                return
            state = StateStore(self._settings)
            rows, missing = [], []
            for course in client.get_current_courses():
                if self.cancel_event.is_set():
                    raise InterruptedError(
                        "Revisión cancelada; no se confirmó que el material esté completo."
                    )
                cid, cname = course["id"], course["name"]
                try:
                    before = len(client.coverage)
                    issues_before = len(getattr(client, "material_issues", []))
                    files = client.list_course_materials(cid, cname)
                    folder = (
                        self._settings.materials_dir
                        / state.namespace
                        / (short_name_for(cname) + "_" + cid.strip("_"))
                    )
                    have = 0
                    for mf in files:
                        dest = folder / Path(mf.rel_path)
                        if len(str(dest.resolve())) > 235:
                            from zoom2txt.storage import safe_name

                            dest = folder / safe_name(Path(mf.rel_path).stem, 60)
                            dest = dest.with_suffix(Path(mf.rel_path).suffix)
                            self.log.emit("[i] Ruta profunda abreviada: " + mf.filename)
                        if not dest.resolve().is_relative_to(folder.resolve()):
                            raise ValueError("Ruta de material fuera de la carpeta permitida.")
                        key = f"{cid}:{mf.content_id}:{mf.attachment_id}"
                        record = state.get("material", key)
                        if (
                            record
                            and record.get("version") == mf.version
                            and file_ok(dest, record.get("checksum", ""))
                        ):
                            have += 1
                        else:
                            missing.append(dict(mf=mf, dest=dest, key=key))
                    unsupported = len(client.coverage) - before
                    issues = getattr(client, "material_issues", [])[issues_before:]
                    for issue in issues:
                        self.log.emit("[!] " + cname + ": " + issue)
                    note = (
                        f"{unsupported} recursos no exportables como adjuntos"
                        if unsupported
                        else "Archivos revisados"
                    )
                    if issues:
                        note = (
                            f"Revisión parcial: {len(issues)} incidencias. {len(files)} archivos identificados disponibles. "
                            + note
                        )
                    rows.append(
                        dict(
                            course=cname,
                            total=len(files),
                            have=have,
                            missing=len(files) - have,
                            note=note,
                            error=bool(issues),
                        )
                    )
                except Exception as exc:
                    rows.append(
                        dict(
                            course=cname,
                            total=0,
                            have=0,
                            missing=0,
                            note=redact(exc),
                            error=True,
                        )
                    )
                    self.log.emit("[!] Revisión incompleta de " + cname + ": " + redact(exc))
            self.found.emit((rows, missing))
        except Exception as exc:
            self.failed.emit(redact(exc))
        finally:
            session = getattr(client, "session", None)
            if session is not None:
                session.close()


class MaterialDownloadWorker(QThread):
    log = Signal(str)
    progress = Signal(int, int)
    finished_all = Signal(int, int)

    def __init__(self, settings, missing, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._missing = missing
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        from zoom2txt.blackboard.client import BlackboardClient
        from zoom2txt.security import redact
        from zoom2txt.storage import StateStore, digest

        ok = failed = 0
        try:
            client = BlackboardClient(self._settings.blackboard_url, self.log.emit)
            state = StateStore(self._settings)
            for index, item in enumerate(self._missing, start=1):
                if self.cancel_event.is_set():
                    break
                mf, dest = item["mf"], item["dest"]
                try:
                    size = client.download_attachment(mf, dest, cancel=self.cancel_event)
                    key = item.get("key") or f"{mf.course_id}:{mf.content_id}:{mf.attachment_id}"
                    state.put(
                        "material",
                        key,
                        path=str(dest),
                        version=mf.version,
                        size=size,
                        checksum=digest(dest),
                    )
                    ok += 1
                    self.log.emit("[+] Material verificado: " + mf.filename)
                except InterruptedError:
                    break
                except Exception as exc:
                    failed += 1
                    self.log.emit("[!] " + mf.filename + ": " + redact(exc))
                self.progress.emit(index, len(self._missing))
        except Exception as exc:
            failed += 1
            self.log.emit("[!] " + redact(exc))
        self.finished_all.emit(ok, failed)


class RecoveryWorker(QThread):
    log = Signal(str)
    stage = Signal(int)
    result = Signal(str)

    def __init__(self, settings, job=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.job = job
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        from zoom2txt.config import DATA_DIR
        from zoom2txt.core.pipeline import retry_pending_copies
        from zoom2txt.security import read_private_json, redact
        from zoom2txt.storage import StateStore

        try:
            if self.job is None:
                retry_pending_copies(self.settings, self.log.emit, self.cancel_event)
                self.result.emit(
                    "Revisión de copias terminada. Consulta el historial para ver pendientes."
                )
                return
            state = StateStore(self.settings)
            key = self.job["id"]
            self.log.emit("")
            self.log.emit(
                "[i] Recuperando clase: "
                + self.job.get("display_name", self.job.get("name", "Clase"))
            )
            data = read_private_json(DATA_DIR / "jobs" / state.namespace / (key + ".json"))
            cookies = DATA_DIR / "sessions" / state.namespace / (key + ".json")
            password = ""
            if not cookies.exists() and not self.job.get("video_id"):
                from zoom2txt.auth.credentials import get_zoom_password

                password = get_zoom_password(self.settings.zoom_user) or ""
            txt, vid = run_pipeline(
                data["zoom_url"],
                password,
                self.job.get("display_name", self.job["name"]),
                self.settings,
                self.log.emit,
                self.stage.emit,
                self.cancel_event,
                cookies if cookies.exists() else None,
                job_key=key,
            )
            if txt and self.job.get("course_id"):
                from zoom2txt.blackboard.registry import Registry

                Registry(self.settings).mark_done(
                    self.job["course_id"],
                    self.job["meeting_id"],
                    self.job["display_name"],
                    txt,
                )
            if (
                txt
                and self.job.get("cleanup_pending")
                and state.get("job", key).get("status") == "done"
            ):
                from zoom2txt.transcription.youtube_engine import (
                    eliminar_video,
                    get_authenticated_service,
                )

                eliminar_video(
                    get_authenticated_service(CLIENT_SECRETS_FILE, TOKEN_FILE),
                    vid,
                    self.log.emit,
                )
                state.put("job", key, cleanup_pending=False, youtube_deleted=True)
            self.result.emit(
                "Transcripción recuperada." if txt else "Trabajo pendiente; la subida se conserva."
            )
        except Exception as exc:
            name = (
                self.job.get("display_name", self.job.get("name", "Clase"))
                if self.job
                else "Copias pendientes"
            )
            self.result.emit("No se pudo continuar: " + name + ". " + redact(exc))


class OAuthWorker(QThread):
    """Corre el flujo OAuth (abre el navegador) sin congelar el wizard."""

    succeeded = Signal()
    failed = Signal(str)

    def __init__(
        self,
        client_secrets: Path = CLIENT_SECRETS_FILE,
        token: Path = TOKEN_FILE,
        parent=None,
    ):
        super().__init__(parent)
        self._client_secrets = client_secrets
        self._token = token

    def run(self) -> None:
        try:
            from zoom2txt.transcription.youtube_engine import run_oauth_flow

            run_oauth_flow(self._client_secrets, self._token)
            self.succeeded.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
