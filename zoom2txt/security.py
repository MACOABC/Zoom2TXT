"""User-bound encryption and sanitized diagnostics. No plaintext fallback."""

from __future__ import annotations

import base64
import ctypes
import json
import re
import sys
from pathlib import Path

from zoom2txt.storage import atomic_write


def secure_keyring():
    import keyring

    backend = keyring.get_keyring()
    module = type(backend).__module__.lower()
    if not any(word in module for word in ("windows", "secretservice", "kwallet", "macos")):
        raise RuntimeError(
            "No hay un almacén seguro de credenciales disponible. Configura Credential Manager o Secret Service/KWallet."
        )
    return keyring


def _protect(data: bytes, decrypt=False) -> bytes:
    if sys.platform == "win32":
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [
                ("size", wintypes.DWORD),
                ("data", ctypes.POINTER(ctypes.c_ubyte)),
            ]

        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = Blob()
        operation = (
            ctypes.windll.crypt32.CryptUnprotectData
            if decrypt
            else ctypes.windll.crypt32.CryptProtectData
        )
        if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise RuntimeError(
                "El sistema no pudo abrir o proteger la sesión de este usuario. Vuelve a autorizar tu cuenta."
            )
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            ctypes.windll.kernel32.LocalFree(target.data)
    from cryptography.fernet import Fernet

    ring = secure_keyring()
    key = ring.get_password("Zoom2TXT", "__file_encryption_key__")
    if not key:
        if decrypt:
            raise RuntimeError(
                "No se encontró la clave local de la sesión. Autoriza de nuevo tu cuenta."
            )
        key = Fernet.generate_key().decode()
        ring.set_password("Zoom2TXT", "__file_encryption_key__", key)
    return Fernet(key.encode()).decrypt(data) if decrypt else Fernet(key.encode()).encrypt(data)


def write_private_json(path: Path, value) -> None:
    sealed = base64.b64encode(_protect(json.dumps(value).encode())).decode()
    atomic_write(path, json.dumps({"zoom2txt_encrypted": 1, "payload": sealed}))


def read_private_json(path: Path, migrate=True):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and raw.get("zoom2txt_encrypted") == 1:
        return json.loads(_protect(base64.b64decode(raw["payload"], validate=True), decrypt=True))
    if migrate:
        write_private_json(path, raw)
    return raw


def redact(message) -> str:
    text = str(message)
    text = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[OCULTO]", text)
    text = re.sub(r"\b[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[correo]", text)
    text = re.sub(
        r'(?i)((?:access_token|refresh_token|id_token|client_secret|password|oauth_signature|authorization|cookie|scid|xsrf)["\s:=]+)[^\s,;<>]+',
        r"\1[OCULTO]",
        text,
    )
    text = re.sub(
        r"([?&](?:pwd|passcode|token|signature|auth|scid|upload_id)=)[^&\s]+",
        r"\1[OCULTO]",
        text,
        flags=re.I,
    )
    return text
