"""Cliente de Blackboard: usa la sesión del usuario (cookies del login
en la ventana integrada) para leer cursos vía REST y las grabaciones
de Zoom vía el lanzamiento LTI, igual que lo haría el navegador.

Como el portal de Zoom puede variar entre instituciones, las
respuestas se procesan de forma defensiva y, ante una forma
inesperada, se guarda un volcado en DATA_DIR/debug para diagnóstico.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from zoom2txt.config import BB_COOKIES_FILE, DEBUG_DIR
from zoom2txt.security import read_private_json, write_private_json
from zoom2txt.storage import atomic_write, safe_name

Log = Callable[[str], None]

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

ZOOM_PLACEMENT_HANDLERS = (
    "resource/x-bb-bltiplacement-Zoom",
    "resource/x-bb-blti-Zoom",
)


class _FormParser(HTMLParser):
    """Extrae el primer <form> (action + inputs) de un HTML de
    auto-envío LTI."""

    def __init__(self):
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._in_form = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and self.action is None:
            self.action = a.get("action", "")
            self._in_form = True
        elif tag == "input" and self._in_form and a.get("name"):
            self.fields[a["name"]] = a.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self._in_form = False


@dataclass
class MaterialFile:
    """Un archivo adjunto del contenido de un curso."""

    course_id: str
    course_name: str
    content_id: str
    attachment_id: str
    filename: str
    rel_path: str  # ruta relativa dentro de la carpeta del curso
    version: str = ""
    size: int | None = None
    download_url: str = field(default="", repr=False)


# Tipos de contenido de Blackboard que llevan archivos adjuntos
ATTACH_HANDLERS = (
    "resource/x-bb-file",
    "resource/x-bb-document",
    "resource/x-bb-assignment",
)


def _safe_segment(name: str) -> str:
    """Nombre de archivo/carpeta válido en Windows y Linux."""
    return safe_name(name, 80)


@dataclass
class Recording:
    course_id: str
    course_name: str
    meeting_id: str
    topic: str
    start_time: str  # texto legible, tal como lo da el portal
    play_url: str = ""
    passcode: str = ""
    extra: dict = field(default_factory=dict)


class BlackboardError(RuntimeError):
    """Error con mensaje pensado para mostrarse al usuario."""


class BlackboardClient:
    def __init__(self, base_url: str, log: Log = print):
        self.base = base_url.rstrip("/")
        if urlparse(self.base).scheme != "https" or not urlparse(self.base).hostname:
            raise BlackboardError("Blackboard requiere una dirección HTTPS válida.")
        self.log = log
        self.coverage = []
        self.material_issues = []
        self._course_modes = {}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _UA, "Accept-Language": "es-PE,es;q=0.9"})
        self._load_cookies()

    # ── Sesión / cookies ──────────────────────────────────────────
    def _load_cookies(self) -> None:
        try:
            raw = read_private_json(BB_COOKIES_FILE)
            if isinstance(raw, dict):
                if raw.get("base") != self.base:
                    return
                raw = raw.get("cookies", [])
            if not isinstance(raw, list):
                return
            for c in raw:
                if not isinstance(c, dict):
                    continue
                self.session.cookies.set(
                    c["name"],
                    c["value"],
                    domain=c.get("domain", ""),
                    path=c.get("path", "/"),
                    secure=c.get("secure", True),
                    expires=c.get("expires"),
                    rest={"HttpOnly": c.get("httponly", True)},
                )
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            pass

    def store_cookies(self, cookies: list[dict]) -> None:
        """Guarda las cookies capturadas por la ventana de login."""
        BB_COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_private_json(BB_COOKIES_FILE, {"base": self.base, "cookies": cookies})
        self.session.cookies.clear()
        self._load_cookies()

    def is_logged_in(self) -> bool:
        try:
            r = self.session.get(
                f"{self.base}/learn/api/public/v1/users/me",
                timeout=25,
                allow_redirects=False,
            )
            return r.status_code == 200 and isinstance(r.json(), dict) and bool(r.json().get("id"))
        except (requests.RequestException, ValueError):
            return False

    # ── Cursos ────────────────────────────────────────────────────
    def get_current_courses(self) -> list[dict]:
        """Cursos del ciclo actual: [{'id', 'name'}]. Se queda con el
        término (termId) más reciente entre las matrículas activas."""
        r = self.session.get(
            f"{self.base}/learn/api/public/v1/users/me/courses",
            params={"expand": "course"},
            timeout=30,
        )
        if r.status_code != 200:
            raise BlackboardError(
                f"Blackboard respondió {r.status_code} al pedir tus cursos. "
                "Vuelve a iniciar sesión."
            )
        data = r.json()
        memberships = list(data.get("results", []))
        seen = set()
        while (data.get("paging") or {}).get("nextPage"):
            link = urljoin(self.base, data["paging"]["nextPage"])
            if link in seen or urlparse(link).netloc != urlparse(self.base).netloc:
                raise BlackboardError(
                    "La paginación de cursos es inconsistente; la búsqueda está incompleta."
                )
            seen.add(link)
            page = self.session.get(link, timeout=30)
            if page.status_code != 200:
                raise BlackboardError(
                    "No se pudieron consultar todos los cursos. Vuelve a intentar."
                )
            data = page.json()
            memberships.extend(data.get("results", []))
        rows = []
        for m in memberships:
            course = m.get("course") or {}
            self._course_modes[course.get("id", m.get("courseId", ""))] = course.get("ultraStatus")
            if m.get("availability", {}).get("available") != "Yes":
                continue
            if not course.get("termId"):
                continue  # cursos sueltos/viejos sin ciclo
            rows.append(
                {
                    "id": course.get("id", m.get("courseId", "")),
                    "name": course.get("name", "(sin nombre)"),
                    "termId": course["termId"],
                    "created": course.get("created", ""),
                }
            )
        if not rows:
            return []
        # término del curso creado más recientemente = ciclo actual
        latest_term = max(rows, key=lambda x: x["created"])["termId"]
        return [
            {"id": r_["id"], "name": r_["name"], "termId": r_["termId"]}
            for r_ in rows
            if r_["termId"] == latest_term
        ]

    # ── Zoom LTI ──────────────────────────────────────────────────
    def find_zoom_content(self, course_id: str) -> str | None:
        """Busca el elemento «Zoom Meeting» del curso; devuelve su
        contentId o None si el curso no tiene Zoom."""
        for item in self._all_contents(course_id):
            handler = (item.get("contentHandler") or {}).get("id", "")
            if handler in ZOOM_PLACEMENT_HANDLERS or (
                "bltiplacement" in handler.lower() and "zoom" in handler.lower()
            ):
                return item.get("id")
        return None

    def _placement_launch_link(self, course_id: str, content_id: str) -> str | None:
        """Obtiene el launchLink real del placement de Zoom vía la API
        privada de Ultra (trae blti_placement_id, que el launchLink
        genérico no conoce)."""
        r = self.session.get(
            f"{self.base}/learn/api/v1/courses/{course_id}/contents/{content_id}",
            headers={"Accept": "application/json"},
            timeout=30,
        )
        if r.status_code != 200:
            return None
        try:
            detail = r.json().get("contentDetail", {})
        except ValueError:
            return None
        for handler, info in detail.items():
            if "zoom" in handler.lower() or "blti" in handler.lower():
                link = info.get("launchLink") or (info.get("placement") or {}).get("launchLink")
                if link:
                    return link
        return None

    def open_zoom_portal(self, course_id: str, content_id: str) -> tuple[str, str, dict]:
        """Lanza el LTI de Zoom con la sesión actual (como el clic del
        usuario) y devuelve (origen_del_portal, scid, headers_extra).

        El portal entrega el identificador de sesión (scid) y unas
        cabeceras x-zm-* / X-XSRF-TOKEN incrustadas en el JS de la
        página; se necesitan para las llamadas a la API de grabaciones.
        """
        launch = self._placement_launch_link(course_id, content_id)
        if not launch:
            self._dump("lti_launch", f"course={course_id} content={content_id}")
            raise BlackboardError(
                "No se encontró el enlace de lanzamiento del módulo de Zoom "
                "de este curso. Se guardó un volcado de diagnóstico."
            )

        launch_url = urljoin(self.base, launch)
        self._validate_lti_url(launch_url)
        r = self.session.get(launch_url, timeout=30)
        parser = _FormParser()
        parser.feed(r.text)
        if not parser.action or not parser.fields:
            self._dump("lti_launch", r.text)
            raise BlackboardError(
                "No se pudo lanzar el módulo de Zoom del curso (Blackboard "
                "no devolvió el formulario LTI). Se guardó un volcado de "
                "diagnóstico."
            )
        action = urljoin(r.url, parser.action)
        self._validate_lti_url(action)
        r2 = self.session.post(action, data=parser.fields, timeout=30, allow_redirects=False)
        for _ in range(8):
            if r2.status_code not in (301, 302, 303, 307, 308):
                break
            destination = urljoin(r2.url, r2.headers.get("Location", ""))
            self._validate_lti_url(destination)
            # Never forward signed form bodies to a different host on 307/308.
            if r2.status_code in (307, 308):
                if urlparse(destination).netloc != urlparse(action).netloc:
                    raise BlackboardError(
                        "Zoom solicitó una redirección de credenciales a otro servidor. Se detuvo por seguridad."
                    )
                r2 = self.session.post(
                    destination, data=parser.fields, timeout=30, allow_redirects=False
                )
            else:
                r2 = self.session.get(destination, timeout=30, allow_redirects=False)
        self._validate_lti_url(r2.url)
        if r2.status_code != 200:
            raise BlackboardError(
                f"El acceso a Zoom no se completó (HTTP {r2.status_code}). Inicia sesión de nuevo."
            )

        m = re.search(r'scid\s*:\s*"([0-9a-f]+)"', r2.text)
        if not m:
            self._dump("zoom_portal", f"URL: {r2.url}\n\n{r2.text}")
            raise BlackboardError(
                "Se llegó al portal de Zoom pero no se encontró el "
                "identificador de sesión (scid). Se guardó un volcado de "
                "diagnóstico."
            )
        scid = m.group(1)
        headers = {"Accept": "application/json"}
        for key, value in re.findall(r'\{key:\s*"([^"]+)",\s*value:\s*"([^"]*)"\}', r2.text):
            if key.lower().startswith("x-") or key.lower() == "x-xsrf-token":
                headers[key] = value
        origin = f"{urlparse(r2.url).scheme}://{urlparse(r2.url).netloc}"
        return origin, scid, headers

    def _validate_lti_url(self, url):
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        allowed = host == urlparse(self.base).hostname or any(
            host == domain or host.endswith("." + domain) for domain in ("zoom.us", "zoom.com")
        )
        if parsed.scheme != "https" or not allowed or parsed.username or parsed.password:
            raise BlackboardError(
                "El módulo de Zoom apunta a un servidor no reconocido. Revisa la integración de tu institución."
            )

    def list_recordings(
        self, origin: str, scid: str, headers: dict, course_id: str, course_name: str
    ) -> list[Recording]:
        """Lee la pestaña «Grabaciones en la nube» del portal LTI
        (paginando hasta traerlas todas)."""
        out: list[Recording] = []
        page = 1
        seen = set()
        while True:
            r = self.session.get(
                f"{origin}/api/v1/lti/rich/recording/COURSE",
                params={
                    "startTime": "",
                    "endTime": "",
                    "keyWord": "",
                    "searchType": "1",
                    "status": "",
                    "page": str(page),
                    "total": "0",
                    "lti_scid": scid,
                },
                headers=headers,
                timeout=30,
            )
            if r.status_code != 200:
                raise BlackboardError(
                    f"No se pudo listar las grabaciones (HTTP {r.status_code}). Renueva la búsqueda; no se considera completo este curso."
                )
            try:
                payload = r.json()
            except ValueError as exc:
                self._dump("recordings_list", r.text)
                raise BlackboardError(
                    "El portal de Zoom no devolvió JSON al listar "
                    "grabaciones. Se guardó un volcado de diagnóstico."
                ) from exc
            if (
                not isinstance(payload, dict)
                or payload.get("status") is False
                or not isinstance(payload.get("result"), dict)
            ):
                raise BlackboardError(
                    "Zoom no completó la lista de grabaciones. No se considera vacío este curso."
                )
            result = payload["result"]
            meetings = result.get("list")
            if not isinstance(meetings, list) or page > 1000:
                self._dump("recordings_list", json.dumps(payload, indent=1, ensure_ascii=False))
                raise BlackboardError(
                    "No se reconoció la lista de grabaciones del portal de "
                    "Zoom. Se guardó un volcado de diagnóstico."
                )
            for m in meetings:
                if not isinstance(m, dict):
                    raise BlackboardError(
                        "La lista contiene una grabación no válida. Se conserva el inventario anterior."
                    )
                meeting_id = str(m.get("meetingId") or "")
                if not meeting_id:
                    raise BlackboardError(
                        "Una grabación no tiene identificador. El inventario no se puede considerar completo."
                    )
                if meeting_id in seen:
                    raise BlackboardError(
                        "Zoom repitió grabaciones durante la paginación. Se detuvo la búsqueda para evitar duplicados."
                    )
                seen.add(meeting_id)
                out.append(
                    Recording(
                        course_id=course_id,
                        course_name=course_name,
                        meeting_id=meeting_id,
                        topic=str(m.get("topic", "")),
                        start_time=str(m.get("listStartTime") or m.get("startTime", "")),
                        extra=m,
                    )
                )
            total = int(result.get("total", len(out)))
            page_size = int(result.get("pageSize", len(meetings) or 1))
            if (
                total < 0
                or page_size <= 0
                or (len(out) < total and (not meetings or page * page_size >= total))
            ):
                raise BlackboardError(
                    "Zoom devolvió una lista incompleta. Vuelve a buscar; se conserva el inventario anterior."
                )
            if len(out) >= total:
                break
            page += 1
        return out

    def recording_detail(self, origin: str, scid: str, headers: dict, rec: Recording) -> Recording:
        """Choose the largest actual video; distinguish denied access from no video."""
        r = self.session.get(
            f"{origin}/api/v1/lti/rich/recording/file",
            params={"meetingId": rec.meeting_id, "lti_scid": scid},
            headers=headers,
            timeout=30,
        )
        if r.status_code in (401, 403):
            raise BlackboardError(
                "Zoom rechazó la sesión de este curso (HTTP "
                + str(r.status_code)
                + "). Vuelve a buscar para renovar su acceso. Esto no significa que falte el video."
            )
        if r.status_code != 200:
            raise BlackboardError(
                f"No se pudo consultar esta grabación (HTTP {r.status_code}). Reintenta la búsqueda."
            )
        try:
            payload = r.json()
        except ValueError as exc:
            raise BlackboardError(
                "Zoom devolvió una página de acceso, no los archivos de la grabación. Renueva la sesión."
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("status") is False
            or not isinstance(payload.get("result"), dict)
        ):
            raise BlackboardError(
                "Zoom no autorizó o no completó la consulta de archivos. No se puede concluir que falte un video."
            )
        result = payload["result"]
        files = result.get("recordingFiles")
        if not isinstance(files, list):
            raise BlackboardError(
                "Zoom devolvió una lista de archivos no válida. Vuelve a buscar; no se puede determinar si falta el video."
            )
        rec.play_url = ""
        candidates = [
            f
            for f in files
            if isinstance(f, dict)
            and str(f.get("fileType", f.get("file_type", ""))).strip().upper() == "MP4"
            and (f.get("playUrl") or f.get("play_url"))
        ]

        def weight(f):
            try:
                return float(f.get("fileSize") or f.get("size") or f.get("duration") or 0)
            except (ValueError, TypeError):
                return 0

        chosen = max(candidates, key=weight) if candidates else None
        if chosen:
            rec.play_url = str(chosen.get("playUrl") or chosen.get("play_url") or "")
            self._validate_lti_url(rec.play_url)
            rec.extra["selected_video_bytes"] = weight(chosen)
        if not rec.play_url:
            self._dump(
                f"recording_{rec.meeting_id[:8]}",
                json.dumps(result, indent=1, ensure_ascii=False),
            )
            raise BlackboardError(
                "Zoom respondió correctamente, pero no incluyó un video MP4 con enlace de reproducción. Puede contener solo audio o estar procesándose; vuelve a buscar más tarde."
            )
        return rec

    # ── Utilidades ────────────────────────────────────────────────
    @staticmethod
    def _find_meeting_list(data: Any) -> list[dict] | None:
        """Busca recursivamente una lista de dicts con meetingId."""
        if isinstance(data, list):
            if (
                data
                and isinstance(data[0], dict)
                and ("meetingId" in data[0] or "topic" in data[0])
            ):
                return [d for d in data if isinstance(d, dict)]
            for item in data:
                found = BlackboardClient._find_meeting_list(item)
                if found is not None:
                    return found
        elif isinstance(data, dict):
            for value in data.values():
                found = BlackboardClient._find_meeting_list(value)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _find_first(data: Any, keys: tuple[str, ...]) -> Any:
        if isinstance(data, dict):
            for k in keys:
                if data.get(k):
                    return data[k]
            for value in data.values():
                found = BlackboardClient._find_first(value, keys)
                if found:
                    return found
        elif isinstance(data, list):
            for item in data:
                found = BlackboardClient._find_first(item, keys)
                if found:
                    return found
        return None

    # ── Material del curso ────────────────────────────────────────
    def _all_contents(self, course_id: str) -> list[dict]:
        """Todos los elementos de contenido del curso (paginado)."""
        url = f"{self.base}/learn/api/public/v1/courses/{course_id}/contents"
        params: dict[str, Any] = {"recursive": "true", "limit": 100}
        items: list[dict] = []
        visited = set()
        for _ in range(1000):
            if url in visited or urlparse(url).netloc != urlparse(self.base).netloc:
                raise BlackboardError(
                    "Paginación de material inconsistente. No se puede confirmar que esté completo."
                )
            visited.add(url)
            r = self.session.get(url, params=params, timeout=30)
            if r.status_code != 200:
                raise BlackboardError(
                    f"No se pudo revisar todo el material (HTTP {r.status_code}). Vuelve a iniciar sesión o reintenta."
                )
            data = r.json()
            items.extend(data.get("results", []))
            next_page = (data.get("paging") or {}).get("nextPage")
            if not next_page:
                return items
            url = urljoin(self.base, next_page)
            params = {}
        raise BlackboardError(
            "El curso supera el límite de seguridad de paginación. Resultado incompleto."
        )

    def _content_attachments(self, course_id, item):
        url = (
            f"{self.base}/learn/api/public/v1/courses/{course_id}/contents/{item['id']}/attachments"
        )
        params = {"limit": 100}
        visited = set()
        attachments = []
        for _ in range(1000):
            if (
                url in visited
                or urlparse(url).scheme != "https"
                or urlparse(url).netloc != urlparse(self.base).netloc
            ):
                raise BlackboardError("Paginación de adjuntos inconsistente.")
            visited.add(url)
            response = self.session.get(url, params=params, timeout=30)
            if response.status_code != 200:
                raise BlackboardError(
                    f"No se pudieron revisar los adjuntos (HTTP {response.status_code})."
                )
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise BlackboardError("Blackboard devolvió una lista de adjuntos no válida.")
            for entry in data["results"]:
                if not isinstance(entry, dict) or not entry.get("id") or not entry.get("fileName"):
                    raise BlackboardError(
                        "Blackboard devolvió un adjunto sin identificador o nombre."
                    )
                attachments.append(entry)
            next_page = (data.get("paging") or {}).get("nextPage")
            if not next_page:
                return attachments
            url = urljoin(self.base, next_page)
            params = {}
        raise BlackboardError("La lista de adjuntos está incompleta.")

    def _document_body(self, course_id, item):
        if isinstance(item.get("body"), str):
            return item["body"]
        response = self.session.get(
            f"{self.base}/learn/api/public/v1/courses/{course_id}/contents/{item['id']}",
            timeout=30,
        )
        if response.status_code != 200:
            raise BlackboardError(f"No se pudo abrir el documento (HTTP {response.status_code}).")
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("body"), str):
            raise BlackboardError(
                "Blackboard no devolvió el contenido del documento; no se puede comprobar si tiene archivos."
            )
        return data["body"]

    def list_course_materials(self, course_id: str, course_name: str) -> list[MaterialFile]:
        """Todos los archivos descargables del curso, con su ruta
        relativa reproduciendo las carpetas de Blackboard."""
        from zoom2txt.blackboard.materials import document_files

        items = self._all_contents(course_id)
        if not hasattr(self, "material_issues"):
            self.material_issues = []
        is_ultra = getattr(self, "_course_modes", {}).get(course_id) == "Ultra"
        # mapa id → (título, parentId) para reconstruir la ruta de carpetas
        meta = {it["id"]: (it.get("title", ""), it.get("parentId")) for it in items}
        by_id = {it["id"]: it for it in items}

        def folder_path(item: dict) -> str:
            parts: list[str] = []
            parent = item.get("parentId")
            visited = set()
            while parent and parent in meta:
                if parent in visited:
                    raise BlackboardError("La estructura de carpetas contiene un ciclo.")
                visited.add(parent)
                title, parent = meta[parent]
                parts.append(_safe_segment(title))
            return "/".join(reversed(parts))

        files: list[MaterialFile] = []
        seen_paths: set[str] = set()
        for it in items:
            handler = (it.get("contentHandler") or {}).get("id", "")
            parent = by_id.get(it.get("parentId"), {})
            title = (
                parent.get("title") if it.get("title") == "ultraDocumentBody" else it.get("title")
            ) or "Documento del curso"
            ultra_document = handler == "resource/x-bb-document" and (
                is_ultra
                or it.get("title") == "ultraDocumentBody"
                or (parent.get("contentHandler") or {}).get("isBbPage")
                or "bbMLEditorVersion" in str(it.get("body", ""))
            )
            try:
                if ultra_document:
                    parsed = document_files(self.base, self._document_body(course_id, it))
                    attachments = [
                        dict(id=f.id, fileName=f.filename, download_url=f.url) for f in parsed.files
                    ]
                    self.material_issues.extend(f"{title}: {issue}" for issue in parsed.issues)
                    if parsed.external_links:
                        self.coverage.append(f"{title}: {parsed.external_links} enlaces externos")
                    if parsed.has_text and not parsed.files:
                        self.coverage.append(f"{title}: documento de texto sin adjuntos")
                elif handler in ATTACH_HANDLERS:
                    attachments = self._content_attachments(course_id, it)
                else:
                    # Learning modules contain children already returned by
                    # the recursive contents query; they are not missing files.
                    if "folder" not in handler and handler != "resource/x-bb-lesson":
                        self.coverage.append(str(title))
                    continue
                folder = folder_path(it)
            except requests.RequestException:
                self.material_issues.append(
                    f"{title}: no se pudo conectar para revisar este recurso. Vuelve a buscar."
                )
                continue
            except (BlackboardError, ValueError, TypeError, KeyError) as exc:
                # Preserve the successfully inspected resources of this course.
                # Signed URLs are never included in these diagnostics.
                message = (
                    str(exc)
                    if isinstance(exc, BlackboardError)
                    else "El recurso tiene un formato no reconocido."
                )
                self.material_issues.append(f"{title}: {message}")
                continue
            seen_attachments = set()
            for att in attachments:
                if str(att["id"]) in seen_attachments:
                    continue
                seen_attachments.add(str(att["id"]))
                fname = _safe_segment(str(att.get("fileName", "archivo")))
                stable = (
                    __import__("hashlib")
                    .sha256(f"{it['id']}:{att.get('id')}".encode())
                    .hexdigest()[:8]
                )
                stem, suffix = Path(fname).stem, Path(fname).suffix
                fname = f"{stem}_{stable}{suffix}"
                rel = f"{folder}/{fname}" if folder else fname
                # dos adjuntos distintos con el mismo nombre → sufijo
                base_rel, n = rel, 2
                while rel in seen_paths:
                    stem, dot, ext = base_rel.rpartition(".")
                    rel = f"{stem}_{n}.{ext}" if dot else f"{base_rel}_{n}"
                    n += 1
                seen_paths.add(rel)
                files.append(
                    MaterialFile(
                        course_id=course_id,
                        course_name=course_name,
                        content_id=it["id"],
                        attachment_id=str(att.get("id")),
                        filename=fname,
                        rel_path=rel,
                        version=str(att.get("modified") or it.get("modified") or ""),
                        size=att.get("size") if isinstance(att.get("size"), int) else None,
                        download_url=att.get("download_url", ""),
                    )
                )
        return files

    def download_attachment(self, mf: MaterialFile, dest: Path, cancel=None) -> int:
        """Descarga un adjunto a dest; devuelve bytes escritos."""
        from zoom2txt.blackboard.materials import material_url

        if cancel is not None and cancel.is_set():
            raise InterruptedError("Descarga de material cancelada.")
        url = (
            f"{self.base}/learn/api/public/v1/courses/{mf.course_id}"
            f"/contents/{mf.content_id}/attachments/{mf.attachment_id}"
            f"/download"
        )
        if mf.download_url:
            try:
                url = material_url(self.base, mf.download_url)
            except ValueError as exc:
                raise BlackboardError(str(exc)) from None
        try:
            return self._download_material_file(url, mf, dest, cancel)
        except requests.RequestException:
            raise BlackboardError(
                "La conexión de descarga falló. Vuelve a buscar el material para renovar su enlace."
            ) from None

    def _download_material_file(self, url, mf, dest, cancel):
        with self.session.get(url, stream=True, timeout=120, allow_redirects=True) as r:
            if r.status_code in (401, 403):
                raise BlackboardError(
                    "La sesión o el enlace del material ya no permite descargarlo. Pulsa Buscar material para renovar el acceso."
                )
            if r.status_code != 200:
                raise BlackboardError(
                    f"Blackboard respondió {r.status_code} al descargar {mf.filename}."
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            fd, temporary = tempfile.mkstemp(prefix=".material-", suffix=".part", dir=dest.parent)
            tmp = Path(temporary)
            prefix = b""
            try:
                with os.fdopen(fd, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if cancel is not None and cancel.is_set():
                            raise InterruptedError("Descarga de material cancelada.")
                        if len(prefix) < 512:
                            prefix += chunk[: 512 - len(prefix)]
                        fh.write(chunk)
                        written += len(chunk)
                    fh.flush()
                    os.fsync(fh.fileno())
                content_type = r.headers.get("Content-Type", "").lower()
                html = prefix.lstrip().lower().startswith(
                    (b"<!doctype html", b"<html")
                ) or content_type.startswith("text/html")
                suffix = dest.suffix.lower()
                if (
                    written == 0
                    or (html and suffix not in (".html", ".htm"))
                    or "/login" in str(r.url).lower()
                ):
                    raise BlackboardError(
                        "El servidor devolvió una página de acceso o un archivo vacío, no el material solicitado."
                    )
                if suffix == ".pdf" and not prefix.startswith(b"%PDF-"):
                    raise BlackboardError("El PDF recibido no tiene un formato válido.")
                if suffix in (
                    ".docx",
                    ".xlsx",
                    ".pptx",
                    ".zip",
                ) and not prefix.startswith(b"PK"):
                    raise BlackboardError("El documento recibido no tiene un formato válido.")
                expected = (
                    r.headers.get("Content-Length")
                    if not r.headers.get("Content-Encoding")
                    else None
                )
                if expected and int(expected) != written:
                    raise BlackboardError(
                        "La descarga está incompleta. Se conserva el archivo anterior."
                    )
                if mf.size is not None and mf.size > 0 and mf.size != written:
                    raise BlackboardError("El tamaño no coincide con el material solicitado.")
                tmp.replace(dest)
            finally:
                tmp.unlink(missing_ok=True)
            return written

    def export_cookies(self, path: Path) -> None:
        """Vuelca las cookies de la sesión a formato Netscape para que
        yt-dlp acceda a la grabación protegida."""
        from http.cookiejar import Cookie, MozillaCookieJar

        jar = MozillaCookieJar()
        for c in self.session.cookies:
            jar.set_cookie(
                Cookie(
                    version=0,
                    name=c.name,
                    value=c.value,
                    port=None,
                    port_specified=False,
                    domain=c.domain,
                    domain_specified=True,
                    domain_initial_dot=str(c.domain).startswith("."),
                    path=c.path or "/",
                    path_specified=True,
                    secure=bool(c.secure),
                    expires=c.expires,
                    discard=False,
                    comment=None,
                    comment_url=None,
                    rest={},
                )
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".cookies-", dir=path.parent)
        os.close(fd)
        try:
            jar.save(temporary, ignore_discard=True, ignore_expires=False)
            write_private_json(path, {"netscape": Path(temporary).read_text(encoding="utf-8")})
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _dump(self, name: str, content: str) -> None:
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            path = DEBUG_DIR / f"{safe_name(name)}_{time.time_ns()}.txt"
            # Raw LTI/HTML may contain signed credentials. Store structure only.
            summary = {
                "diagnostic": safe_name(name),
                "response_bytes": len(content.encode("utf-8")),
                "has_form": "<form" in content.lower(),
                "has_html": "<html" in content.lower(),
            }
            atomic_write(path, json.dumps(summary, indent=2))
            for previous in sorted(
                DEBUG_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True
            )[20:]:
                previous.unlink(missing_ok=True)
            self.log(f"[i] Volcado de diagnóstico: {path}")
        except OSError:
            pass
