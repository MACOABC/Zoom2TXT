"""Credenciales de Zoom en el keyring del sistema operativo.

La contraseña nunca se escribe en archivos ni en el código fuente:
se guarda cifrada por el SO (Credential Manager en Windows,
Secret Service/KWallet en Linux) para el usuario actual.
"""

from __future__ import annotations

import keyring
from keyring.errors import KeyringError

from zoom2txt import APP_NAME
from zoom2txt.security import secure_keyring

_SERVICE = APP_NAME


def set_zoom_password(user: str, password: str) -> None:
    secure_keyring().set_password(_SERVICE, user, password)


def get_zoom_password(user: str) -> str | None:
    if not user:
        return None
    try:
        return secure_keyring().get_password(_SERVICE, user)
    except (KeyringError, RuntimeError):
        return None


def delete_zoom_password(user: str) -> None:
    try:
        keyring.delete_password(_SERVICE, user)
    except KeyringError:
        pass
