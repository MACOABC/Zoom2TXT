"""Catálogo, descubrimiento y enrutado de modelos Gemini (capa gratuita, BYOK).

Google retira y renombra modelos varias veces al año y sus cuotas gratuitas son
ajustadas (pocas decenas de solicitudes diarias en los modelos más capaces). En
vez de fijar un modelo, la app:

* parte de un catálogo conocido y lo completa con lo que la propia API dice que
  existe para la clave del usuario (``refresh``);
* en modo **Automático** elige el modelo según la tarea: uno ligero y con mucha
  cuota para conversar, uno más capaz para guías, simulacros y resúmenes;
* recuerda qué modelo agotó su cuota diaria o dejó de existir y lo salta hasta
  que vuelva a estar disponible, sin gastar más solicitudes en comprobarlo;
* lleva la cuenta local de solicitudes por modelo y día (hora del Pacífico, que
  es cuando Google reinicia la cuota). Es un contador propio, no el saldo real.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

AUTO_MODEL = "auto"
PACIFIC = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    tier: str  # 'lite' | 'flash' | 'pro'
    blurb: str = ""

    @property
    def short(self) -> str:
        return self.label.replace("Gemini ", "")


CATALOG: tuple[ModelInfo, ...] = (
    ModelInfo(
        "gemini-3.5-flash-lite",
        "Gemini 3.5 Flash-Lite",
        "lite",
        "Rápido y con la mayor cuota gratuita diaria. Ideal para conversar.",
    ),
    ModelInfo(
        "gemini-3.8-flash",
        "Gemini 3.8 Flash",
        "flash",
        "Más capaz para guías, simulacros y resúmenes. Cuota diaria reducida.",
    ),
    ModelInfo(
        "gemini-3.6-flash",
        "Gemini 3.6 Flash",
        "flash",
        "Equilibrado. Cuota diaria reducida.",
    ),
    ModelInfo(
        "gemini-4-hydrogen",
        "Gemini 4 Hydrogen",
        "pro",
        "El más avanzado disponible en la capa gratuita.",
    ),
    ModelInfo(
        "gemini-3.1-flash-lite",
        "Gemini 3.1 Flash-Lite",
        "lite",
        "Alternativa ligera con cuota amplia.",
    ),
    ModelInfo(
        "gemini-2.5-flash-lite",
        "Gemini 2.5 Flash-Lite",
        "lite",
        "Generación anterior, aún en servicio.",
    ),
)
CATALOG_IDS: tuple[str, ...] = tuple(m.id for m in CATALOG)
_BY_ID = {m.id: m for m in CATALOG}

# Orden de preferencia por tipo de tarea en modo Automático.
_TASK_ORDER = {
    "chat": ("lite", "flash", "pro"),
    "deep": ("flash", "pro", "lite"),
}
# Familias que la API lista pero no sirven para texto→texto.
_EXCLUDED = (
    "tts", "image", "live", "audio", "embedding", "robotics", "computer-use",
    "transcribe", "omni", "banana", "decision", "translate", "lyria", "veo",
    "imagen", "research", "antigravity", "aqa", "gemma", "learnlm",
)  # fmt: skip
_MODEL_ID = re.compile(r"^gemini-[a-z0-9][a-z0-9.\-]{1,60}$")


def is_valid_model_id(value: str) -> bool:
    return value == AUTO_MODEL or bool(_MODEL_ID.match(str(value or "")))


def describe(model_id: str) -> ModelInfo:
    """Ficha de un modelo; los desconocidos se deducen de su nombre."""
    if model_id in _BY_ID:
        return _BY_ID[model_id]
    name = str(model_id)
    tier = "lite" if "lite" in name else "pro" if ("pro" in name or name.startswith("gemini-4")) else "flash"
    label = " ".join(
        part.upper() if part in ("tts",) else part.capitalize() for part in name.split("-")
    ).replace("Flash Lite", "Flash-Lite")
    return ModelInfo(name, label, tier, "Detectado en tu cuenta de Google AI Studio.")


def model_label(model_id: str) -> str:
    return "Automático" if model_id == AUTO_MODEL else describe(model_id).label


def _version_key(model_id: str) -> tuple:
    match = re.search(r"gemini-(\d+)(?:\.(\d+))?", model_id)
    major, minor = (int(match.group(1)), int(match.group(2) or 0)) if match else (0, 0)
    return (0 if "preview" in model_id or "exp" in model_id else 1, major, minor)


def pacific_day(now: float | None = None) -> str:
    return datetime.fromtimestamp(now or time.time(), PACIFIC).date().isoformat()


def next_pacific_midnight(now: float | None = None) -> float:
    current = datetime.fromtimestamp(now or time.time(), PACIFIC)
    tomorrow = (current + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return tomorrow.timestamp()


@dataclass
class ErrorInfo:
    kind: str  # rate_minute | rate_day | missing | auth | overloaded | blocked | network | bad_request | other
    retry_after: int = 30
    message: str = ""


def classify_error(exc: BaseException) -> ErrorInfo:
    """Traduce cualquier excepción del SDK en una categoría accionable."""
    text = str(exc)
    low = text.lower()
    code = getattr(exc, "code", None)
    retry = 30
    match = re.search(r"retry(?:delay)?['\"]?\s*[:=in]+\s*['\"]?(\d+(?:\.\d+)?)\s*s", text, flags=re.I)
    if match:
        retry = max(5, min(600, math.ceil(float(match.group(1)))))
    if code == 429 or "429" in text or "resource_exhausted" in low or "quota" in low:
        per_day = "perday" in low.replace("_", "").replace(" ", "") or "per day" in low
        return ErrorInfo("rate_day" if per_day else "rate_minute", retry, text)
    if (
        code in (401, 403)
        or "api_key_invalid" in low
        or "api key not valid" in low
        or "permission_denied" in low
        or "unauthenticated" in low
    ):
        # 403 por modelo no habilitado para la cuenta también llega aquí; se
        # distingue porque la clave sí funciona con otros modelos.
        if "api key" in low or "api_key" in low or code == 401 or "unauthenticated" in low:
            return ErrorInfo("auth", 0, text)
        return ErrorInfo("missing", 0, text)
    if (
        code == 404
        or "not_found" in low
        or "not found" in low
        or "is not supported for generatecontent" in low
    ):
        return ErrorInfo("missing", 0, text)
    if (
        code in (500, 502, 503, 504)
        or "unavailable" in low
        or "overloaded" in low
        or "high demand" in low
        or "capacity" in low
        or "deadline" in low
        or "internal" in low
        or re.search(r"\b50[0234]\b", text)
    ):
        return ErrorInfo("overloaded", retry, text)
    if code == 400 or "invalid_argument" in low or "failed_precondition" in low:
        return ErrorInfo("bad_request", 0, text)
    if "safety" in low or "blocked" in low or "prohibited" in low:
        return ErrorInfo("blocked", 0, text)
    if any(
        word in low
        for word in ("connection", "timed out", "timeout", "getaddrinfo", "ssl", "network", "dns")
    ):
        return ErrorInfo("network", 15, text)
    return ErrorInfo("other", 0, text)


class ModelRouter:
    """Decide qué modelos intentar y recuerda su estado entre sesiones."""

    DISCOVERY_TTL = 24 * 3600
    MISSING_TTL = 24 * 3600

    def __init__(self, store, clock=time.time):
        self.store = store
        self.clock = clock
        self._minute_cooldown: dict[str, float] = {}

    # ── Descubrimiento ────────────────────────────────────────────
    def discovered(self) -> list[str]:
        meta = self.store.get("meta", "gemini_models")
        models = meta.get("models")
        return [m for m in models if isinstance(m, str)] if isinstance(models, list) else []

    def discovery_is_fresh(self) -> bool:
        meta = self.store.get("meta", "gemini_models")
        return bool(meta.get("models")) and self.clock() - float(meta.get("checked_at", 0)) < self.DISCOVERY_TTL

    def refresh(self, client) -> list[str]:
        """Pregunta a la API qué modelos de texto admite esta clave."""
        found: list[str] = []
        for model in client.models.list():
            name = str(getattr(model, "name", "") or "").removeprefix("models/")
            actions = getattr(model, "supported_actions", None) or getattr(
                model, "supported_generation_methods", None
            )
            if actions is not None and "generateContent" not in actions:
                continue
            if not _MODEL_ID.match(name) or any(word in name for word in _EXCLUDED):
                continue
            if re.search(r"-\d{3}$|-\d{2}-\d{4}$|latest$", name):
                continue  # alias con fecha/revisión: se prefiere el nombre estable
            found.append(name)
        found = sorted(set(found), key=_version_key, reverse=True)
        if found:
            self.store.put("meta", "gemini_models", models=found, checked_at=self.clock())
            for model_id in found:
                self._clear(model_id, "missing_until")
        return found

    def available(self) -> list[ModelInfo]:
        """Modelos que la interfaz debe ofrecer, del más recomendable al menos."""
        discovered = self.discovered()
        if discovered:
            ids = [m for m in CATALOG_IDS if m in discovered]
            ids += [m for m in discovered if m not in ids and "preview" not in m and "exp" not in m]
        else:
            ids = list(CATALOG_IDS)
        now = self.clock()
        states = self.store.get_many("ai_model_state")
        ids = [m for m in ids if float(states.get(m, {}).get("missing_until", 0)) <= now] or ids
        return [describe(m) for m in ids]

    # ── Enrutado ──────────────────────────────────────────────────
    def chain(self, preferred: str | None = None, task: str = "chat") -> list[str]:
        """Lista ordenada de modelos a intentar para una solicitud."""
        infos = self.available()
        order = _TASK_ORDER.get(task, _TASK_ORDER["chat"])
        ranked = sorted(
            infos,
            key=lambda info: (order.index(info.tier) if info.tier in order else 9, infos.index(info)),
        )
        chain = [info.id for info in ranked]
        if preferred and preferred != AUTO_MODEL:
            chain = [preferred] + [m for m in chain if m != preferred]
        usable = [m for m in chain if self.is_usable(m)]
        blocked = [m for m in chain if m not in usable]
        # Un modelo elegido a mano se intenta siempre primero, aunque esté en pausa
        # por minuto: el usuario lo pidió y el error real es más útil que omitirlo.
        if preferred and preferred != AUTO_MODEL and preferred in blocked:
            state = self.state(preferred)
            if float(state.get("missing_until", 0)) <= self.clock() and float(
                state.get("exhausted_until", 0)
            ) <= self.clock():
                usable.insert(0, preferred)
                blocked.remove(preferred)
        return usable or blocked

    def state(self, model_id: str) -> dict:
        return self.store.get("ai_model_state", model_id)

    def is_usable(self, model_id: str) -> bool:
        now = self.clock()
        if self._minute_cooldown.get(model_id, 0) > now:
            return False
        state = self.state(model_id)
        return (
            float(state.get("exhausted_until", 0)) <= now
            and float(state.get("missing_until", 0)) <= now
        )

    def _clear(self, model_id: str, field: str) -> None:
        if self.state(model_id).get(field):
            self.store.put("ai_model_state", model_id, **{field: 0})

    # ── Aprendizaje ───────────────────────────────────────────────
    def note_error(self, model_id: str, info: ErrorInfo) -> None:
        now = self.clock()
        if info.kind == "rate_day":
            self.store.put("ai_model_state", model_id, exhausted_until=next_pacific_midnight(now))
        elif info.kind == "rate_minute":
            self._minute_cooldown[model_id] = now + max(5, info.retry_after)
        elif info.kind == "missing":
            self.store.put("ai_model_state", model_id, missing_until=now + self.MISSING_TTL)
        elif info.kind == "overloaded":
            self._minute_cooldown[model_id] = now + 20

    def note_success(self, model_id: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
        day = pacific_day(self.clock())
        key = f"{day}|{model_id}"
        row = self.store.get("ai_usage", key)
        self.store.put(
            "ai_usage",
            key,
            day=day,
            model=model_id,
            requests=int(row.get("requests", 0)) + 1,
            input_tokens=int(row.get("input_tokens", 0)) + int(input_tokens or 0),
            output_tokens=int(row.get("output_tokens", 0)) + int(output_tokens or 0),
        )

    def usage_today(self) -> dict[str, dict]:
        day = pacific_day(self.clock())
        return {
            row["model"]: row
            for row in self.store.get_many("ai_usage").values()
            if row.get("day") == day and row.get("model")
        }

    def usage_summary(self) -> str:
        """Texto breve para la interfaz: 'Hoy: Flash-Lite 12 · 3.8 Flash 3'."""
        usage = self.usage_today()
        if not usage:
            return "Sin consultas hoy"
        parts = [
            f"{describe(model).short} {int(row.get('requests', 0))}"
            for model, row in sorted(usage.items(), key=lambda item: -int(item[1].get("requests", 0)))
        ]
        return "Hoy: " + " · ".join(parts[:4])

    def exhausted_today(self) -> list[str]:
        now = self.clock()
        return [
            model
            for model, state in self.store.get_many("ai_model_state").items()
            if float(state.get("exhausted_until", 0)) > now
        ]

    def seconds_until_any_usable(self) -> int:
        now = self.clock()
        waits = [until - now for until in self._minute_cooldown.values() if until > now]
        return max(5, int(min(waits))) if waits else 30
