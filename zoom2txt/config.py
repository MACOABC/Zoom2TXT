"""Configuración y rutas de usuario multiplataforma.

Todo lo sensible o específico del usuario vive en la carpeta de datos
del SO (AppData en Windows, ~/.local/share en Linux), nunca en el
directorio del proyecto.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from platformdirs import user_data_dir

from zoom2txt import APP_NAME
from zoom2txt.storage import atomic_write

DATA_DIR = Path(
    os.environ.get("ZOOM2TXT_DATA_DIR") or user_data_dir(APP_NAME, appauthor=False)
).resolve()
SETTINGS_FILE = DATA_DIR / "settings.json"
CLIENT_SECRETS_FILE = DATA_DIR / "client_secrets.json"
TOKEN_FILE = DATA_DIR / "token.json"
# Sesión de Blackboard (cookies) y registro de grabaciones ya procesadas
BB_COOKIES_FILE = DATA_DIR / "bb_cookies.json"
REGISTRY_FILE = DATA_DIR / "processed.json"
WEBENGINE_DIR = DATA_DIR / "webengine"
DEBUG_DIR = DATA_DIR / "debug"

DEFAULT_VIDEOS_DIR = Path.home() / APP_NAME / "Videos clase"
DEFAULT_TRANSCRIPTS_DIR = Path.home() / APP_NAME / "Transcripciones"
DEFAULT_MATERIALS_DIR = Path.home() / APP_NAME / "Material"
DEFAULT_BLACKBOARD_URL = ""

AI_STUDIO_APIKEY_URL = "https://aistudio.google.com/apikey"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
FREE_TIER_MODELS = (
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-2.5-flash",
)
EMBEDDING_MODEL = "gemini-embedding-001"


def save_gemini_api_key(api_key: str) -> None:
    """Guarda la GEMINI_API_KEY cifrada localmente para el usuario actual."""
    from zoom2txt.security import write_private_json

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key_file = DATA_DIR / "gemini_key.json"
    cleaned = str(api_key or "").strip()
    if not cleaned:
        key_file.unlink(missing_ok=True)
        return
    try:
        write_private_json(key_file, {"api_key": cleaned})
    except RuntimeError:
        # Fallback atómico con permisos 0600 si el entorno sin escritorio no tiene keyring activo.
        atomic_write(key_file, json.dumps({"api_key": cleaned}))


def load_gemini_api_key() -> str:
    """Recupera la GEMINI_API_KEY cifrada local o desde variable de entorno."""
    from zoom2txt.security import read_private_json

    key_file = DATA_DIR / "gemini_key.json"
    if key_file.is_file():
        try:
            data = read_private_json(key_file, migrate=False)
            if isinstance(data, dict) and isinstance(data.get("api_key"), str):
                return data["api_key"].strip()
        except Exception:  # noqa: BLE001, S110
            pass
    return os.environ.get("GEMINI_API_KEY", "").strip()


def test_gemini_connection(api_key: str, model: str | None = None) -> tuple[bool, str]:
    """Prueba la conexión con Google AI Studio usando el SDK oficial google-genai."""
    cleaned = str(api_key or "").strip()
    if not cleaned:
        return False, "Ingresa una GEMINI_API_KEY obtenida en Google AI Studio (aistudio.google.com/apikey)."
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return False, "El paquete oficial 'google-genai' no está instalado en este entorno."

    client = genai.Client(api_key=cleaned)
    candidates = [model] if model else []
    for m in FREE_TIER_MODELS:
        if m not in candidates:
            candidates.append(m)

    last_err = ""
    for candidate in candidates:
        if not candidate:
            continue
        try:
            resp = client.models.generate_content(
                model=candidate,
                contents="Responde únicamente: OK",
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=10,
                ),
            )
            text = getattr(resp, "text", "") or "OK"
            return True, f"Conexión verificada con {candidate} ({text.strip()[:30]})."
        except Exception as exc:  # noqa: BLE001
            from zoom2txt.security import redact

            msg = redact(str(exc))
            last_err = msg
            if "401" in msg or "API_KEY_INVALID" in msg or "API key not valid" in msg:
                return False, "La clave API no es válida. Verifica que la copiaste completa desde Google AI Studio."
            continue
    return False, f"No se pudo conectar con Gemini API: {last_err}"


def clean_blackboard_url(value: str) -> str:
    """Extrae la URL base limpia (https://campus.edu) a partir de cualquier enlace del campus."""
    base = str(value or "").strip()
    if not base:
        return ""
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    parsed = urlparse(base)
    if parsed.hostname:
        return f"https://{parsed.hostname.lower()}"
    return base.rstrip("/")


def validate_blackboard_url(value: str) -> tuple[bool, str]:
    """Accept only credential-free HTTPS Blackboard base URLs."""
    base = str(value or "").strip().rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return False, (
            "Escribe la dirección HTTPS de tu campus Blackboard, sin usuario, "
            "contraseña, parámetros ni fragmentos."
        )
    return True, ""


def find_downloaded_client_secrets() -> Path | None:
    """Busca en carpetas de Descargas habituales el archivo client_secret*.json más reciente."""
    home = Path.home()
    download_dirs = [
        home / "Downloads",
        home / "Descargas",
        home / "downloads",
        home / "descargas",
    ]
    candidates: list[Path] = []
    for d in download_dirs:
        if d.is_dir():
            try:
                for f in d.glob("client_secret*.json"):
                    if f.is_file():
                        candidates.append(f)
            except OSError:
                continue
    if not candidates:
        return None
    try:
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0]
    except OSError:
        return candidates[0] if candidates else None


