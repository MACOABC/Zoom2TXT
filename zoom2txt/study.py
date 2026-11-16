"""Herramientas de estudio del asistente: qué pide cada una, cómo se valida lo
que devuelve el modelo y cómo se guarda o exporta el resultado.

Dos familias:

* **Texto** (resumen, guía, conceptos, fechas): Markdown con citas ``[n]`` que
  va directo a la conversación.
* **Interactivas** (tarjetas y simulacro): JSON validado que la interfaz
  convierte en un mazo para repasar o un examen con nota. Se guardan en la base
  local para repetirlas sin gastar cuota.
"""

from __future__ import annotations

import csv
import io
import time
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class StudyTool:
    key: str
    label: str
    icon: str
    kind: str  # 'markdown' | 'flashcards' | 'quiz'
    hint: str
    prompt: str
    default_count: int = 0


_CITE_RULE = (
    "Cita la fuente de cada punto con su número entre corchetes, por ejemplo [2]. "
    "No inventes nada que no esté en las fuentes."
)

TOOLS: dict[str, StudyTool] = {
    tool.key: tool
    for tool in (
        StudyTool(
            "resumen",
            "Resumen",
            "file-text",
            "markdown",
            "Lo esencial de las fuentes marcadas, en una página.",
            "Escribe un resumen de estudio de las fuentes. Estructura: una idea central en dos "
            "líneas; luego secciones con título por cada tema tratado, con viñetas concretas "
            "(definiciones, pasos, fórmulas, ejemplos que dio el profesor); cierra con «Para "
            "recordar» (3 a 5 viñetas). " + _CITE_RULE,
        ),
        StudyTool(
            "guia",
            "Guía de estudio",
            "graduation-cap",
            "markdown",
            "Temas, conceptos, preguntas probables y un plan de repaso.",
            "Elabora una guía de estudio para preparar el examen con estas fuentes. Incluye, en "
            "este orden: «Mapa de temas» (lista jerárquica), «Conceptos que debes dominar» "
            "(definición breve de cada uno), «Fórmulas y procedimientos» (si los hay), «Lo que "
            "el profesor recalcó» (énfasis, advertencias, pistas de examen), «Preguntas "
            "probables» (8 preguntas abiertas sin respuesta) y «Plan de repaso» en 3 sesiones. "
            + _CITE_RULE,
        ),
        StudyTool(
            "flashcards",
            "Tarjetas",
            "layers",
            "flashcards",
            "Mazo de repaso con anverso y reverso para practicar.",
            "Crea {count} tarjetas de estudio (flashcards) que cubran de forma equilibrada los "
            "conceptos más importantes de las fuentes. El anverso es una pregunta o término "
            "breve; el reverso, una respuesta precisa de 1 a 3 frases. Evita tarjetas triviales "
            "o duplicadas.",
            default_count=16,
        ),
        StudyTool(
            "quiz",
            "Simulacro",
            "list-checks",
            "quiz",
            "Examen de opción múltiple con nota y explicación.",
            "Crea un simulacro de examen de {count} preguntas de opción múltiple basadas en las "
            "fuentes. Cada pregunta tiene exactamente 4 opciones plausibles y una sola correcta; "
            "varía la posición de la correcta. Mezcla comprensión, aplicación y análisis, no "
            "solo memoria. La explicación justifica la respuesta en 1 o 2 frases.",
            default_count=10,
        ),
        StudyTool(
            "conceptos",
            "Conceptos clave",
            "lightbulb",
            "markdown",
            "Glosario de términos, reglas y fórmulas mencionadas.",
            "Extrae un glosario de las fuentes: cada concepto, regla o fórmula en negrita, "
            "seguido de su definición en una o dos frases y, si existe, el ejemplo usado en "
            "clase. Ordénalo de lo más fundamental a lo más específico. " + _CITE_RULE,
        ),
        StudyTool(
            "fechas",
            "Fechas y entregas",
            "calendar-clock",
            "markdown",
            "Evaluaciones, tareas, plazos y avisos del profesor.",
            "Lista todas las fechas, evaluaciones, entregas, porcentajes de nota, requisitos y "
            "avisos administrativos que aparezcan en las fuentes, en una tabla o lista con: qué "
            "es, cuándo, y detalles. Si una fecha se menciona de forma relativa («la próxima "
            "semana»), consérvala tal cual e indica en qué clase se dijo. Si no hay ninguna, "
            "dilo claramente. " + _CITE_RULE,
        ),
    )
}

# Compatibilidad con los botones rápidos anteriores (etiqueta, ayuda, prompt).
QUICK_TOOL_KEYS = ("resumen", "quiz", "conceptos", "fechas")

FLASHCARDS_SCHEMA = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "front": {"type": "string"},
                    "back": {"type": "string"},
                    "source": {"type": "integer"},
                },
                "required": ["front", "back"],
            },
        }
    },
    "required": ["cards"],
}

QUIZ_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "answer": {"type": "integer"},
                    "explanation": {"type": "string"},
                    "source": {"type": "integer"},
                },
                "required": ["question", "options", "answer"],
            },
        }
    },
    "required": ["questions"],
}

