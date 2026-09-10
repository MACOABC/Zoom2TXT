"""Biblioteca y estadísticas: reconoce en disco las transcripciones y
los videos ya generados por la app, y calcula métricas para el panel.

Fuentes de verdad, en orden:
- carpeta de transcripciones (los .txt *_Para_NotebookLM.txt)
- carpeta de videos (los .mp4)
- el registro de procesadas (para saber a qué curso pertenece cada una)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from zoom2txt.blackboard.registry import Registry
from zoom2txt.config import Settings

TXT_SUFFIX = "_Para_NotebookLM.txt"
_METRICS_CACHE = {}


def text_metrics(path):
    info = path.stat()
    key = (str(path), info.st_size, info.st_mtime_ns)
    if key in _METRICS_CACHE:
        return _METRICS_CACHE[key]
    words = chars = 0
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            chars += len(line)
            if (
                "-->" in line
                or line.strip().isdigit()
                or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE"))
            ):
                continue
            words += len(re.sub(r"<[^>]*>", "", line).split())
    if len(_METRICS_CACHE) > 10000:
        _METRICS_CACHE.clear()
    _METRICS_CACHE[key] = (words, chars)
    return words, chars


@dataclass
class TxtItem:
    path: Path
    name: str
    words: int
    chars: int
    size: int
    modified: datetime
    course: str = ""
    week: int | None = None


@dataclass
class LibraryStats:
    items: list[TxtItem] = field(default_factory=list)
    videos: int = 0
    videos_size: int = 0
    total_words: int = 0
    total_txt_size: int = 0
    by_course: dict[str, int] = field(default_factory=dict)
    by_week: dict[int, int] = field(default_factory=dict)
    missing_dirs: list[str] = field(default_factory=list)

    @property
    def txt_count(self) -> int:
        return len(self.items)

    @property
    def avg_words(self) -> int:
        return round(self.total_words / len(self.items)) if self.items else 0


def _course_index(registry: Registry) -> dict[str, str]:
    """name-de-archivo → nombre de curso, a partir del registro."""
    idx: dict[str, str] = {}
    data = getattr(registry, "_data", {})
    # Necesitamos el nombre legible del curso; el registro guarda por
    # course_id. Se reconstruye desde los nombres de archivo asignados.
    for course_id, info in data.items():
        for _meeting_id, filename in info.get("meetings", {}).items():
            idx[filename] = course_id
    return idx


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit in ("B", "KB") else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def _course_from_name(name: str) -> str:
    """'AprendizDeMaquina_Semana14' → 'AprendizDeMaquina'."""
    m = re.match(r"(.+?)_(Semana|Clase)\d+", name)
    return m.group(1) if m else name


def _week_from_name(name: str) -> int | None:
    m = re.search(r"_Semana(\d+)", name)
    return int(m.group(1)) if m else None


def scan_library(settings: Settings, cancelled=lambda: False) -> LibraryStats:
    stats = LibraryStats()
    registry = Registry(settings)
    course_names = _course_display_names(registry)

    t_dir = settings.transcripts_dir
    if t_dir.exists():
        for txt in sorted(t_dir.rglob(f"*{TXT_SUFFIX}")):
            if cancelled():
                break
            try:
                if txt.stat().st_size == 0:
                    continue
                words, chars = text_metrics(txt)
            except OSError:
                continue
            name = txt.name[: -len(TXT_SUFFIX)]
            stats.items.append(
                TxtItem(
                    path=txt,
                    name=name,
                    words=words,
                    chars=chars,
                    size=txt.stat().st_size,
                    modified=datetime.fromtimestamp(txt.stat().st_mtime),
                    course=course_names.get(_course_from_name(name), _course_from_name(name)),
                    week=_week_from_name(name),
                )
            )
    else:
        stats.missing_dirs.append(str(t_dir))

    v_dir = settings.videos_dir
    if v_dir.exists():
        vids = list(v_dir.rglob("*.mp4"))
        stats.videos = len(vids)
        stats.videos_size = sum(v.stat().st_size for v in vids)
    else:
        stats.missing_dirs.append(str(v_dir))

    for it in stats.items:
        stats.total_words += it.words
        stats.total_txt_size += it.size
        stats.by_course[it.course] = stats.by_course.get(it.course, 0) + 1
        if it.week:
            stats.by_week[it.week] = stats.by_week.get(it.week, 0) + 1
    return stats


def _course_display_names(registry: Registry) -> dict[str, str]:
    """Prefijo-de-archivo → nombre corto legible. Como el registro no
    guarda el nombre del curso, se usa el propio prefijo (que ya es
    legible: 'AprendizDeMaquina')."""
    names: dict[str, str] = {}
    data = getattr(registry, "_data", {})
    for info in data.values():
        for filename in info.get("meetings", {}).values():
            pref = _course_from_name(filename)
            names.setdefault(pref, pref)
    return names