def detect_drive_dir() -> str:
    """Detecta Google Drive para Escritorio y propone la carpeta
    compartida PC↔celular. '' si no hay Drive montado."""
    import string

    candidates = []
    for letter in string.ascii_uppercase:
        for mount in ("Mi unidad", "My Drive"):
            candidates.append(Path(f"{letter}:/") / mount)
    for root in candidates:
        try:
            if root.exists():
                return str(root / APP_NAME / "Transcripciones")
        except OSError:
            continue
    return ""


@dataclass
class Settings:
    zoom_user: str = ""
    videos_dir: Path = field(default_factory=lambda: DEFAULT_VIDEOS_DIR)
    transcripts_dir: Path = field(default_factory=lambda: DEFAULT_TRANSCRIPTS_DIR)
    materials_dir: Path = field(default_factory=lambda: DEFAULT_MATERIALS_DIR)
    sub_lang: str = "es"
    wizard_done: bool = False
    blackboard_url: str = DEFAULT_BLACKBOARD_URL
    auto_delete_youtube: bool = True
    term_start: str = ""  # 'YYYY-MM-DD' del lunes de la Semana 1
    theme: str = "dark"  # 'dark' | 'light'
    # Carpeta compartida PC↔celular en Google Drive ('' = desactivado)
    drive_dir: str = ""
    delete_local_video: bool = False
    batch_size: int = 6
    longest_per_day: bool = True
    youtube_private: bool = False
    gemini_api_key: str = ""
    gemini_model: str = DEFAULT_GEMINI_MODEL
    use_semantic_embeddings: bool = True

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "zoom_user": self.zoom_user,
            "videos_dir": str(self.videos_dir),
            "transcripts_dir": str(self.transcripts_dir),
            "materials_dir": str(self.materials_dir),
            "sub_lang": self.sub_lang,
            "wizard_done": self.wizard_done,
            "blackboard_url": self.blackboard_url,
            "auto_delete_youtube": self.auto_delete_youtube,
            "term_start": self.term_start,
            "theme": self.theme,
            "drive_dir": self.drive_dir,
            "delete_local_video": self.delete_local_video,
            "batch_size": self.batch_size,
            "longest_per_day": self.longest_per_day,
            "youtube_private": False,
            "gemini_model": self.gemini_model
            if self.gemini_model in FREE_TIER_MODELS
            else DEFAULT_GEMINI_MODEL,
            "use_semantic_embeddings": bool(self.use_semantic_embeddings),
            "schema_version": 2,
        }
        atomic_write(SETTINGS_FILE, json.dumps(payload, indent=2, ensure_ascii=False))
        if self.gemini_api_key is not None:
            save_gemini_api_key(self.gemini_api_key)

    @classmethod
    def load(cls) -> Settings:
        """Carga settings.json; ante cualquier archivo ausente o corrupto
        devuelve defaults (lo que dispara el wizard, nunca un traceback)."""
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("La configuración debe ser un objeto.")

            def flag(key, default=False):
                value = raw.get(key, default)
                return value if isinstance(value, bool) else default

            def folder(key, default):
                value = raw.get(key)
                return (
                    Path(value).expanduser().resolve()
                    if isinstance(value, str) and value.strip() and Path(value).is_absolute()
                    else default
                )

            base = str(raw.get("blackboard_url", DEFAULT_BLACKBOARD_URL)).rstrip("/")
            if not validate_blackboard_url(base)[0]:
                raise ValueError("Blackboard debe usar una dirección HTTPS válida.")
            raw_model = str(raw.get("gemini_model", DEFAULT_GEMINI_MODEL)).strip()
            model = raw_model if raw_model in FREE_TIER_MODELS else DEFAULT_GEMINI_MODEL
            legacy_key = str(raw.get("gemini_api_key", "")).strip()
            if legacy_key:
                save_gemini_api_key(legacy_key)
            api_key = load_gemini_api_key() or legacy_key
            return cls(
                zoom_user=str(raw.get("zoom_user", "")),
                videos_dir=folder("videos_dir", DEFAULT_VIDEOS_DIR),
                transcripts_dir=folder("transcripts_dir", DEFAULT_TRANSCRIPTS_DIR),
                materials_dir=folder("materials_dir", DEFAULT_MATERIALS_DIR),
                sub_lang=str(raw.get("sub_lang", "es")),
                wizard_done=flag("wizard_done"),
                blackboard_url=base,
                auto_delete_youtube=True,
                term_start=str(raw.get("term_start", "")),
                theme="light" if raw.get("theme") == "light" else "dark",
                drive_dir=str(folder("drive_dir", "")),
                delete_local_video=flag("delete_local_video")
                if raw.get("schema_version") == 2
                else False,
                batch_size=max(1, min(6, int(raw.get("batch_size", 6)))),
                longest_per_day=flag("longest_per_day", True),
                youtube_private=False,
                gemini_api_key=api_key,
                gemini_model=model,
                use_semantic_embeddings=flag("use_semantic_embeddings", True),
            )
        except (OSError, ValueError, TypeError):
            return cls(gemini_api_key=load_gemini_api_key())


