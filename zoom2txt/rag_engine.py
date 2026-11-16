"""Motor RAG Híbrido (FTS5 BM25 + Embeddings Matryoshka 768d) e ingesta automática
de transcripciones y materiales de curso para Zoom2TXT."""

from __future__ import annotations

import json
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from zoom2txt.blackboard.registry import Registry, short_name_for
from zoom2txt.cleaning.subtitle_cleaner import vtt_a_texto
from zoom2txt.config import (
    DEFAULT_GEMINI_MODEL,
    EMBEDDING_MODEL,
    FREE_TIER_MODELS,
    Settings,
    load_gemini_api_key,
)
from zoom2txt.library import TXT_SUFFIX, _course_from_name
from zoom2txt.security import redact
from zoom2txt.storage import StateStore, digest, file_ok

STRICT_SYSTEM_PROMPT = (
    "Eres el asistente académico oficial integrado en Zoom2TXT. Tu ÚNICA fuente de verdad son las "
    "transcripciones de clases y los materiales de estudio provistos en el bloque <CONTEXTO_BASE_DE_DATOS>.\n"
    "REGLAS ESTRICTAS:\n"
    "1. Responde ÚNICAMENTE con la información contenida en <CONTEXTO_BASE_DE_DATOS>.\n"
    "2. PROHIBIDO inventar datos, suponer fechas de exámenes o responder usando conocimiento general fuera de las fuentes provistas.\n"
    "3. Si la respuesta a la pregunta del estudiante NO se encuentra explícitamente en las fuentes provistas, responde exactamente: "
    "'No encontré información sobre esto en las transcripciones ni en los materiales cargados para la selección actual.'\n"
    "4. Cita SIEMPRE el origen exacto de cada afirmación al final del párrafo o idea usando el formato: "
    "`[Curso: <Nombre> | Fuente: <Título de Clase o Archivo>]`."
)

NO_INFO_RESPONSE = (
    "No encontré información sobre esto en las transcripciones ni en los materiales cargados para la selección actual."
)

SUPPORTED_EXTENSIONS = {
    ".txt": "txt",
    ".md": "txt",
    ".pdf": "pdf",
    ".pptx": "pptx",
    ".docx": "docx",
}

_SPANISH_STOPWORDS = frozenset(
    {
        "el",
        "la",
        "los",
        "las",
        "un",
        "una",
        "unos",
        "unas",
        "de",
        "del",
        "al",
        "a",
        "en",
        "con",
        "por",
        "para",
        "sin",
        "sobre",
        "entre",
        "hacia",
        "hasta",
        "desde",
        "que",
        "qué",
        "cual",
        "cuál",
        "cuales",
        "cuáles",
        "como",
        "cómo",
        "cuando",
        "cuándo",
        "donde",
        "dónde",
        "quien",
        "quién",
        "es",
        "son",
        "fue",
        "era",
        "ser",
        "está",
        "están",
        "este",
        "esta",
        "estos",
        "estas",
        "ese",
        "esa",
        "esos",
        "esas",
        "aquel",
        "aquella",
        "mi",
        "tu",
        "su",
        "mis",
        "tus",
        "sus",
        "nuestro",
        "nuestra",
        "y",
        "o",
        "u",
        "pero",
        "mas",
        "más",
        "sino",
        "porque",
        "pues",
        "si",
        "sí",
        "no",
        "se",
        "le",
        "les",
        "lo",
        "me",
        "te",
        "nos",
        "hay",
        "tiene",
        "tienen",
        "dijo",
        "dice",
        "explica",
        "explicó",
        "menciona",
        "mencionó",
        "habla",
        "trata",
        "acerca",
        "según",
    }
)


class RateLimitExceededError(RuntimeError):
    """Error específico para código HTTP 429 (límite gratuito por minuto alcanzado)."""

    def __init__(self, message: str, retry_after: int = 30):
        super().__init__(message)
        self.retry_after = max(5, int(retry_after))


