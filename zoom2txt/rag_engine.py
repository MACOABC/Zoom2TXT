"""Motor de conocimiento de Zoom2TXT: ingesta de transcripciones y materiales,
recuperación híbrida (FTS5 BM25 + embeddings opcionales fusionados con RRF) y
respuestas ancladas a las fuentes con citas numeradas.

Qué hace distinto la versión 3:

* **Contexto completo cuando cabe.** Si las fuentes marcadas son pocas (una o
  varias clases), el modelo recibe el texto íntegro en lugar de fragmentos: los
  resúmenes y las preguntas amplias dejan de depender de la suerte del buscador.
* **Memoria de conversación.** Las preguntas de seguimiento («¿y eso cómo se
  calcula?») se entienden con los turnos anteriores.
* **Citas verificables.** Cada afirmación lleva ``[n]`` y cada ``n`` apunta al
  pasaje exacto (clase, página, diapositiva o minuto aproximado).
* **Rotación de modelos.** ``ai_gateway`` elige el modelo según la tarea y salta
  los que agotaron su cuota.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from zoom2txt.ai_gateway import (
    EMBEDDING_MODEL,  # noqa: F401 — reexportado para compatibilidad
    AIError,
    GeminiGateway,
    RateLimitExceededError,
)
from zoom2txt.ai_models import AUTO_MODEL, ModelRouter
from zoom2txt.blackboard.registry import Registry, short_name_for
from zoom2txt.cleaning.subtitle_cleaner import vtt_a_texto
from zoom2txt.config import Settings, load_gemini_api_key
from zoom2txt.library import TXT_SUFFIX, _course_from_name
from zoom2txt.storage import StateStore, digest, digest_cached, file_ok

__all__ = [
    "NO_INFO_RESPONSE",
    "STRICT_SYSTEM_PROMPT",
    "SUPPORTED_EXTENSIONS",
    "AIError",
    "RAGEngine",
    "RAGResponse",
    "RateLimitExceededError",
    "RateLimiter",
    "RetrievedChunk",
    "auto_ingest_transcript",
    "build_fts5_query",
    "chunk_sections",
    "chunk_text",
    "extract_sections_from_file",
    "ingest_file",
    "sync_existing_materials",
    "sync_existing_transcripts",
]

_BASE_RULES = (
    "Eres el asistente de estudio integrado en Zoom2TXT. Respondes en español claro y bien "
    "organizado (Markdown: títulos breves, viñetas, negritas para términos clave), como un "
    "compañero que tomó apuntes perfectos de cada clase.\n"
)

STRICT_SYSTEM_PROMPT = _BASE_RULES + (
    "REGLAS:\n"
    "1. Tu única fuente de verdad son las fuentes numeradas del bloque <fuentes>. No uses "
    "conocimiento externo ni inventes datos, fechas, porcentajes o nombres.\n"
    "2. Cita cada afirmación con el número de su fuente entre corchetes, justo después de la "
    "frase que respalda: [1], o [2][5] si la respaldan varias. No cites números que no existan.\n"
    "3. Si las fuentes no contienen la respuesta, dilo sin rodeos: «No encontré esto en las "
    "fuentes seleccionadas», indica qué sí aparece relacionado y qué clase o material podría "
    "faltar. No rellenes con suposiciones.\n"
    "4. Las transcripciones vienen de subtítulos automáticos: puede haber palabras mal "
    "reconocidas. Interpreta con criterio y, si un término es dudoso, señálalo.\n"
    "5. Sé concreto y útil para estudiar: pasos, fórmulas, ejemplos y énfasis del profesor "
    "cuando existan. Sin introducciones ni despedidas."
)

TUTOR_SYSTEM_PROMPT = _BASE_RULES + (
    "REGLAS:\n"
    "1. Prioriza las fuentes numeradas del bloque <fuentes> y cita lo que tomes de ellas con "
    "[n] justo después de cada frase.\n"
    "2. Puedes complementar con conocimiento general cuando ayude a entender mejor, pero "
    "solo en un apartado final titulado «Más allá de tus apuntes», sin citas, para que quede "
    "claro qué no proviene de tus clases.\n"
    "3. Nunca atribuyas a las fuentes algo que no dicen, ni inventes fechas o condiciones del "
    "curso.\n"
    "4. Las transcripciones vienen de subtítulos automáticos: interpreta con criterio.\n"
    "5. Sé concreto: pasos, fórmulas y ejemplos. Sin introducciones ni despedidas."
)

GENERAL_SYSTEM_PROMPT = (
    "Eres un tutor universitario claro, empático y pedagógico. Explica en español con "
    "estructura, ejemplos sencillos y pasos. No inventes datos del curso del estudiante "
    "(fechas, sílabos, criterios de evaluación): no los conoces."
)

GENERAL_NOTE = (
    "> **Sin fuentes seleccionadas.** Esta respuesta usa conocimiento general, no tus clases. "
    "Marca fuentes a la izquierda para que cite exactamente lo dicho por tu profesor.\n\n"
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

# Presupuestos de contexto completo, en palabras (≈1.5 tokens por palabra en
# español). Se mantienen por debajo del límite gratuito de tokens por minuto.
FULL_CONTEXT_WORDS = {"chat": 60_000, "deep": 120_000}
SAMPLE_WORDS = 16_000
HISTORY_TURNS = 6
HISTORY_CHARS = 1800

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
    path: str = ""


@dataclass
class RAGResponse:
    answer: str
    citations: list[dict]
    chunks: list[RetrievedChunk]
    model_used: str
    used_embeddings: bool = False
    mode: str = "retrieval"  # full | retrieval | sample | general | none
    passages: list = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    fallback_from: str = ""
    grounding: str = "strict"


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
def extract_sections_from_file(
    path: Path, source_type: str | None = None, cancelled=None
) -> list[SectionData]:
    """Extrae el texto por secciones. ``cancelled`` permite abandonar un PDF o
    una presentación larga entre páginas (p. ej. al cerrar la aplicación)."""
    path = Path(path)

    def check_cancel() -> None:
        if cancelled is not None and cancelled():
            raise InterruptedError("Indexación cancelada.")

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
        import logging

        from pypdf import PdfReader

        logging.getLogger("pypdf").setLevel(logging.ERROR)

        reader = PdfReader(str(path))
        sections = []
        for idx, page in enumerate(reader.pages, start=1):
            check_cancel()
            text = (page.extract_text() or "").strip()
            if text:
                sections.append(SectionData(text=text, metadata={"page": idx}))
        return sections

    if stype == "pptx" or ext == ".pptx":
        from pptx import Presentation

        prs = Presentation(str(path))
        sections = []
        for idx, slide in enumerate(prs.slides, start=1):
            check_cancel()
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
class CourseResolver:
    """Relaciona un archivo con su curso usando lo que la app ya sabe. Carga los
    catálogos una sola vez, así sincronizar cientos de archivos no repite
    consultas por cada uno."""

    def __init__(self, store: StateStore):
        self.known_courses: dict[str, str] = {}
        self.short_to_course: dict[str, tuple[str, str]] = {}
        meta = store.get("meta", "current_courses")
        for choice in meta.get("choices", []):
            if choice.get("id") and choice.get("name"):
                self._remember(str(choice["id"]), str(choice["name"]), override=True)
        for course in store.list("course"):
            if course.get("id") and course.get("name"):
                self._remember(str(course["id"]), str(course["name"]), override=True)
        for course in store.list_rag_courses():
            if course.get("id") and course.get("name"):
                self._remember(str(course["id"]), str(course["name"]), override=False)
        self.recordings = store.list("recording")
        self.jobs = store.list("job")
        self.catalog = store.list("catalog")

    def _remember(self, cid: str, name: str, override: bool) -> None:
        short = short_name_for(name).lower()
        if override:
            self.known_courses[cid] = name
            self.short_to_course[short] = (cid, name)
        else:
            self.known_courses.setdefault(cid, name)
            self.short_to_course.setdefault(short, (cid, name))

    def resolve(self, file_path: Path, course_id: str = "", course_name: str = "") -> tuple[str, str]:
        cid = str(course_id or "").strip()
        cname = str(course_name or "").strip()
        if cid and not cname and cid in self.known_courses:
            cname = self.known_courses[cid]
        if cid and cname:
            return cid, cname

        resolved_str = str(file_path.resolve()) if file_path.exists() else str(file_path)
        if file_path.name.endswith(TXT_SUFFIX):
            stem = file_path.name[: -len(TXT_SUFFIX)]
        else:
            stem = file_path.stem

        for rec in self.recordings:
            rec_path = rec.get("path", "")
            rec_name = rec.get("name", "")
            if (rec_path and Path(rec_path).name == file_path.name) or (rec_name and rec_name == stem):
                rcid = str(rec.get("course_id", "")).strip()
                if rcid:
                    cid = cid or rcid
                    cname = cname or self.known_courses.get(rcid, "")
                    break

        if not cid:
            for job in self.jobs:
                job_txt = job.get("txt", "")
                job_name = job.get("name", "") or job.get("display_name", "")
                if (job_txt and (job_txt == resolved_str or Path(job_txt).name == file_path.name)) or (
                    job_name and job_name == stem
                ):
                    jcid = str(job.get("course_id", "")).strip()
                    if jcid:
                        cid = jcid
                        cname = cname or self.known_courses.get(jcid, "")
                        break

        if cid and not cname:
            for cat in self.catalog:
                if str(cat.get("course_id", "")) == cid and cat.get("course_name"):
                    cname = str(cat["course_name"])
                    break

        prefix = _course_from_name(stem)
        if not cid and prefix.lower() in self.short_to_course:
            cid, cname = self.short_to_course[prefix.lower()]
        if not cid:
            cid = prefix or "general"
        if not cname:
            cname = self.known_courses.get(cid, prefix or cid)
        return cid, cname


def _resolve_course_identity(
    settings: Settings,
    store: StateStore,
    file_path: Path,
    course_id: str = "",
    course_name: str = "",
) -> tuple[str, str]:
    """Determina (course_id, course_name) vinculando con los registros existentes de Zoom2TXT."""
    return CourseResolver(store).resolve(file_path, course_id, course_name)


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
    resolver: CourseResolver | None = None,
    cancelled=None,
) -> int | None:
    """Indexa un archivo de transcripción o material en knowledge_sources y knowledge_chunks.
    Evita re-indexar si el hash SHA-256 no ha cambiado."""
    path = Path(file_path)
    if not file_ok(path):
        return None

    store = store or StateStore(settings)
    resolved_path = str(path.resolve())
    try:
        current_hash = digest_cached(path)
    except OSError:
        current_hash = digest(path)

    cid, cname = (resolver or CourseResolver(store)).resolve(path, course_id, course_name)
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
            if not str(course_id or "").strip() and saved_cid and saved_cid != "general":
                # Sin curso explícito (p. ej. la sincronización de carpetas) se respeta
                # el curso ya asignado: una suposición por nombre de archivo nunca
                # mueve una fuente que el estudiante o el registro ubicaron antes.
                cid, cname = str(saved_cid), str(saved_cname or cname)
            chunk_count = db.execute(
                "SELECT COUNT(*) FROM knowledge_chunks WHERE source_id = ?",
                (source_id,),
            ).fetchone()[0]
            if saved_hash == current_hash and chunk_count > 0:
                changed = (cid and cid != saved_cid) or (cname and cname != saved_cname)
                if changed:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute(
                        "UPDATE knowledge_sources SET course_id = ?, course_name = ?, namespace = ? WHERE id = ?",
                        (cid, cname, store.namespace, source_id),
                    )
                    db.execute(
                        "UPDATE knowledge_chunks SET course_id = ? WHERE source_id = ?",
                        (cid, source_id),
                    )
    if existing and saved_hash == current_hash and chunk_count > 0:
        if changed:
            store.upsert_course(cid, cname)
        return int(source_id)

    sections = extract_sections_from_file(path, stype, cancelled)
    chunks = chunk_sections(sections)
    if not chunks:
        return None
    word_count = sum(len((section.text or "").split()) for section in sections)

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
                    title = ?, file_hash = ?, created_at = ?, word_count = ?
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
                    word_count,
                    source_id,
                ),
            )
            db.execute("DELETE FROM knowledge_chunks WHERE source_id = ?", (source_id,))
        else:
            cur = db.execute(
                """
                INSERT INTO knowledge_sources
                    (namespace, course_id, course_name, source_type, title, file_path, file_hash,
                     is_active, created_at, word_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
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
                    word_count,
                ),
            )
            source_id = int(cur.lastrowid)

        db.executemany(
            """
            INSERT INTO knowledge_chunks
                (source_id, course_id, chunk_index, content, metadata_json, embedding_blob)
            VALUES (?, ?, ?, ?, ?, NULL)
            """,
            [
                (
                    source_id,
                    cid,
                    chunk.chunk_index,
                    chunk.content,
                    json.dumps(
                        {
                            **chunk.metadata,
                            "course_name": chunk.metadata.get("course_name", cname),
                            "source_title": chunk.metadata.get("source_title", doc_title),
                            "source_type": chunk.metadata.get("source_type", stype),
                        },
                        ensure_ascii=False,
                    ),
                )
                for chunk in chunks
            ],
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
    cancelled: Callable[[], bool] | None = None,
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
    resolver = CourseResolver(store)

    for idx, (path, cid, cname, title) in enumerate(candidates, start=1):
        if cancelled is not None and cancelled():
            break
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
                resolver=resolver,
                cancelled=cancelled,
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
    cancelled: Callable[[], bool] | None = None,
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
        folder_index = {
            f"{short_name_for(kcname)}_{kcid.strip('_')}": (kcid, kcname)
            for kcid, kcname in known_courses.items()
        }
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
                if parent.name in folder_index:
                    inferred_cid, inferred_cname = folder_index[parent.name]
                    break
                for kcid, kcname in known_courses.items():
                    if parent.name.endswith("_" + kcid.strip("_")):
                        inferred_cid, inferred_cname = kcid, kcname
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
    resolver = CourseResolver(store)
    for idx, (path, cid, cname) in enumerate(candidates, start=1):
        if cancelled is not None and cancelled():
            break
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
                resolver=resolver,
                cancelled=cancelled,
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


_FOLLOW_UP = re.compile(
    r"^(y|pero|entonces|adem[aá]s|tambi[eé]n|o sea)\b|"
    r"\b(eso|esto|esa|ese|aquello|lo anterior|lo mismo|ah[ií]|al respecto|de eso|"
    r"dame m[aá]s|expl[ií]ca(lo|me)? (m[aá]s|mejor)|un ejemplo|otro ejemplo|"
    r"m[aá]s detalle|profundiza|contin[uú]a|sigue)\b",
    flags=re.I,
)


def search_query(question: str, history: list[dict] | None) -> str:
    """Consulta de búsqueda para una pregunta: si es un seguimiento corto o usa
    referencias («eso», «lo anterior»), se le suma la pregunta previa."""
    question = (question or "").strip()
    if not history:
        return question
    previous = next(
        (m.get("content", "") for m in reversed(history) if m.get("role") == "user"), ""
    )
    if previous and (len(question.split()) < 7 or _FOLLOW_UP.search(question)):
        return f"{question} {previous}"[:600]
    return question


def stitch_chunks(texts: list[str], max_overlap: int = 140) -> str:
    """Une fragmentos consecutivos de una misma fuente quitando el solapamiento
    con que fueron troceados."""
    result: list[str] = []
    for text in texts:
        words = text.split()
        if result and words:
            limit = min(max_overlap, len(result), len(words))
            overlap = 0
            for size in range(limit, 7, -1):
                if result[-size:] == words[:size]:
                    overlap = size
                    break
            words = words[overlap:]
            if overlap == 0:
                result.append("\n\n")
        result.extend(words)
    text = " ".join(result)
    return re.sub(r" ?\n\n ?", "\n\n", text).strip()


def _locator(metadata: dict, source_type: str) -> str:
    parts = []
    if metadata.get("page"):
        parts.append(f"pág. {metadata['page']}")
    if metadata.get("slide"):
        parts.append(f"diap. {metadata['slide']}")
    if metadata.get("approx_minute") and source_type == "transcript":
        parts.append(f"min ~{metadata['approx_minute']}")
    if metadata.get("section"):
        parts.append(str(metadata["section"])[:60])
    return " · ".join(parts)


_CITE = re.compile(r"\[(\d{1,3}(?:\s*[,\-–y]\s*\d{1,3})*)\]")


def cited_numbers(answer: str) -> list[int]:
    """Números de fuente citados en una respuesta, en orden de aparición."""
    numbers: list[int] = []
    for match in _CITE.finditer(answer or ""):
        body = match.group(1)
        for piece in re.split(r"\s*[,y]\s*", body):
            span = re.split(r"\s*[\-–]\s*", piece)
            try:
                values = [int(v) for v in span if v.strip()]
            except ValueError:
                continue
            if len(values) == 2 and 0 < values[1] - values[0] <= 12:
                values = list(range(values[0], values[1] + 1))
            for value in values:
                if value not in numbers:
                    numbers.append(value)
    return numbers


@dataclass
class Passage:
    """Texto que se le entrega al modelo con un número citable."""

    number: int
    source_id: int
    course_id: str
    course_name: str
    title: str
    source_type: str
    text: str
    locator: str = ""
    path: str = ""
    chunk_ids: list[int] = field(default_factory=list)
    whole: bool = False

    def citation(self) -> dict:
        return {
            "n": self.number,
            "source_id": self.source_id,
            "course_id": self.course_id,
            "course_name": self.course_name,
            "source_title": self.title,
            "source_type": self.source_type,
            "locator": self.locator,
            "path": self.path,
            "snippet": self.text[:700],
            "whole": self.whole,
        }


@dataclass
class ContextPlan:
    mode: str  # 'full' | 'retrieval' | 'sample' | 'none'
    passages: list[Passage] = field(default_factory=list)
    chunks: list[RetrievedChunk] = field(default_factory=list)
    used_embeddings: bool = False
    words: int = 0


class RAGEngine:
    """Recuperación híbrida y generación anclada a las fuentes del estudiante."""

    def __init__(
        self,
        settings: Settings | None = None,
        store: StateStore | None = None,
        rate_limiter: RateLimiter | None = None,
    ):
        self.settings = settings or Settings.load()
        self.store = store or StateStore(self.settings)
        self.rate_limiter = rate_limiter or RateLimiter(max_rpm=10)
        self.router = ModelRouter(self.store)
        self.gateway = GeminiGateway(self._get_api_key, self.router)

    def _get_api_key(self) -> str:
        return (self.settings.gemini_api_key or load_gemini_api_key() or "").strip()

    # ── Ámbito ────────────────────────────────────────────────────
    def _scoped_source_ids(
        self,
        course_id: str | None,
        all_courses: bool,
        active_source_ids: list[int] | None,
        weeks: list[int] | None,
    ) -> list[int] | None:
        """Aplica el filtro de semanas sobre la selección. None = sin lista explícita."""
        if not weeks:
            return active_source_ids
        target_weeks = {int(w) for w in weeks}
        scoped_sources = self.store.list_knowledge_sources(
            course_id=course_id, all_courses=all_courses, active_only=True
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
            allowed = {int(sid) for sid in active_source_ids}
            return [sid for sid in week_ids if sid in allowed]
        return week_ids

    def _scope_sql(
        self, course_id: str | None, all_courses: bool, source_ids: list[int] | None
    ) -> tuple[str, list]:
        clauses = ["(ks.namespace = ? OR ks.namespace = '')", "ks.is_active = 1"]
        params: list = [self.store.namespace]
        if not all_courses:
            clauses.append("ks.course_id = ?")
            clauses.append("kc.course_id = ?")
            params.extend([str(course_id), str(course_id)])
        if source_ids is not None:
            placeholders = ",".join("?" for _ in source_ids)
            clauses.append(f"ks.id IN ({placeholders})")
            params.extend(int(sid) for sid in source_ids)
        return " AND ".join(clauses), params

    @staticmethod
    def _row_to_chunk(row, score: float) -> RetrievedChunk:
        try:
            meta = json.loads(row[8]) if row[8] else {}
        except (TypeError, ValueError):
            meta = {}
        return RetrievedChunk(
            chunk_id=int(row[0]),
            source_id=int(row[1]),
            course_id=str(row[2]),
            course_name=str(row[3]),
            source_title=str(row[4]),
            source_type=str(row[5]),
            chunk_index=int(row[6]),
            content=str(row[7]),
            metadata=meta if isinstance(meta, dict) else {},
            score=score,
            path=str(row[10] or ""),
        )

    _COLUMNS = (
        "kc.id, kc.source_id, kc.course_id, ks.course_name, ks.title, ks.source_type, "
        "kc.chunk_index, kc.content, kc.metadata_json, kc.embedding_blob, ks.file_path"
    )

    # ── Recuperación ──────────────────────────────────────────────
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
        source_ids = self._scoped_source_ids(course_id, all_courses, active_source_ids, weeks)
        if source_ids is not None and len(source_ids) == 0:
            return [], False

        where_scope, scope_params = self._scope_sql(course_id, all_courses, source_ids)
        fts_query = build_fts5_query(question)
        candidates: dict[int, tuple[RetrievedChunk, bytes | None]] = {}
        lexical: dict[int, float] = {}
        pool = max(top_k * 4, 24)

        with self.store.connect() as db:
            # 1. Búsqueda léxica con SQLite FTS5 (BM25)
            if fts_query:
                try:
                    rows = db.execute(
                        f"""
                        SELECT {self._COLUMNS}, bm25(knowledge_chunks_fts) AS rank_score
                        FROM knowledge_chunks_fts
                        JOIN knowledge_chunks kc ON kc.id = knowledge_chunks_fts.rowid
                        JOIN knowledge_sources ks ON ks.id = kc.source_id
                        WHERE knowledge_chunks_fts MATCH ?
                          AND {where_scope}
                        ORDER BY rank_score ASC
                        LIMIT ?
                        """,  # noqa: S608 — solo se interpolan cláusulas internas
                        [fts_query, *scope_params, pool],
                    ).fetchall()
                except Exception:  # noqa: BLE001 — una consulta FTS rara no debe romper el chat
                    rows = []
                for idx, row in enumerate(rows):
                    # bm25() es negativo: más negativo = más relevante.
                    raw_bm25 = float(row[11]) if row[11] is not None else 0.0
                    chunk = self._row_to_chunk(row, 1.0 / (1.0 + idx) + max(0.0, -raw_bm25) * 0.1)
                    candidates[chunk.chunk_id] = (chunk, row[9])
                    lexical[chunk.chunk_id] = max(0.0, -raw_bm25) + 1.0 / (1.0 + idx) * 1e-3

            # 2. Completar con fragmentos del ámbito si la búsqueda trajo pocos
            if len(candidates) < top_k:
                rows = db.execute(
                    f"""
                    SELECT {self._COLUMNS}
                    FROM knowledge_chunks kc
                    JOIN knowledge_sources ks ON ks.id = kc.source_id
                    WHERE {where_scope}
                    ORDER BY ks.id DESC, kc.chunk_index ASC
                    LIMIT ?
                    """,  # noqa: S608
                    [*scope_params, top_k * 3],
                ).fetchall()
                for row in rows:
                    if int(row[0]) not in candidates:
                        chunk = self._row_to_chunk(row, 0.05)
                        candidates[chunk.chunk_id] = (chunk, row[9])

        if not candidates:
            return [], False

        # 3. Re-ranking semántico (embeddings) fusionado con la señal léxica
        should_embed = (
            self.settings.use_semantic_embeddings if use_embeddings is None else use_embeddings
        )
        used_semantic = False
        if should_embed and self._get_api_key() and not self.rate_limiter.near_limit():
            try:
                used_semantic = self._rerank_with_embeddings(question, candidates, lexical)
            except Exception:  # noqa: BLE001 — sin cuota o sin red: responde el índice local
                used_semantic = False

        ranked = sorted(
            (item[0] for item in candidates.values()),
            key=lambda c: (-c.score, c.source_id, c.chunk_index),
        )
        return ranked[:top_k], used_semantic

    def _rerank_with_embeddings(
        self,
        question: str,
        candidates: dict[int, tuple[RetrievedChunk, bytes | None]],
        lexical: dict[int, float],
    ) -> bool:
        self.rate_limiter.record_call()
        query_vec = self.gateway.embed([question], task_type="RETRIEVAL_QUERY")[0]

        vectors: dict[int, np.ndarray] = {}
        missing_ids: list[int] = []
        missing_texts: list[str] = []
        for chunk_id, (chunk, blob) in candidates.items():
            vec = deserialize_embedding(blob)
            if vec is not None and vec.shape == query_vec.shape:
                vectors[chunk_id] = vec
            else:
                missing_ids.append(chunk_id)
                missing_texts.append(chunk.content[:6000])

        if missing_ids and not self.rate_limiter.near_limit(reserve_for_generation=2):
            self.rate_limiter.record_call()
            fresh = self.gateway.embed(missing_texts, task_type="RETRIEVAL_DOCUMENT")
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                for chunk_id, vec in zip(missing_ids, fresh, strict=False):
                    vectors[chunk_id] = vec
                    db.execute(
                        "UPDATE knowledge_chunks SET embedding_blob = ? WHERE id = ?",
                        (serialize_embedding(vec), chunk_id),
                    )
        if not vectors:
            return False

        similarity = {
            chunk_id: max(0.0, cosine_similarity(vec, query_vec)) for chunk_id, vec in vectors.items()
        }
        # Combinación convexa de señales normalizadas: BM25 relativo al mejor
        # resultado léxico y coseno reescalado al rango observado. Así un pasaje
        # que responde con otras palabras puede superar a uno que solo comparte
        # un término, y viceversa.
        best_lexical = max(lexical.values(), default=0.0) or 1.0
        low, high = min(similarity.values()), max(similarity.values())
        spread = high - low
        for chunk_id, (chunk, _) in candidates.items():
            lexical_part = lexical.get(chunk_id, 0.0) / best_lexical
            sim = similarity.get(chunk_id)
            if sim is None:
                semantic_part = 0.0
            elif spread > 1e-6:
                semantic_part = (sim - low) / spread
            else:
                semantic_part = 1.0 if sim > 0 else 0.0
            chunk.score = 0.45 * lexical_part + 0.55 * semantic_part
        return True

    # ── Construcción del contexto ─────────────────────────────────
    def scope_sources(
        self,
        course_id: str | None,
        all_courses: bool,
        active_source_ids: list[int] | None,
        weeks: list[int] | None = None,
    ) -> list[dict]:
        """Fuentes activas del ámbito, tras aplicar selección y semanas."""
        if not all_courses and not course_id:
            return []
        sources = self.store.list_knowledge_sources(
            course_id=course_id, all_courses=all_courses, active_only=True
        )
        source_ids = self._scoped_source_ids(course_id, all_courses, active_source_ids, weeks)
        if source_ids is not None:
            allowed = set(source_ids)
            sources = [s for s in sources if int(s["id"]) in allowed]
        return sources

    def _source_words(self, source_ids: list[int]) -> dict[int, int]:
        if not source_ids:
            return {}
        placeholders = ",".join("?" for _ in source_ids)
        with self.store.connect() as db:
            rows = db.execute(
                f"""
                SELECT ks.id, ks.word_count,
                       (SELECT COALESCE(SUM(LENGTH(content) - LENGTH(REPLACE(content, ' ', '')) + 1), 0)
                          FROM knowledge_chunks WHERE source_id = ks.id)
                FROM knowledge_sources ks WHERE ks.id IN ({placeholders})
                """,  # noqa: S608
                [int(s) for s in source_ids],
            ).fetchall()
        # Sin word_count (fuentes indexadas antes de v3) se estima desde los
        # fragmentos, descontando su solapamiento.
        return {int(r[0]): int(r[1]) or int(int(r[2]) * 0.87) for r in rows}

    def _load_whole_sources(self, sources: list[dict]) -> list[Passage]:
        if not sources:
            return []
        ids = [int(s["id"]) for s in sources]
        placeholders = ",".join("?" for _ in ids)
        with self.store.connect() as db:
            rows = db.execute(
                f"SELECT source_id, id, content FROM knowledge_chunks WHERE source_id IN ({placeholders}) "  # noqa: S608
                "ORDER BY source_id, chunk_index",
                ids,
            ).fetchall()
        by_source: dict[int, list[tuple[int, str]]] = {}
        for source_id, chunk_id, content in rows:
            by_source.setdefault(int(source_id), []).append((int(chunk_id), str(content)))
        ordered = sorted(
            sources,
            key=lambda s: (
                str(s.get("course_name", "")).casefold(),
                infer_source_week(s.get("title", "")) or 99,
                str(s.get("title", "")).casefold(),
            ),
        )
        passages: list[Passage] = []
        for source in ordered:
            parts = by_source.get(int(source["id"]), [])
            if not parts:
                continue
            passages.append(
                Passage(
                    number=len(passages) + 1,
                    source_id=int(source["id"]),
                    course_id=str(source["course_id"]),
                    course_name=str(source["course_name"]),
                    title=str(source["title"]),
                    source_type=str(source["source_type"]),
                    text=stitch_chunks([text for _, text in parts]),
                    locator="documento completo" if source["source_type"] != "transcript" else "clase completa",
                    path=str(source.get("file_path", "")),
                    chunk_ids=[cid for cid, _ in parts],
                    whole=True,
                )
            )
        return passages

    @staticmethod
    def passages_from_chunks(chunks: list[RetrievedChunk]) -> list[Passage]:
        """Agrupa fragmentos contiguos de una misma fuente en pasajes numerados,
        conservando el orden de relevancia."""
        groups: list[list[RetrievedChunk]] = []
        placed: dict[tuple[int, int], int] = {}
        for chunk in chunks:
            neighbor = next(
                (
                    placed[(chunk.source_id, index)]
                    for index in (chunk.chunk_index - 1, chunk.chunk_index + 1)
                    if (chunk.source_id, index) in placed
                ),
                None,
            )
            if neighbor is None:
                groups.append([chunk])
                placed[(chunk.source_id, chunk.chunk_index)] = len(groups) - 1
            else:
                groups[neighbor].append(chunk)
                placed[(chunk.source_id, chunk.chunk_index)] = neighbor
        passages: list[Passage] = []
        for group in groups:
            group.sort(key=lambda c: c.chunk_index)
            first = group[0]
            passages.append(
                Passage(
                    number=len(passages) + 1,
                    source_id=first.source_id,
                    course_id=first.course_id,
                    course_name=first.course_name,
                    title=first.source_title,
                    source_type=first.source_type,
                    text=stitch_chunks([c.content for c in group]),
                    locator=_locator(first.metadata, first.source_type),
                    path=first.path,
                    chunk_ids=[c.chunk_id for c in group],
                )
            )
        return passages

    def _sample_chunks(self, sources: list[dict], budget_words: int) -> list[RetrievedChunk]:
        """Muestra amplia y pareja de todas las fuentes (para guías y simulacros
        cuando el conjunto no cabe entero)."""
        if not sources:
            return []
        ids = [int(s["id"]) for s in sources]
        placeholders = ",".join("?" for _ in ids)
        with self.store.connect() as db:
            rows = db.execute(
                f"""
                SELECT {self._COLUMNS}
                FROM knowledge_chunks kc JOIN knowledge_sources ks ON ks.id = kc.source_id
                WHERE kc.source_id IN ({placeholders})
                ORDER BY kc.source_id, kc.chunk_index
                """,  # noqa: S608
                ids,
            ).fetchall()
        by_source: dict[int, list] = {}
        for row in rows:
            by_source.setdefault(int(row[1]), []).append(row)
        per_source = max(1, (budget_words // 650) // max(1, len(by_source)))
        picked: list[RetrievedChunk] = []
        for source_rows in by_source.values():
            if len(source_rows) <= per_source:
                chosen = source_rows
            else:
                step = len(source_rows) / per_source
                chosen = [source_rows[int(i * step)] for i in range(per_source)]
            picked.extend(self._row_to_chunk(row, 0.5) for row in chosen)
        return picked

    def build_context(
        self,
        question: str,
        course_id: str | None = None,
        all_courses: bool = False,
        active_source_ids: list[int] | None = None,
        weeks: list[int] | None = None,
        top_k: int = 10,
        context_mode: str = "auto",
        history: list[dict] | None = None,
        task: str = "chat",
    ) -> ContextPlan:
        """Decide qué texto recibe el modelo: fuentes completas si caben, una
        muestra amplia para tareas de estudio, o los fragmentos más relevantes."""
        sources = self.scope_sources(course_id, all_courses, active_source_ids, weeks)
        if not sources:
            return ContextPlan(mode="none")
        words = self._source_words([int(s["id"]) for s in sources])
        total_words = sum(words.values())
        budget = FULL_CONTEXT_WORDS.get(task, FULL_CONTEXT_WORDS["chat"])
        if context_mode == "full" or (context_mode == "auto" and 0 < total_words <= budget):
            passages = self._load_whole_sources(sources)
            if passages:
                return ContextPlan(mode="full", passages=passages, words=total_words)
        if task == "deep":
            chunks = self._sample_chunks(sources, SAMPLE_WORDS)
            return ContextPlan(
                mode="sample",
                passages=self.passages_from_chunks(chunks),
                chunks=chunks,
                words=sum(len(c.content.split()) for c in chunks),
            )
        chunks, used_embeddings = self.retrieve(
            question=search_query(question, history),
            course_id=course_id,
            all_courses=all_courses,
            active_source_ids=[int(s["id"]) for s in sources],
            top_k=top_k,
        )
        return ContextPlan(
            mode="retrieval" if chunks else "none",
            passages=self.passages_from_chunks(chunks),
            chunks=chunks,
            used_embeddings=used_embeddings,
            words=sum(len(c.content.split()) for c in chunks),
        )

    @staticmethod
    def format_sources_block(passages: list[Passage]) -> str:
        """Bloque <fuentes> con cada pasaje numerado y su procedencia."""
        if not passages:
            return "<fuentes>\n(Sin fuentes disponibles en la selección actual)\n</fuentes>"
        kinds = {"transcript": "Clase", "pdf": "PDF", "pptx": "Diapositivas", "docx": "Documento"}
        parts = ["<fuentes>"]
        for passage in passages:
            kind = kinds.get(passage.source_type, "Archivo")
            where = f" · {passage.locator}" if passage.locator else ""
            parts.append(
                f"[{passage.number}] Curso: {passage.course_name} · {kind}: {passage.title}{where}\n"
                f"{passage.text}\n"
            )
        parts.append("</fuentes>")
        return "\n".join(parts)

    @staticmethod
    def format_context_block(chunks: list[RetrievedChunk]) -> str:
        """Compatibilidad: bloque de fuentes a partir de fragmentos recuperados."""
        return RAGEngine.format_sources_block(RAGEngine.passages_from_chunks(chunks))

    def _model_fallback_chain(self, preferred_model: str | None = None, task: str = "chat") -> list[str]:
        return self.router.chain(preferred_model or self.settings.gemini_model, task)

    @staticmethod
    def _history_contents(history: list[dict] | None) -> list[dict]:
        contents: list[dict] = []
        for message in (history or [])[-HISTORY_TURNS:]:
            text = str(message.get("content", "")).strip()
            if not text:
                continue
            role = "user" if message.get("role") == "user" else "model"
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"][0]["text"] += "\n\n" + text[:HISTORY_CHARS]
            else:
                contents.append({"role": role, "parts": [{"text": text[:HISTORY_CHARS]}]})
        while contents and contents[0]["role"] != "user":
            contents.pop(0)
        return contents

    @staticmethod
    def citations_for(answer: str, passages: list[Passage]) -> list[dict]:
        """Citas realmente usadas en la respuesta; si el modelo no numeró nada,
        se listan las fuentes entregadas para no perder la procedencia."""
        by_number = {p.number: p for p in passages}
        used = [by_number[n].citation() for n in cited_numbers(answer) if n in by_number]
        if used:
            return used
        seen: set[int] = set()
        fallback = []
        for passage in passages:
            if passage.source_id not in seen:
                seen.add(passage.source_id)
                fallback.append({**passage.citation(), "implicit": True})
        return fallback[:12]

    # ── Preguntar ─────────────────────────────────────────────────
    def ask(
        self,
        question: str,
        course_id: str | None = None,
        all_courses: bool = False,
        active_source_ids: list[int] | None = None,
        weeks: list[int] | None = None,
        preferred_model: str | None = None,
        top_k: int = 10,
        on_chunk: Callable[[str], None] | None = None,
        history: list[dict] | None = None,
        grounding: str = "strict",
        context_mode: str = "auto",
        cancel: threading.Event | None = None,
        on_reset: Callable[[], None] | None = None,
        task: str = "chat",
        instruction: str = "",
    ) -> RAGResponse:
        """Flujo completo: contexto (completo o recuperado) + generación anclada
        a las fuentes, con rotación automática de modelos."""
        question = (question or "").strip()
        if not question:
            raise ValueError("Escribe una pregunta para consultar tus fuentes.")
        preferred = preferred_model or self.settings.gemini_model or AUTO_MODEL
        plan = self.build_context(
            question, course_id, all_courses, active_source_ids, weeks, top_k,
            context_mode, history, task,
        )  # fmt: skip
        api_key = self._get_api_key()

        if not plan.passages:
            if not api_key:
                return RAGResponse(
                    answer=NO_INFO_RESPONSE,
                    citations=[],
                    chunks=[],
                    model_used="local-fts5",
                    used_embeddings=False,
                    mode="none",
                )
            generation = self.gateway.generate(
                system=GENERAL_SYSTEM_PROMPT,
                contents=[
                    *self._history_contents(history),
                    {"role": "user", "parts": [{"text": question}]},
                ],
                preferred=preferred,
                task=task,
                temperature=0.4,
                on_chunk=on_chunk,
                on_reset=on_reset,
                cancel=cancel,
            )
            return RAGResponse(
                answer=GENERAL_NOTE + (generation.text or NO_INFO_RESPONSE),
                citations=[],
                chunks=[],
                model_used=generation.model,
                mode="general",
                input_tokens=generation.input_tokens,
                output_tokens=generation.output_tokens,
                fallback_from=generation.fallback_from,
                grounding="general",
            )

        if not api_key:
            raise RuntimeError(
                "Configura tu GEMINI_API_KEY gratuita de Google AI Studio (aistudio.google.com/apikey) para generar respuestas."
            )

        def run(current: ContextPlan):
            closing = (
                "Responde solo con lo que dicen las fuentes y cita con [n]."
                if grounding == "strict"
                else "Prioriza las fuentes y cítalas con [n]."
            )
            body = (
                f"{self.format_sources_block(current.passages)}\n\n"
                + (f"TAREA:\n{instruction}\n\n" if instruction else "")
                + f"PREGUNTA DEL ESTUDIANTE:\n{question}\n\n{closing}"
            )
            return self.gateway.generate(
                system=STRICT_SYSTEM_PROMPT if grounding == "strict" else TUTOR_SYSTEM_PROMPT,
                contents=[
                    *self._history_contents(history),
                    {"role": "user", "parts": [{"text": body}]},
                ],
                preferred=preferred,
                task=task,
                temperature=0.1 if grounding == "strict" else 0.3,
                on_chunk=on_chunk,
                on_reset=on_reset,
                cancel=cancel,
            )

        try:
            generation = run(plan)
        except RateLimitExceededError as exc:
            # El contexto completo puede exceder los tokens por minuto de la
            # capa gratuita: se reintenta una vez solo con los fragmentos clave.
            if plan.mode != "full" or context_mode == "full" or exc.daily:
                raise
            plan = self.build_context(
                question, course_id, all_courses, active_source_ids, weeks, top_k,
                "retrieval", history, "chat",
            )  # fmt: skip
            if not plan.passages:
                raise
            if on_reset is not None:
                on_reset()
            generation = run(plan)

        answer = generation.text or NO_INFO_RESPONSE
        return RAGResponse(
            answer=answer,
            citations=self.citations_for(answer, plan.passages),
            chunks=plan.chunks,
            model_used=generation.model,
            used_embeddings=plan.used_embeddings,
            mode=plan.mode,
            passages=plan.passages,
            input_tokens=generation.input_tokens,
            output_tokens=generation.output_tokens,
            fallback_from=generation.fallback_from,
            grounding=grounding,
        )

    # ── Herramientas de estudio ───────────────────────────────────
    def generate_study(
        self,
        tool_key: str,
        course_id: str | None = None,
        all_courses: bool = False,
        active_source_ids: list[int] | None = None,
        weeks: list[int] | None = None,
        preferred_model: str | None = None,
        count: int | None = None,
        focus: str = "",
        cancel: threading.Event | None = None,
        title: str = "",
    ):
        """Genera tarjetas o un simulacro (JSON validado) a partir de las fuentes."""
        from zoom2txt import study

        tool = study.TOOLS[tool_key]
        if tool.kind not in ("flashcards", "quiz"):
            raise ValueError("Esta herramienta se genera en la conversación.")
        if not self._get_api_key():
            raise RuntimeError(
                "Configura tu GEMINI_API_KEY gratuita de Google AI Studio (aistudio.google.com/apikey) para generar respuestas."
            )
        plan = self.build_context(
            focus or tool.label, course_id, all_courses, active_source_ids, weeks,
            context_mode="auto", task="deep",
        )  # fmt: skip
        if not plan.passages:
            raise RuntimeError(
                "Marca al menos una clase o material en Fuentes para generar " + tool.label.lower() + "."
            )
        amount = max(4, min(40, int(count or tool.default_count)))
        task_text = tool.prompt.format(count=amount)
        if focus:
            task_text += f" Céntrate en: {focus}."
        body = (
            f"{self.format_sources_block(plan.passages)}\n\nTAREA:\n{task_text}\n\n"
            f"{study.JSON_FORMAT[tool.kind]} Escribe en español."
        )
        payload, generation = self.gateway.generate_json(
            system=(
                "Eres un docente que prepara material de estudio riguroso a partir de las "
                "fuentes numeradas de <fuentes>. Usa únicamente su contenido; no inventes datos."
            ),
            contents=body,
            preferred=preferred_model or self.settings.gemini_model or AUTO_MODEL,
            task="deep",
            json_schema=study.FLASHCARDS_SCHEMA if tool.kind == "flashcards" else study.QUIZ_SCHEMA,
            cancel=cancel,
        )
        try:
            items = (
                study.parse_flashcards(payload) if tool.kind == "flashcards" else study.parse_quiz(payload)
            )
        except ValueError as exc:
            raise AIError(str(exc) + " Vuelve a intentarlo o prueba con otro modelo.") from exc
        by_number = {p.number: p for p in plan.passages}
        used = sorted({int(i.get("source") or 0) for i in items} & set(by_number))
        return study.StudyResult(
            kind=tool.kind,
            tool=tool.key,
            title=title or tool.label,
            items=items,
            citations=[by_number[n].citation() for n in used],
            model_used=generation.model,
        )

    # ── Mantenimiento ─────────────────────────────────────────────
    def source_text(self, source_id: int) -> str:
        """Texto íntegro de una fuente indexada (para el visor de citas)."""
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT content FROM knowledge_chunks WHERE source_id = ? ORDER BY chunk_index",
                (int(source_id),),
            ).fetchall()
        return stitch_chunks([str(r[0]) for r in rows])

    def search_text(self, query: str, limit: int = 40) -> list[dict]:
        """Búsqueda de texto completo en todo lo indexado, con extracto resaltado.
        No usa la API: funciona sin clave y sin conexión."""
        fts_query = build_fts5_query(query)
        if not fts_query:
            return []
        with self.store.connect() as db:
            try:
                rows = db.execute(
                    """
                    SELECT ks.id, ks.title, ks.course_name, ks.source_type, ks.file_path,
                           kc.chunk_index, kc.metadata_json,
                           snippet(knowledge_chunks_fts, 0, '[[', ']]', ' … ', 28),
                           bm25(knowledge_chunks_fts) AS rank_score
                    FROM knowledge_chunks_fts
                    JOIN knowledge_chunks kc ON kc.id = knowledge_chunks_fts.rowid
                    JOIN knowledge_sources ks ON ks.id = kc.source_id
                    WHERE knowledge_chunks_fts MATCH ?
                      AND (ks.namespace = ? OR ks.namespace = '')
                    ORDER BY rank_score ASC
                    LIMIT ?
                    """,
                    (fts_query, self.store.namespace, int(limit)),
                ).fetchall()
            except Exception:  # noqa: BLE001
                return []
        results = []
        for row in rows:
            try:
                meta = json.loads(row[6]) if row[6] else {}
            except (TypeError, ValueError):
                meta = {}
            results.append(
                {
                    "source_id": int(row[0]),
                    "title": str(row[1]),
                    "course_name": str(row[2]),
                    "source_type": str(row[3]),
                    "path": str(row[4]),
                    "chunk_index": int(row[5]),
                    "locator": _locator(meta if isinstance(meta, dict) else {}, str(row[3])),
                    "snippet": str(row[7] or ""),
                }
            )
        return results

    def warm_embeddings(
        self,
        course_id: str | None = None,
        all_courses: bool = True,
        limit: int = 400,
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Calcula por lotes los embeddings que falten (opcional; acelera y
        mejora las siguientes búsquedas semánticas)."""
        where, params = self._scope_sql(course_id, all_courses, None)
        with self.store.connect() as db:
            rows = db.execute(
                f"""
                SELECT kc.id, kc.content FROM knowledge_chunks kc
                JOIN knowledge_sources ks ON ks.id = kc.source_id
                WHERE {where} AND kc.embedding_blob IS NULL
                ORDER BY kc.id LIMIT ?
                """,  # noqa: S608
                [*params, int(limit)],
            ).fetchall()
        done = 0
        for start in range(0, len(rows), 24):
            if cancelled is not None and cancelled():
                break
            batch = rows[start : start + 24]
            vectors = self.gateway.embed([str(r[1])[:6000] for r in batch], "RETRIEVAL_DOCUMENT")
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.executemany(
                    "UPDATE knowledge_chunks SET embedding_blob = ? WHERE id = ?",
                    [(serialize_embedding(vec), int(r[0])) for r, vec in zip(batch, vectors, strict=True)],
                )
            done += len(batch)
            if progress is not None:
                progress(done, len(rows))
            time.sleep(0.4)
        return done