def validate_client_secrets(path: Path) -> tuple[bool, str]:
    """Valida que el JSON sea una credencial OAuth de tipo
    'Aplicación de escritorio' (clave raíz "installed")."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, (
            "El archivo no es un JSON válido. Descarga el archivo de "
            "credenciales desde Google Cloud Console sin modificarlo."
        )
    if not isinstance(raw, dict):
        return (
            False,
            "Se necesita un JSON de credenciales OAuth de Aplicación de escritorio, no una lista o un valor vacío.",
        )
    if "installed" in raw:
        info = raw["installed"]
        if (
            isinstance(info, dict)
            and all(
                isinstance(info.get(k), str) and info[k].strip()
                for k in ("client_id", "client_secret")
            )
            and info["client_id"].endswith(".apps.googleusercontent.com")
            and info.get("auth_uri") == "https://accounts.google.com/o/oauth2/auth"
            and info.get("token_uri") == "https://oauth2.googleapis.com/token"
        ):
            return True, ""
        return False, (
            "La credencial está incompleta o no usa los servidores oficiales de Google. "
            "Vuelve a descargar la credencial desde Google Cloud Console."
        )
    if "web" in raw:
        return False, (
            "Esta credencial es de tipo «Aplicación web», pero se necesita "
            "una de tipo «Aplicación de escritorio». En Google Cloud Console "
            "→ Credenciales → Crear credencial → ID de cliente OAuth, elige "
            "«Aplicación de escritorio» y descarga ese JSON."
        )
    return False, (
        "El JSON no tiene el formato de una credencial OAuth de escritorio. "
        "Asegúrate de descargar el archivo desde Google Cloud Console → "
        "Credenciales → tu ID de cliente OAuth de tipo «Aplicación de escritorio»."
    )


def credentials_status(settings: Settings) -> list[str]:
    """Devuelve la lista de piezas de configuración que faltan.
    Lista vacía = todo listo para trabajar."""
    missing: list[str] = []
    if not validate_blackboard_url(settings.blackboard_url)[0]:
        missing.append("blackboard")
    if not settings.wizard_done or not settings.zoom_user:
        missing.append("zoom")
    if not CLIENT_SECRETS_FILE.exists() or not validate_client_secrets(CLIENT_SECRETS_FILE)[0]:
        missing.append("client_secrets")
    else:
        # Cubre token ausente, corrupto o con permisos desactualizados.
        from zoom2txt.transcription.youtube_engine import token_is_valid

        if not token_is_valid(TOKEN_FILE):
            missing.append("token")
    return missing