class RateLimiter:
    """Controla las llamadas por minuto (RPM) en el Free Tier para priorizar FTS5 local."""

    def __init__(self, max_rpm: int = 10, window_seconds: float = 60.0):
        self.max_rpm = max_rpm
        self.window_seconds = window_seconds
        self._timestamps: deque[float] = deque()

    def _prune(self, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        while self._timestamps and (now - self._timestamps[0]) > self.window_seconds:
            self._timestamps.popleft()

    def record_call(self, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        self._prune(now)
        self._timestamps.append(now)

    def remaining_rpm(self, now: float | None = None) -> int:
        self._prune(now)
        return max(0, self.max_rpm - len(self._timestamps))

    def near_limit(self, reserve_for_generation: int = 3, now: float | None = None) -> bool:
        """True si conviene ahorrar cuota de embeddings y usar FTS5 directo en 1 sola llamada."""
        return self.remaining_rpm(now) <= reserve_for_generation


@dataclass
class SectionData:
    text: str
    metadata: dict = field(default_factory=dict)


@dataclass
class ChunkData:
    chunk_index: int
    content: str
    metadata: dict = field(default_factory=dict)


@dataclass
class RetrievedChunk:
    chunk_id: int
    source_id: int
    course_id: str
    course_name: str
    source_title: str
    source_type: str
    chunk_index: int
    content: str
    metadata: dict
    score: float = 0.0


@dataclass
class RAGResponse:
    answer: str
    citations: list[dict]
    chunks: list[RetrievedChunk]
    model_used: str
    used_embeddings: bool = False


# ── Serialización de Embeddings (float32 + numpy) ─────────────────
def serialize_embedding(vector: list[float] | np.ndarray) -> bytes:
    arr = np.asarray(vector, dtype=np.float32).ravel()
    return arr.tobytes()


def deserialize_embedding(blob: bytes | None) -> np.ndarray | None:
    if not blob:
        return None
    try:
        arr = np.frombuffer(blob, dtype=np.float32)
        return arr if arr.size > 0 else None
    except (ValueError, TypeError):
        return None


def cosine_similarity(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
    if vec_a is None or vec_b is None or vec_a.shape != vec_b.shape:
        return 0.0
    norm_a = float(np.linalg.norm(vec_a))
    norm_b = float(np.linalg.norm(vec_b))
    if norm_a < 1e-10 or norm_b < 1e-10:
        return 0.0
    return float(np.dot(vec_a, vec_b) / (norm_a * norm_b))


# ── Text Chunker Inteligente (~600-800 palabras, solapamiento 100) ──
def chunk_sections(
    sections: list[SectionData],
    target_words: int = 650,
    max_words: int = 800,
    overlap_words: int = 100,
) -> list[ChunkData]:
    """Divide secciones de texto en bloques de ~600 a 800 palabras con solapamiento de 100 palabras,
    preservando el contexto de los párrafos y metadatos (página, diapositiva, minuto)."""
    chunks: list[ChunkData] = []
    current_words: list[str] = []
    current_paragraphs: list[str] = []
    current_meta: dict = {}

    def flush_chunk() -> None:
        nonlocal current_words, current_paragraphs, current_meta
        text = "\n\n".join(p.strip() for p in current_paragraphs if p.strip()).strip()
        if not text:
            return
        meta = dict(current_meta)
        meta["word_count"] = len(text.split())
        chunks.append(
            ChunkData(
                chunk_index=len(chunks),
                content=text,
                metadata=meta,
            )
        )
        if overlap_words > 0 and len(current_words) > overlap_words:
            tail = " ".join(current_words[-overlap_words:])
            current_paragraphs = [tail]
            current_words = tail.split()
        elif overlap_words > 0 and current_words:
            tail = " ".join(current_words)
            current_paragraphs = [tail]
            current_words = tail.split()
        else:
            current_paragraphs = []
            current_words = []

    for section in sections:
        raw_text = (section.text or "").strip()
        if not raw_text:
            continue
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", raw_text) if p.strip()]
        for para in paragraphs:
            para_words = para.split()
            if not para_words:
                continue
            if not current_meta:
                current_meta = dict(section.metadata)
            # Si un único párrafo es gigante (> max_words), lo subdividimos respetando el solapamiento
            if len(para_words) > max_words:
                if current_words and len(current_words) >= target_words // 2:
                    flush_chunk()
                step = max(1, target_words - overlap_words)
                idx = 0
                while idx < len(para_words):
                    slice_words = para_words[idx : idx + max_words]
                    if not slice_words:
                        break
                    current_meta = dict(section.metadata)
                    current_paragraphs = [" ".join(slice_words)]
                    current_words = list(slice_words)
                    flush_chunk()
                    if idx + max_words >= len(para_words):
                        current_paragraphs = []
                        current_words = []
                        break
                    idx += step
                current_meta = dict(section.metadata)
                continue

            if current_words and (len(current_words) + len(para_words) > max_words):
                flush_chunk()
                current_meta = dict(section.metadata)

            # Fusionar metadatos (ej. rango de páginas o diapositivas)
            for k, v in section.metadata.items():
                if k not in current_meta:
                    current_meta[k] = v
                elif k in ("page", "slide") and current_meta[k] != v:
                    start_val = str(current_meta[k]).split("-")[0]
                    current_meta[k] = f"{start_val}-{v}"

            current_paragraphs.append(para)
            current_words.extend(para_words)

            if len(current_words) >= target_words:
                flush_chunk()
                current_meta = dict(section.metadata)

    # Emitir remanente si no es meramente el solapamiento del bloque previo
    if current_paragraphs:
        if not chunks or len(current_words) > overlap_words:
            text = "\n\n".join(p.strip() for p in current_paragraphs if p.strip()).strip()
            if text:
                meta = dict(current_meta)
                meta["word_count"] = len(text.split())
                chunks.append(
                    ChunkData(
                        chunk_index=len(chunks),
                        content=text,
                        metadata=meta,
                    )
                )

    return chunks


def chunk_text(
    text: str,
    base_metadata: dict | None = None,
    target_words: int = 650,
    max_words: int = 800,
    overlap_words: int = 100,
    is_transcript: bool = False,
) -> list[ChunkData]:
    """Divide un texto plano o transcripción en bloques de ~600-800 palabras con solapamiento de 100."""
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    if cleaned.startswith("WEBVTT"):
        cleaned = vtt_a_texto(cleaned)

    base_meta = dict(base_metadata or {})
    if is_transcript:
        # Estimar minuto aproximado (~140 palabras por minuto en clases habladas)
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", cleaned) if p.strip()]
        sections: list[SectionData] = []
        running_words = 0
        for p in paragraphs:
            approx_min = max(1, round(running_words / 140) + 1)
            meta = dict(base_meta)
            meta["approx_minute"] = approx_min
            sections.append(SectionData(text=p, metadata=meta))
            running_words += len(p.split())
        return chunk_sections(
            sections,
            target_words=target_words,
            max_words=max_words,
            overlap_words=overlap_words,
        )

    return chunk_sections(
        [SectionData(text=cleaned, metadata=base_meta)],
        target_words=target_words,
        max_words=max_words,
        overlap_words=overlap_words,
    )


# ── Extracción de Materiales (PDF, PPTX, DOCX, TXT/MD) ───────────
def extract_sections_from_file(path: Path, source_type: str | None = None) -> list[SectionData]:
    path = Path(path)
    ext = path.suffix.lower()
    stype = source_type or SUPPORTED_EXTENSIONS.get(ext, "txt")

    if stype in ("transcript", "txt") or ext in (".txt", ".md"):
        raw = path.read_text(encoding="utf-8", errors="replace")
        if raw.lstrip().startswith("WEBVTT"):
            raw = vtt_a_texto(raw)
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
        sections: list[SectionData] = []
        running_words = 0
        for p in paragraphs:
            meta = {}
            if stype == "transcript":
                meta["approx_minute"] = max(1, round(running_words / 140) + 1)
            sections.append(SectionData(text=p, metadata=meta))
            running_words += len(p.split())
        return sections

    if stype == "pdf" or ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        sections = []
        for idx, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                sections.append(SectionData(text=text, metadata={"page": idx}))
        return sections

    if stype == "pptx" or ext == ".pptx":
        from pptx import Presentation

        prs = Presentation(str(path))
        sections = []
        for idx, slide in enumerate(prs.slides, start=1):
            parts: list[str] = []
            slide_title = ""
            if slide.shapes.title and slide.shapes.title.text:
                slide_title = slide.shapes.title.text.strip()
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False) and shape.text_frame:
                    txt = shape.text_frame.text.strip()
                    if txt:
                        parts.append(txt)
                if getattr(shape, "has_table", False) and shape.table:
                    for row in shape.table.rows:
                        row_txt = " | ".join(
                            cell.text.strip() for cell in row.cells if cell.text.strip()
                        )
                        if row_txt:
                            parts.append(row_txt)
            if getattr(slide, "has_notes_slide", False) and slide.notes_slide:
                notes_tf = getattr(slide.notes_slide, "notes_text_frame", None)
                if notes_tf and notes_tf.text.strip():
                    parts.append(notes_tf.text.strip())
            combined = "\n\n".join(parts).strip()
            if combined:
                meta: dict = {"slide": idx}
                if slide_title:
                    meta["section"] = slide_title[:120]
                sections.append(SectionData(text=combined, metadata=meta))
        return sections

    if stype == "docx" or ext == ".docx":
        from docx import Document

        doc = Document(str(path))
        sections = []
        current_heading = ""
        buffer: list[str] = []

        def flush_docx_section() -> None:
            if buffer:
                meta = {"section": current_heading} if current_heading else {}
                sections.append(SectionData(text="\n\n".join(buffer), metadata=meta))
                buffer.clear()

        for para in doc.paragraphs:
            txt = para.text.strip()
            if not txt:
                continue
            style_name = getattr(getattr(para, "style", None), "name", "") or ""
            if style_name.lower().startswith(("heading", "título", "titulo")):
                flush_docx_section()
                current_heading = txt[:120]
                buffer.append(txt)
            else:
                buffer.append(txt)

        for table in doc.tables:
            for row in table.rows:
                row_txt = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_txt:
                    buffer.append(row_txt)

        flush_docx_section()
        return sections

    raise ValueError(f"Formato de archivo no soportado para indexación RAG: {path.suffix}")


# ── Resolución de Cursos y Auto-Ingesta ──────────────────────────
def _resolve_course_identity(
    settings: Settings,
    store: StateStore,
    file_path: Path,
    course_id: str = "",
    course_name: str = "",
) -> tuple[str, str]:
    """Determina (course_id, course_name) vinculando con los registros existentes de Zoom2TXT."""
    cid = str(course_id or "").strip()
    cname = str(course_name or "").strip()

    # 1. Mapa de cursos conocidos en StateStore
    known_courses: dict[str, str] = {}
    short_to_course: dict[str, tuple[str, str]] = {}

    meta = store.get("meta", "current_courses")
    for ch in meta.get("choices", []):
        if ch.get("id") and ch.get("name"):
            known_courses[str(ch["id"])] = str(ch["name"])
            short_to_course[short_name_for(ch["name"]).lower()] = (
                str(ch["id"]),
                str(ch["name"]),
            )

    for c in store.list("course"):
        if c.get("id") and c.get("name"):
            known_courses[str(c["id"])] = str(c["name"])
            short_to_course[short_name_for(c["name"]).lower()] = (
                str(c["id"]),
                str(c["name"]),
            )

    for c in store.list_rag_courses():
        if c.get("id") and c.get("name"):
            known_courses.setdefault(str(c["id"]), str(c["name"]))
            short_to_course.setdefault(
                short_name_for(c["name"]).lower(), (str(c["id"]), str(c["name"]))
            )

    if cid and not cname and cid in known_courses:
        cname = known_courses[cid]

    if cid and cname:
        return cid, cname

    # 2. Buscar coincidencia por archivo en recording / job / catalog
    resolved_str = str(file_path.resolve()) if file_path.exists() else str(file_path)
    stem = file_path.name
    if stem.endswith(TXT_SUFFIX):
        stem = stem[: -len(TXT_SUFFIX)]
    else:
        stem = file_path.stem

    for rec in store.list("recording"):
        rec_path = rec.get("path", "")
        rec_name = rec.get("name", "")
        if (rec_path and Path(rec_path).name == file_path.name) or (rec_name and rec_name == stem):
            rcid = str(rec.get("course_id", "")).strip()
            if rcid:
                cid = cid or rcid
                cname = cname or known_courses.get(rcid, "")
                break

    if not cid:
        for job in store.list("job"):
            job_txt = job.get("txt", "")
            job_name = job.get("name", "") or job.get("display_name", "")
            if (job_txt and (job_txt == resolved_str or Path(job_txt).name == file_path.name)) or (
                job_name and job_name == stem
            ):
                jcid = str(job.get("course_id", "")).strip()
                if jcid:
                    cid = jcid
                    cname = cname or known_courses.get(jcid, "")
                    break

    if cid and not cname:
        for cat in store.list("catalog"):
            if str(cat.get("course_id", "")) == cid and cat.get("course_name"):
                cname = str(cat["course_name"])
                break

    # 3. Inferir desde el prefijo del archivo (ej. 'AprendizDeMaquina_Semana14...')
    prefix = _course_from_name(stem)
    if not cid and prefix.lower() in short_to_course:
        cid, cname = short_to_course[prefix.lower()]

    if not cid:
        cid = prefix or "general"
    if not cname:
        cname = known_courses.get(cid, prefix or cid)

    return cid, cname


def infer_source_week(value: str) -> int | None:
    """Extrae el número de semana (1-20) desde el título o ruta de una fuente."""
    m = re.search(r"(?:semana|sem)[\s_-]*0*(\d{1,2})\b", str(value or ""), flags=re.I)
    if m:
        w = int(m.group(1))
        if 1 <= w <= 20:
            return w
    return None


def clean_source_title(file_path: Path, title: str = "") -> str:
    if title:
        cleaned = title.strip()
        if cleaned.endswith(TXT_SUFFIX):
            cleaned = cleaned[: -len(TXT_SUFFIX)]
        return cleaned
    name = file_path.name
    if name.endswith(TXT_SUFFIX):
        return name[: -len(TXT_SUFFIX)]
    return file_path.stem


def ingest_file(
    settings: Settings,
    file_path: Path | str,
    course_id: str = "",
    course_name: str = "",
    source_type: str | None = None,
    title: str = "",
    store: StateStore | None = None,
) -> int | None:
    """Indexa un archivo de transcripción o material en knowledge_sources y knowledge_chunks.
    Evita re-indexar si el hash SHA-256 no ha cambiado."""
    path = Path(file_path)
    if not file_ok(path):
        return None

    store = store or StateStore(settings)
    resolved_path = str(path.resolve())
    current_hash = digest(path)

    cid, cname = _resolve_course_identity(settings, store, path, course_id, course_name)
    ext = path.suffix.lower()
    if source_type is None:
        if path.name.endswith(TXT_SUFFIX):
            stype = "transcript"
        else:
            stype = SUPPORTED_EXTENSIONS.get(ext, "txt")
    else:
        stype = source_type

    doc_title = clean_source_title(path, title)

    # Comprobar si ya está indexado con el mismo SHA-256
    with store.connect() as db:
        existing = db.execute(
            "SELECT id, file_hash, course_id, course_name FROM knowledge_sources WHERE file_path = ?",
            (resolved_path,),
        ).fetchone()
        if existing:
            source_id, saved_hash, saved_cid, saved_cname = existing
            chunk_count = db.execute(
                "SELECT COUNT(*) FROM knowledge_chunks WHERE source_id = ?",
                (source_id,),
            ).fetchone()[0]
            if saved_hash == current_hash and chunk_count > 0:
                if (cid and cid != saved_cid) or (cname and cname != saved_cname):
                    db.execute("BEGIN IMMEDIATE")
                    db.execute(
                        "UPDATE knowledge_sources SET course_id = ?, course_name = ?, namespace = ? WHERE id = ?",
                        (cid, cname, store.namespace, source_id),
                    )
                    db.execute(
                        "UPDATE knowledge_chunks SET course_id = ? WHERE source_id = ?",
                        (cid, source_id),
                    )
                store.upsert_course(cid, cname)
                return int(source_id)

    sections = extract_sections_from_file(path, stype)
    chunks = chunk_sections(sections)
    if not chunks:
        return None

    now_iso = datetime.now(UTC).isoformat()
    store.upsert_course(cid, cname, now_iso)

    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT id FROM knowledge_sources WHERE file_path = ?",
            (resolved_path,),
        ).fetchone()
        if row:
            source_id = int(row[0])
            db.execute(
                """
                UPDATE knowledge_sources
                SET namespace = ?, course_id = ?, course_name = ?, source_type = ?,
                    title = ?, file_hash = ?, created_at = ?
                WHERE id = ?
                """,
                (
                    store.namespace,
                    cid,
                    cname,
                    stype,
                    doc_title,
                    current_hash,
                    now_iso,
                    source_id,
                ),
            )
            db.execute("DELETE FROM knowledge_chunks WHERE source_id = ?", (source_id,))
        else:
            cur = db.execute(
                """
                INSERT INTO knowledge_sources
                    (namespace, course_id, course_name, source_type, title, file_path, file_hash, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    store.namespace,
                    cid,
                    cname,
                    stype,
                    doc_title,
                    resolved_path,
                    current_hash,
                    now_iso,
                ),
            )
            source_id = int(cur.lastrowid)

        for chunk in chunks:
            meta = dict(chunk.metadata)
            meta.setdefault("course_name", cname)
            meta.setdefault("source_title", doc_title)
            meta.setdefault("source_type", stype)
            db.execute(
                """
                INSERT INTO knowledge_chunks
                    (source_id, course_id, chunk_index, content, metadata_json, embedding_blob)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (
                    source_id,
                    cid,
                    chunk.chunk_index,
                    chunk.content,
                    json.dumps(meta, ensure_ascii=False),
                ),
            )

    return source_id


def auto_ingest_transcript(
    settings: Settings,
    file_path: Path | str,
    course_id: str = "",
    course_name: str = "",
    title: str = "",
) -> int | None:
    """Hook seguro invocado al terminar cualquier transcripción en Zoom2TXT."""
    try:
        return ingest_file(
            settings=settings,
            file_path=file_path,
            course_id=course_id,
            course_name=course_name,
            source_type="transcript",
            title=title,
        )
    except Exception:  # noqa: BLE001 — nunca interrumpir el pipeline principal de transcripción
        return None


def sync_existing_transcripts(
    settings: Settings,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> dict[str, int]:
    """Escanea todas las transcripciones descargadas en versiones anteriores o actuales
    y las incorpora automáticamente a knowledge_sources y knowledge_chunks."""
    store = StateStore(settings)
    registry = Registry(settings)

    candidates: list[tuple[Path, str, str, str]] = []
    seen_paths: set[str] = set()

    def add_candidate(p: Path, cid: str = "", cname: str = "", title: str = "") -> None:
        try:
            if not file_ok(p):
                return
            resolved = str(p.resolve())
            if resolved in seen_paths:
                return
            seen_paths.add(resolved)
            candidates.append((p, cid, cname, title))
        except OSError:
            return

    # 1. Desde el registro de grabaciones completadas
    for rec in registry.records():
        if rec.get("status") == "done" and rec.get("path"):
            add_candidate(
                Path(rec["path"]),
                str(rec.get("course_id", "")),
                "",
                str(rec.get("name", "")),
            )

    # 2. Desde la tabla de trabajos (jobs)
    for job in store.list("job"):
        if job.get("txt"):
            add_candidate(
                Path(job["txt"]),
                str(job.get("course_id", "")),
                "",
                str(job.get("display_name") or job.get("name") or ""),
            )

    # 3. Escaneo de la carpeta de transcripciones en disco
    if settings.transcripts_dir.exists():
        for txt_path in sorted(settings.transcripts_dir.rglob(f"*{TXT_SUFFIX}")):
            add_candidate(txt_path)
        for txt_path in sorted(settings.transcripts_dir.rglob("*.txt")):
            add_candidate(txt_path)

    indexed = 0
    skipped = 0
    total = len(candidates)

    for idx, (path, cid, cname, title) in enumerate(candidates, start=1):
        if progress_callback:
            progress_callback(idx, total, path.name)
        try:
            source_id = ingest_file(
                settings=settings,
                file_path=path,
                course_id=cid,
                course_name=cname,
                source_type="transcript",
                title=title,
                store=store,
            )
            if source_id is not None:
                indexed += 1
            else:
                skipped += 1
        except Exception:  # noqa: BLE001
            skipped += 1

    return {"total": total, "indexed": indexed, "skipped": skipped}


def sync_existing_materials(
    settings: Settings,
    course_id: str | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> dict[str, int]:
    """Escanea los materiales descargados (.pdf, .pptx, .docx, .txt, .md) y los indexa por curso."""
    store = StateStore(settings)
    candidates: list[tuple[Path, str, str]] = []
    seen_paths: set[str] = set()

    # Mapa de carpetas de curso en materials_dir
    known_courses = {c["id"]: c["name"] for c in store.list_rag_courses()}
    for c in store.list("course"):
        if c.get("id") and c.get("name"):
            known_courses[str(c["id"])] = str(c["name"])

    for mat in store.list("material"):
        p_str = mat.get("path", "")
        key = str(mat.get("id", ""))
        cid = key.split(":")[0] if ":" in key else ""
        if course_id is not None and cid and cid != course_id:
            continue
        if p_str:
            p = Path(p_str)
            if p.suffix.lower() in SUPPORTED_EXTENSIONS and file_ok(p):
                resolved = str(p.resolve())
                if resolved not in seen_paths:
                    seen_paths.add(resolved)
                    candidates.append((p, cid, known_courses.get(cid, "")))

    if settings.materials_dir.exists():
        for p in sorted(settings.materials_dir.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if not file_ok(p):
                continue
            resolved = str(p.resolve())
            if resolved in seen_paths:
                continue
            # Intentar deducir el curso por el nombre de la subcarpeta (<ShortName>_<course_id>)
            inferred_cid = ""
            inferred_cname = ""
            for parent in p.parents:
                if parent == settings.materials_dir:
                    break
                for kcid, kcname in known_courses.items():
                    folder_prefix = f"{short_name_for(kcname)}_{kcid.strip('_')}"
                    if parent.name == folder_prefix or parent.name.endswith("_" + kcid.strip("_")):
                        inferred_cid = kcid
                        inferred_cname = kcname
                        break
                if inferred_cid:
                    break
            if course_id is not None and inferred_cid and inferred_cid != course_id:
                continue
            seen_paths.add(resolved)
            candidates.append((p, inferred_cid or (course_id or "general"), inferred_cname))

    indexed = 0
    skipped = 0
    total = len(candidates)
    for idx, (path, cid, cname) in enumerate(candidates, start=1):
        if progress_callback:
            progress_callback(idx, total, path.name)
        try:
            ext = path.suffix.lower()
            stype = SUPPORTED_EXTENSIONS.get(ext, "txt")
            sid = ingest_file(
                settings=settings,
                file_path=path,
                course_id=cid,
                course_name=cname,
                source_type=stype,
                store=store,
            )
            if sid is not None:
                indexed += 1
            else:
                skipped += 1
        except Exception:  # noqa: BLE001
            skipped += 1

    return {"total": total, "indexed": indexed, "skipped": skipped}


# ── Búsqueda Híbrida FTS5 (BM25) + Embeddings Opcionales ─────────
def build_fts5_query(question: str) -> str:
    """Convierte una pregunta en lenguaje natural en una consulta FTS5 segura y efectiva."""
    words = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9]{2,}", (question or "").lower())
    if not words:
        return ""
    meaningful = [w for w in words if w not in _SPANISH_STOPWORDS and len(w) >= 2]
    chosen = meaningful if meaningful else words
    # Limitar términos y usar OR para que BM25 ordene por mayor cantidad de coincidencias
    unique_terms: list[str] = []
    for w in chosen[:16]:
        if w not in unique_terms:
            unique_terms.append(w)
    return " OR ".join(f'"{term}"*' if len(term) >= 4 else f'"{term}"' for term in unique_terms)


class RAGEngine:
    """Motor de recuperación híbrida (SQLite FTS5 BM25 + Gemini Embeddings 768d) y generación con blindaje estricto."""

    def __init__(
        self,
        settings: Settings | None = None,
        store: StateStore | None = None,
        rate_limiter: RateLimiter | None = None,
    ):
        self.settings = settings or Settings.load()
        self.store = store or StateStore(self.settings)
        self.rate_limiter = rate_limiter or RateLimiter(max_rpm=10)

    def _get_api_key(self) -> str:
        return (self.settings.gemini_api_key or load_gemini_api_key() or "").strip()

    def retrieve(
        self,
        question: str,
        course_id: str | None = None,
        all_courses: bool = False,
        active_source_ids: list[int] | None = None,
        weeks: list[int] | None = None,
        top_k: int = 8,
        use_embeddings: bool | None = None,
    ) -> tuple[list[RetrievedChunk], bool]:
        """Recupera los fragmentos más relevantes filtrando estrictamente por curso o en modo global.
        Devuelve (lista_de_chunks, used_embeddings)."""
        if not all_courses and not course_id:
            return [], False

        if weeks:
            target_weeks = {int(w) for w in weeks}
            scoped_sources = self.store.list_knowledge_sources(
                course_id=course_id,
                all_courses=all_courses,
                active_only=True,
            )
            week_ids = [
                int(src["id"])
                for src in scoped_sources
                if (
                    infer_source_week(src.get("title", "")) in target_weeks
                    or infer_source_week(src.get("file_path", "")) in target_weeks
                )
            ]
            if active_source_ids is not None:
                allowed = set(int(sid) for sid in active_source_ids)
                active_source_ids = [sid for sid in week_ids if sid in allowed]
            else:
                active_source_ids = week_ids

        fts_query = build_fts5_query(question)
        candidates: dict[int, tuple[RetrievedChunk, bytes | None]] = {}

        # Construcción de filtros estrictos de ámbito (Scope)
        scope_clauses = ["(ks.namespace = ? OR ks.namespace = '')", "ks.is_active = 1"]
        scope_params: list = [self.store.namespace]

        if not all_courses:
            scope_clauses.append("ks.course_id = ?")
            scope_clauses.append("kc.course_id = ?")
            scope_params.extend([str(course_id), str(course_id)])

        if active_source_ids is not None:
            if len(active_source_ids) == 0:
                return [], False
            placeholders = ",".join("?" for _ in active_source_ids)
            scope_clauses.append(f"ks.id IN ({placeholders})")
            scope_params.extend(int(sid) for sid in active_source_ids)

        where_scope = " AND ".join(scope_clauses)

        with self.store.connect() as db:
            # 1. Búsqueda léxica rápida con SQLite FTS5 (BM25)
            if fts_query:
                try:
                    fts_sql = f"""
                        SELECT
                            kc.id,
                            kc.source_id,
                            kc.course_id,
                            ks.course_name,
                            ks.title,
                            ks.source_type,
                            kc.chunk_index,
                            kc.content,
                            kc.metadata_json,
                            kc.embedding_blob,
                            bm25(knowledge_chunks_fts) AS rank_score
                        FROM knowledge_chunks_fts
                        JOIN knowledge_chunks kc ON kc.id = knowledge_chunks_fts.rowid
                        JOIN knowledge_sources ks ON ks.id = kc.source_id
                        WHERE knowledge_chunks_fts MATCH ?
                          AND {where_scope}
                        ORDER BY rank_score ASC
                        LIMIT ?
                    """  # noqa: S608
                    rows = db.execute(fts_sql, [fts_query, *scope_params, top_k * 3]).fetchall()
                    for idx, r in enumerate(rows):
                        try:
                            meta = json.loads(r[8]) if r[8] else {}
                        except (TypeError, ValueError):
                            meta = {}
                        # En SQLite FTS5, bm25() devuelve valores negativos (más negativo = más relevante)
                        raw_bm25 = float(r[10]) if r[10] is not None else 0.0
                        lexical_score = 1.0 / (1.0 + idx) + max(0.0, -raw_bm25) * 0.1
                        chunk = RetrievedChunk(
                            chunk_id=int(r[0]),
                            source_id=int(r[1]),
                            course_id=str(r[2]),
                            course_name=str(r[3]),
                            source_title=str(r[4]),
                            source_type=str(r[5]),
                            chunk_index=int(r[6]),
                            content=str(r[7]),
                            metadata=meta,
                            score=lexical_score,
                        )
                        candidates[chunk.chunk_id] = (chunk, r[9])
                except Exception:  # noqa: BLE001, S110
                    pass

            # 2. Si la pregunta es general o FTS5 devolvió pocos resultados, completar con chunks del ámbito activo
            if len(candidates) < top_k:
                fallback_sql = f"""
                    SELECT
                        kc.id,
                        kc.source_id,
                        kc.course_id,
                        ks.course_name,
                        ks.title,
                        ks.source_type,
                        kc.chunk_index,
                        kc.content,
                        kc.metadata_json,
                        kc.embedding_blob
                    FROM knowledge_chunks kc
                    JOIN knowledge_sources ks ON ks.id = kc.source_id
                    WHERE {where_scope}
                    ORDER BY ks.id DESC, kc.chunk_index ASC
                    LIMIT ?
                """  # noqa: S608
                rows = db.execute(fallback_sql, [*scope_params, top_k * 3]).fetchall()
                for r in rows:
                    cid_num = int(r[0])
                    if cid_num in candidates:
                        continue
                    try:
                        meta = json.loads(r[8]) if r[8] else {}
                    except (TypeError, ValueError):
                        meta = {}
                    chunk = RetrievedChunk(
                        chunk_id=cid_num,
                        source_id=int(r[1]),
                        course_id=str(r[2]),
                        course_name=str(r[3]),
                        source_title=str(r[4]),
                        source_type=str(r[5]),
                        chunk_index=int(r[6]),
                        content=str(r[7]),
                        metadata=meta,
                        score=0.05,
                    )
                    candidates[cid_num] = (chunk, r[9])

        if not candidates:
            return [], False

        # 3. Re-ranking Semántico Híbrido con gemini-embedding-001 (Matryoshka 768d) si hay cuota RPM y API Key
        should_embed = (
            self.settings.use_semantic_embeddings if use_embeddings is None else use_embeddings
        )
        api_key = self._get_api_key()
        used_semantic = False

        if should_embed and api_key and not self.rate_limiter.near_limit():
            try:
                used_semantic = self._rerank_with_embeddings(api_key, question, candidates)
            except Exception:  # noqa: BLE001 — Si falla o hay límite de cuota, FTS5 local responde en 1 llamada
                used_semantic = False

        ranked = sorted(
            (item[0] for item in candidates.values()),
            key=lambda c: (-c.score, c.source_id, c.chunk_index),
        )
        return ranked[:top_k], used_semantic

    def _rerank_with_embeddings(
        self,
        api_key: str,
        question: str,
        candidates: dict[int, tuple[RetrievedChunk, bytes | None]],
    ) -> bool:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        # Generar embedding de la consulta con dimensionalidad Matryoshka = 768
        self.rate_limiter.record_call()
        q_resp = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=question,
            config=types.EmbedContentConfig(output_dimensionality=768),
        )
        if not getattr(q_resp, "embeddings", None):
            return False
        query_vec = np.asarray(q_resp.embeddings[0].values, dtype=np.float32)

        # Calcular y cachear embeddings faltantes de los candidatos si tenemos presupuesto RPM
        missing_ids: list[int] = []
        missing_texts: list[str] = []
        vectors: dict[int, np.ndarray] = {}

        for cid_num, (chunk, blob) in candidates.items():
            vec = deserialize_embedding(blob)
            if vec is not None and vec.shape == query_vec.shape:
                vectors[cid_num] = vec
            else:
                missing_ids.append(cid_num)
                missing_texts.append(chunk.content[:6000])

        if missing_ids and not self.rate_limiter.near_limit(reserve_for_generation=2):
            self.rate_limiter.record_call()
            batch_resp = client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=missing_texts,
                config=types.EmbedContentConfig(output_dimensionality=768),
            )
            if getattr(batch_resp, "embeddings", None):
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    for cid_num, emb in zip(missing_ids, batch_resp.embeddings, strict=False):
                        vec = np.asarray(emb.values, dtype=np.float32)
                        vectors[cid_num] = vec
                        db.execute(
                            "UPDATE knowledge_chunks SET embedding_blob = ? WHERE id = ?",
                            (serialize_embedding(vec), cid_num),
                        )

        if not vectors:
            return False

        # Combinar score BM25 normalizado + similitud coseno
        max_lex = max((c.score for c, _ in candidates.values()), default=1.0) or 1.0
        for cid_num, (chunk, _) in candidates.items():
            norm_lex = chunk.score / max_lex
            vec = vectors.get(cid_num)
            if vec is not None:
                sim = max(0.0, cosine_similarity(vec, query_vec))
                chunk.score = 0.55 * norm_lex + 0.45 * sim
            else:
                chunk.score = 0.55 * norm_lex
        return True

    @staticmethod
    def format_context_block(chunks: list[RetrievedChunk]) -> str:
        """Construye el bloque <CONTEXTO_BASE_DE_DATOS> con etiquetas exactas de curso y fuente."""
        if not chunks:
            return "<CONTEXTO_BASE_DE_DATOS>\n(Sin fuentes disponibles en la selección actual)\n</CONTEXTO_BASE_DE_DATOS>"
        parts = ["<CONTEXTO_BASE_DE_DATOS>"]
        for idx, ch in enumerate(chunks, start=1):
            extra_info = []
            if ch.metadata.get("page"):
                extra_info.append(f"Página {ch.metadata['page']}")
            if ch.metadata.get("slide"):
                extra_info.append(f"Diapositiva {ch.metadata['slide']}")
            if ch.metadata.get("approx_minute"):
                extra_info.append(f"Minuto aprox. {ch.metadata['approx_minute']}")
            if ch.metadata.get("section"):
                extra_info.append(f"Sección: {ch.metadata['section']}")
            loc = f" ({', '.join(extra_info)})" if extra_info else ""
            parts.append(
                f"--- FRAGMENTO {idx} | [Curso: {ch.course_name} | Fuente: {ch.source_title}]{loc} ---\n"
                f"{ch.content}\n"
            )
        parts.append("</CONTEXTO_BASE_DE_DATOS>")
        return "\n".join(parts)

    def _model_fallback_chain(self, preferred_model: str | None = None) -> list[str]:
        chosen = (preferred_model or self.settings.gemini_model or DEFAULT_GEMINI_MODEL).strip()
        chain = [chosen] if chosen else []
        for m in FREE_TIER_MODELS:
            if m not in chain:
                chain.append(m)
        return chain

    def ask(
        self,
        question: str,
        course_id: str | None = None,
        all_courses: bool = False,
        active_source_ids: list[int] | None = None,
        weeks: list[int] | None = None,
        preferred_model: str | None = None,
        top_k: int = 8,
        on_chunk: Callable[[str], None] | None = None,
    ) -> RAGResponse:
        """Ejecuta el flujo RAG completo: recuperación híbrida + generación con blindaje de contexto
        y rotación automática de modelos gratuitos en la nube ante errores 429 o restricciones."""
        question = (question or "").strip()
        if not question:
            raise ValueError("Escribe una pregunta para consultar tus fuentes.")

        chunks, used_embeddings = self.retrieve(
            question=question,
            course_id=course_id,
            all_courses=all_courses,
            active_source_ids=active_source_ids,
            weeks=weeks,
            top_k=top_k,
        )

        if not chunks:
            api_key = self._get_api_key()
            if not api_key:
                return RAGResponse(
                    answer=NO_INFO_RESPONSE,
                    citations=[],
                    chunks=[],
                    model_used="local-fts5",
                    used_embeddings=False,
                )
            context_block = ""
            prompt = (
                f"PREGUNTA DEL ESTUDIANTE:\n{question}\n\n"
                "Instrucción didáctica: Actualmente no hay clases transcritas ni documentos marcados en la app sobre este tema específico. "
                "Responde a la duda del estudiante con máxima claridad, pedagogía y ejemplos prácticos, en un tono accesible y fácil de comprender. "
                "Incluye al inicio esta nota:\n"
                "> 💡 *Nota: Esta respuesta se generó con conocimiento general porque no hay apuntes o clases seleccionadas para este tema. "
                "Cuando agregues tus clases o lecturas en Materiales, citaré exactamente lo dicho por tu profesor.*\n\n"
            )
            system_inst = (
                "Eres un tutor universitario claro, empático y pedagógico. "
                "Explica conceptos de manera estructurada, con ejemplos sencillos y comprensibles para estudiantes de cualquier carrera."
            )
        else:
            api_key = self._get_api_key()
            if not api_key:
                raise RuntimeError(
                    "Configura tu GEMINI_API_KEY gratuita de Google AI Studio (aistudio.google.com/apikey) para generar respuestas."
                )
            context_block = self.format_context_block(chunks)
            prompt = (
                f"{context_block}\n\n"
                f"PREGUNTA DEL ESTUDIANTE:\n{question}\n\n"
                "Recuerda: responde ÚNICAMENTE con base en <CONTEXTO_BASE_DE_DATOS> y cita cada idea con "
                "`[Curso: <Nombre> | Fuente: <Título de Clase o Archivo>]`."
            )
            system_inst = STRICT_SYSTEM_PROMPT

        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        gen_config = types.GenerateContentConfig(
            system_instruction=system_inst,
            temperature=0.2 if not chunks else 0.1,
        )

        models_to_try = self._model_fallback_chain(preferred_model)
        last_error: Exception | None = None
        saw_rate_limit = False
        retry_seconds = 30

        for model_name in models_to_try:
            try:
                self.rate_limiter.record_call()
                if on_chunk is not None:
                    collected: list[str] = []
                    for part in client.models.generate_content_stream(
                        model=model_name,
                        contents=prompt,
                        config=gen_config,
                    ):
                        piece = getattr(part, "text", "") or ""
                        if piece:
                            collected.append(piece)
                            on_chunk(piece)
                    answer_text = "".join(collected).strip()
                else:
                    resp = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=gen_config,
                    )
                    answer_text = (getattr(resp, "text", "") or "").strip()

                if not answer_text:
                    answer_text = NO_INFO_RESPONSE

                if preferred_model and model_name != preferred_model:
                    answer_text += (
                        f"\n\n> 💡 *Nota: `{preferred_model}` tenía alta demanda temporal en los servidores de Google; "
                        f"la respuesta se completó automáticamente con `{model_name}`.*"
                    )

                citations = []
                seen_cit = set()
                for ch in chunks:
                    key = (ch.course_name, ch.source_title)
                    if key not in seen_cit:
                        seen_cit.add(key)
                        citations.append(
                            {
                                "course_id": ch.course_id,
                                "course_name": ch.course_name,
                                "source_title": ch.source_title,
                                "source_type": ch.source_type,
                            }
                        )

                return RAGResponse(
                    answer=answer_text,
                    citations=citations,
                    chunks=chunks,
                    model_used=model_name,
                    used_embeddings=used_embeddings,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                err_str = str(exc)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                    saw_rate_limit = True
                    m = re.search(r"retry in (\d+)", err_str, flags=re.I)
                    if m:
                        retry_seconds = max(5, min(120, int(m.group(1))))
                    continue
                if (
                    "404" in err_str
                    or "NOT_FOUND" in err_str
                    or "not found" in err_str.lower()
                    or "not supported" in err_str.lower()
                    or "permission" in err_str.lower()
                    or "403" in err_str
                    or "503" in err_str
                    or "500" in err_str
                    or "502" in err_str
                    or "504" in err_str
                    or "UNAVAILABLE" in err_str
                    or "unavailable" in err_str.lower()
                    or "high demand" in err_str.lower()
                    or "overloaded" in err_str.lower()
                    or "capacity" in err_str.lower()
                ):
                    # Google model experiencing temporary capacity demand or restriction: rotate to fallback model
                    continue
                break

        if saw_rate_limit:
            raise RateLimitExceededError(
                f"Se alcanzó el límite gratuito por minuto (429 RESOURCE_EXHAUSTED) en los modelos disponibles. "
                f"Espera {retry_seconds} segundos e inténtalo de nuevo; tu pregunta sigue escrita.",
                retry_after=retry_seconds,
            )

        err_msg = redact(last_error or "Error desconocido")
        if (
            "503" in err_msg
            or "UNAVAILABLE" in err_msg
            or "unavailable" in err_msg.lower()
            or "high demand" in err_msg.lower()
        ):
            raise RuntimeError(
                "Los servidores de Google Gemini están experimentando una alta demanda temporal (503 UNAVAILABLE). "
                "Por favor, intenta enviar tu pregunta nuevamente en unos momentos."
            )

        raise RuntimeError("Error al consultar Gemini API: " + err_msg)
