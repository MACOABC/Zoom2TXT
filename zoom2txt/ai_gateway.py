"""Puerta única hacia Gemini: generación (normal, en streaming y JSON),
embeddings, rotación automática de modelos y errores en lenguaje claro.

Todo lo que habla con la API pasa por aquí, de modo que el resto de la app no
conoce el SDK y las pruebas pueden sustituir ``google.genai.Client``.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass

from zoom2txt.ai_models import AUTO_MODEL, ModelRouter, classify_error, model_label
from zoom2txt.security import redact

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSIONS = 768
EMBEDDING_BATCH = 48


class AIError(RuntimeError):
    """Error de IA ya redactado para mostrarse al estudiante."""


class AIAuthError(AIError):
    pass


class AIUnavailableError(AIError):
    pass


class RateLimitExceededError(AIError):
    """Límite gratuito alcanzado en todos los modelos disponibles."""

    def __init__(self, message: str, retry_after: int = 30, daily: bool = False):
        super().__init__(message)
        self.retry_after = max(5, int(retry_after))
        self.daily = daily


@dataclass
class Generation:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    fallback_from: str = ""
    finish_reason: str = ""


def _usage(response) -> tuple[int, int]:
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return 0, 0
    try:
        return int(getattr(meta, "prompt_token_count", 0) or 0), int(
            getattr(meta, "candidates_token_count", 0) or 0
        )
    except (TypeError, ValueError):
        return 0, 0


def _finish_reason(response) -> str:
    try:
        candidates = getattr(response, "candidates", None) or []
        reason = getattr(candidates[0], "finish_reason", "") if candidates else ""
        return str(getattr(reason, "name", reason) or "")
    except (TypeError, IndexError, AttributeError):
        return ""


def parse_json_payload(text: str):
    """Extrae el primer objeto o lista JSON de una respuesta del modelo, aunque
    venga envuelto en ``` o con texto alrededor."""
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I).strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
    if not starts:
        raise ValueError("La respuesta no contiene JSON.")
    start = min(starts)
    closer = "}" if cleaned[start] == "{" else "]"
    end = cleaned.rfind(closer)
    if end <= start:
        raise ValueError("La respuesta JSON está incompleta.")
    return json.loads(cleaned[start : end + 1])


class GeminiGateway:
    def __init__(self, key_getter: Callable[[], str], router: ModelRouter):
        self._key_getter = key_getter
        self.router = router

    # ── Infraestructura ───────────────────────────────────────────
    def api_key(self) -> str:
        return (self._key_getter() or "").strip()

    def has_key(self) -> bool:
        return bool(self.api_key())

    def client(self):
        key = self.api_key()
        if not key:
            raise AIAuthError(
                "Configura tu GEMINI_API_KEY gratuita de Google AI Studio "
                "(aistudio.google.com/apikey) para usar el asistente."
            )
        from google import genai

        return genai.Client(api_key=key)

    def refresh_models(self) -> list[str]:
        try:
            return self.router.refresh(self.client())
        except AIError:
            raise
        except Exception as exc:  # noqa: BLE001
            info = classify_error(exc)
            if info.kind == "auth":
                raise AIAuthError(_AUTH_MESSAGE) from exc
            raise AIError("No se pudo consultar la lista de modelos: " + redact(exc)) from exc

    # ── Generación ────────────────────────────────────────────────
    def generate(
        self,
        *,
        system: str,
        contents,
        preferred: str | None = None,
        task: str = "chat",
        temperature: float = 0.2,
        on_chunk: Callable[[str], None] | None = None,
        on_reset: Callable[[], None] | None = None,
        cancel: threading.Event | None = None,
        json_schema: dict | None = None,
        want_json: bool = False,
        max_output_tokens: int | None = None,
    ) -> Generation:
        """Genera texto probando la cadena de modelos hasta que uno responde."""
        client = self.client()
        from google.genai import types

        chain = self.router.chain(preferred, task)
        chosen = preferred if preferred and preferred != AUTO_MODEL else ""
        saw_rate = saw_overload = saw_network = False
        all_daily = True
        retry_after = 0
        last_error: BaseException | None = None

        for model in chain:
            use_schema = json_schema is not None
            for _attempt in range(2):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("Consulta cancelada.")
                config_args = {"system_instruction": system, "temperature": temperature}
                if max_output_tokens:
                    config_args["max_output_tokens"] = int(max_output_tokens)
                if want_json or json_schema is not None:
                    config_args["response_mime_type"] = "application/json"
                    if use_schema:
                        config_args["response_json_schema"] = json_schema
                config = types.GenerateContentConfig(**config_args)
                emitted = False
                try:
                    if on_chunk is not None:
                        pieces: list[str] = []
                        last = None
                        for part in client.models.generate_content_stream(
                            model=model, contents=contents, config=config
                        ):
                            if cancel is not None and cancel.is_set():
                                raise InterruptedError("Consulta cancelada.")
                            last = part
                            piece = getattr(part, "text", "") or ""
                            if piece:
                                pieces.append(piece)
                                emitted = True
                                on_chunk(piece)
                        text = "".join(pieces).strip()
                        tokens_in, tokens_out = _usage(last)
                        reason = _finish_reason(last)
                    else:
                        response = client.models.generate_content(
                            model=model, contents=contents, config=config
                        )
                        text = (getattr(response, "text", "") or "").strip()
                        tokens_in, tokens_out = _usage(response)
                        reason = _finish_reason(response)
                    self.router.note_success(model, tokens_in, tokens_out)
                    return Generation(
                        text=text,
                        model=model,
                        input_tokens=tokens_in,
                        output_tokens=tokens_out,
                        fallback_from=chosen if chosen and chosen != model else "",
                        finish_reason=reason,
                    )
                except InterruptedError:
                    raise
                except Exception as exc:  # noqa: BLE001 — se clasifica y se decide
                    last_error = exc
                    info = classify_error(exc)
                    if emitted and on_reset is not None:
                        on_reset()
                    if info.kind == "auth":
                        raise AIAuthError(_AUTH_MESSAGE) from exc
                    if info.kind == "bad_request" and use_schema:
                        # Algunos modelos no admiten el esquema: se pide JSON sin él.
                        use_schema = False
                        continue
                    self.router.note_error(model, info)
                    if info.kind in ("rate_minute", "rate_day"):
                        saw_rate = True
                        all_daily = all_daily and info.kind == "rate_day"
                        if info.kind == "rate_minute":
                            retry_after = (
                                info.retry_after
                                if not retry_after
                                else min(retry_after, info.retry_after)
                            )
                    elif info.kind == "overloaded":
                        saw_overload = True
                    elif info.kind == "network":
                        saw_network = True
                    elif info.kind in ("bad_request", "blocked", "other"):
                        raise AIError(_friendly(info.kind, exc)) from exc
                    break  # siguiente modelo de la cadena

        if saw_rate:
            daily = all_daily and not saw_overload and not saw_network
            if daily:
                raise RateLimitExceededError(
                    "Se agotó la cuota gratuita diaria de los modelos disponibles (429 RESOURCE_EXHAUSTED). "
                    "Google la renueva a medianoche del Pacífico; tu pregunta sigue escrita.",
                    retry_after=3600,
                    daily=True,
                )
            seconds = retry_after or 30
            raise RateLimitExceededError(
                "Se alcanzó el límite gratuito por minuto (429 RESOURCE_EXHAUSTED) en los modelos "
                f"disponibles. Espera {seconds} segundos e inténtalo de nuevo; tu pregunta sigue escrita.",
                retry_after=seconds,
            )
        if saw_overload:
            raise AIUnavailableError(
                "Los servidores de Google Gemini están experimentando una alta demanda temporal "
                "(503 UNAVAILABLE). Intenta enviar tu pregunta nuevamente en unos momentos."
            )
        if saw_network:
            raise AIUnavailableError(
                "No se pudo conectar con Google. Revisa tu conexión a Internet e inténtalo de nuevo."
            )
        raise AIError("Error al consultar Gemini API: " + redact(last_error or "sin respuesta"))

    def generate_json(self, **kwargs):
        """Como ``generate`` pero devuelve (objeto JSON, Generation)."""
        kwargs.setdefault("want_json", True)
        kwargs.setdefault("temperature", 0.3)
        result = self.generate(**kwargs)
        try:
            return parse_json_payload(result.text), result
        except ValueError as exc:
            raise AIError(
                "El modelo devolvió un formato inesperado. Vuelve a intentarlo; "
                "si se repite, prueba con otro modelo."
            ) from exc

    # ── Embeddings ────────────────────────────────────────────────
    def embed(self, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list:
        """Vectores float32 (768 d) para cada texto. Lanza si la API falla."""
        import numpy as np
        from google.genai import types

        client = self.client()
        vectors = []
        for start in range(0, len(texts), EMBEDDING_BATCH):
            batch = texts[start : start + EMBEDDING_BATCH]
            response = client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=batch if len(batch) > 1 else batch[0],
                config=types.EmbedContentConfig(
                    output_dimensionality=EMBEDDING_DIMENSIONS, task_type=task_type
                ),
            )
            embeddings = getattr(response, "embeddings", None) or []
            if len(embeddings) != len(batch):
                raise AIError("La API de embeddings devolvió una respuesta incompleta.")
            vectors.extend(np.asarray(e.values, dtype=np.float32) for e in embeddings)
        return vectors

    def test_connection(self, preferred: str | None = None) -> tuple[bool, str]:
        """Comprueba la clave con una solicitud mínima y refresca el catálogo."""
        try:
            try:
                found = self.refresh_models()
            except AIAuthError:
                raise
            except AIError:
                found = []
            result = self.generate(
                system="Responde únicamente con la palabra OK.",
                contents="OK",
                preferred=preferred,
                temperature=0.0,
                max_output_tokens=16,
            )
            extra = f" · {len(found)} modelos detectados en tu cuenta" if found else ""
            return True, f"Conexión verificada con {model_label(result.model)}{extra}."
        except RateLimitExceededError:
            return True, (
                "La clave es válida, pero la cuota gratuita está agotada por ahora. "
                "Podrás consultar cuando se renueve."
            )
        except AIAuthError as exc:
            return False, str(exc)
        except AIError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, "No se pudo conectar con Gemini API: " + redact(exc)


_AUTH_MESSAGE = (
    "La clave API no es válida o fue revocada. Copia la clave completa desde "
    "Google AI Studio (aistudio.google.com/apikey) y pégala en Configuración de IA."
)


def _friendly(kind: str, exc: BaseException) -> str:
    if kind == "blocked":
        return (
            "Gemini no generó una respuesta para esta consulta por sus filtros de contenido. "
            "Reformula la pregunta o prueba con otras fuentes."
        )
    return "Error al consultar Gemini API: " + redact(exc)
