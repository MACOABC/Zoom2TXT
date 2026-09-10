"""Crash-safe activation of an OAuth client/token pair with rollback."""

import json

from zoom2txt.config import CLIENT_SECRETS_FILE, DATA_DIR, TOKEN_FILE
from zoom2txt.storage import atomic_write


def recover_oauth_change():
    journal = DATA_DIR / "oauth-change.json"
    if not journal.exists():
        return
    record = json.loads(journal.read_text(encoding="utf-8"))
    for key, target in (("client", CLIENT_SECRETS_FILE), ("token", TOKEN_FILE)):
        backup = DATA_DIR / (key + ".oauth-backup")
        if record.get(key):
            if not backup.exists():
                raise RuntimeError(
                    "Falta el respaldo de la autorización anterior. Reabre el asistente antes de procesar."
                )
            atomic_write(target, backup.read_bytes())
        else:
            target.unlink(missing_ok=True)
    journal.unlink()


def commit_oauth_change(candidate, token):
    recover_oauth_change()
    previous = {}
    for key, target in (("client", CLIENT_SECRETS_FILE), ("token", TOKEN_FILE)):
        previous[key] = target.exists()
        if target.exists():
            atomic_write(DATA_DIR / (key + ".oauth-backup"), target.read_bytes())
    journal = DATA_DIR / "oauth-change.json"
    atomic_write(journal, json.dumps(previous))
    try:
        if candidate.exists():
            atomic_write(CLIENT_SECRETS_FILE, candidate.read_bytes())
        atomic_write(TOKEN_FILE, token.read_bytes())
        journal.unlink()
    except Exception:
        recover_oauth_change()
        raise
    candidate.unlink(missing_ok=True)
    token.unlink(missing_ok=True)
    for key in ("client", "token"):
        (DATA_DIR / (key + ".oauth-backup")).unlink(missing_ok=True)