JSON_FORMAT = {
    "flashcards": (
        'Devuelve SOLO JSON con esta forma: {"cards": [{"front": "…", "back": "…", '
        '"source": 1}]}. «source» es el número de la fuente de donde sale la tarjeta.'
    ),
    "quiz": (
        'Devuelve SOLO JSON con esta forma: {"questions": [{"question": "…", "options": '
        '["…", "…", "…", "…"], "answer": 0, "explanation": "…", "source": 1}]}. «answer» es '
        "el índice (0 a 3) de la opción correcta y «source» el número de la fuente."
    ),
}


@dataclass
class StudyResult:
    kind: str
    tool: str
    title: str
    items: list[dict] = field(default_factory=list)
    markdown: str = ""
    citations: list[dict] = field(default_factory=list)
    model_used: str = ""
    deck_id: str = ""


def _text(value, limit: int = 600) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse_flashcards(payload) -> list[dict]:
    raw = payload.get("cards") if isinstance(payload, dict) else payload
    cards: list[dict] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        front = _text(item.get("front") or item.get("question") or item.get("term"), 300)
        back = _text(item.get("back") or item.get("answer") or item.get("definition"), 900)
        if not front or not back or front.casefold() in seen:
            continue
        seen.add(front.casefold())
        try:
            source = int(item.get("source") or 0)
        except (TypeError, ValueError):
            source = 0
        cards.append({"front": front, "back": back, "source": source})
    if not cards:
        raise ValueError("El modelo no devolvió tarjetas válidas.")
    return cards


def parse_quiz(payload) -> list[dict]:
    raw = payload.get("questions") if isinstance(payload, dict) else payload
    questions: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        question = _text(item.get("question"), 500)
        options = [_text(o, 300) for o in (item.get("options") or []) if _text(o, 300)]
        if not question or len(options) < 2:
            continue
        options = options[:6]
        answer = item.get("answer", item.get("answer_index", 0))
        if isinstance(answer, str):
            letter = answer.strip()[:1].upper()
            if letter in "ABCDEF" and letter:
                answer = "ABCDEF".index(letter)
            else:
                answer = next(
                    (i for i, option in enumerate(options) if option.casefold() == answer.strip().casefold()),
                    0,
                )
        try:
            answer = int(answer)
        except (TypeError, ValueError):
            answer = 0
        if not 0 <= answer < len(options):
            continue
        try:
            source = int(item.get("source") or 0)
        except (TypeError, ValueError):
            source = 0
        questions.append(
            {
                "question": question,
                "options": options,
                "answer": answer,
                "explanation": _text(item.get("explanation"), 700),
                "source": source,
            }
        )
    if not questions:
        raise ValueError("El modelo no devolvió preguntas válidas.")
    return questions


# ── Persistencia de mazos y simulacros ────────────────────────────
def save_deck(store, result: StudyResult, scope: str) -> str:
    deck_id = result.deck_id or uuid.uuid4().hex[:16]
    store.put(
        "study_deck",
        deck_id,
        deck_type=result.kind,
        tool=result.tool,
        title=result.title,
        scope=scope,
        items=result.items,
        citations=result.citations,
        model=result.model_used,
        created=time.time(),
    )
    result.deck_id = deck_id
    return deck_id


def list_decks(store, kind: str | None = None) -> list[dict]:
    decks = [d for d in store.list("study_deck") if d.get("items")]
    if kind:
        decks = [d for d in decks if d.get("deck_type") == kind]
    return sorted(decks, key=lambda d: -float(d.get("created", 0)))


def record_attempt(store, deck_id: str, score: int, total: int) -> None:
    deck = store.get("study_deck", deck_id)
    if not deck:
        return
    attempts = list(deck.get("attempts", []))[-19:]
    attempts.append({"at": time.time(), "score": int(score), "total": int(total)})
    store.put("study_deck", deck_id, attempts=attempts)


def delete_deck(store, deck_id: str) -> None:
    store.delete("study_deck", deck_id)


# ── Exportación ───────────────────────────────────────────────────
def flashcards_to_csv(cards: list[dict]) -> str:
    """CSV de dos columnas (anverso, reverso) compatible con Anki y Quizlet."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for card in cards:
        writer.writerow([card.get("front", ""), card.get("back", "")])
    return buffer.getvalue()


def flashcards_to_markdown(title: str, cards: list[dict]) -> str:
    lines = [f"# Tarjetas — {title}", ""]
    for index, card in enumerate(cards, start=1):
        lines += [f"**{index}. {card.get('front', '')}**", "", card.get("back", ""), ""]
    return "\n".join(lines)


def quiz_to_markdown(title: str, questions: list[dict], answers: dict[int, int] | None = None) -> str:
    lines = [f"# Simulacro — {title}", ""]
    for index, item in enumerate(questions, start=1):
        lines += [f"## {index}. {item.get('question', '')}", ""]
        for option_index, option in enumerate(item.get("options", [])):
            mark = "x" if option_index == item.get("answer") else " "
            chosen = (
                "  ← tu respuesta"
                if answers is not None and answers.get(index - 1) == option_index
                else ""
            )
            lines.append(f"- [{mark}] {'ABCDEF'[option_index]}) {option}{chosen}")
        if item.get("explanation"):
            lines += ["", f"> {item['explanation']}"]
        lines.append("")
    return "\n".join(lines)
