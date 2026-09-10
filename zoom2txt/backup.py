"""Portable, credential-free history backups for a Zoom2TXT profile."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from zoom2txt import APP_NAME, __version__
from zoom2txt.storage import StateStore

FORMAT_VERSION = 1
_ALLOWED = {"manifest.json", "state.sqlite3"}
_MAX_UNCOMPRESSED = 100 * 1024 * 1024


def export_history(settings, destination: Path) -> Path:
    """Export current profile state; credentials and cookies are never included."""

    destination = Path(destination)
    if destination.suffix.lower() != ".zip":
        destination = destination.with_suffix(".zip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    state = StateStore(settings)
    with tempfile.TemporaryDirectory(prefix="zoom2txt-backup-") as temp:
        snapshot = Path(temp) / "state.sqlite3"
        with state.connect() as source, closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
        manifest = {
            "app": APP_NAME,
            "app_version": __version__,
            "format_version": FORMAT_VERSION,
            "created_utc": datetime.now(UTC).isoformat(),
            "source_namespace": state.namespace,
            "contains": ["historial", "cola", "planificación", "estado de trabajos"],
            "excluded": [
                "contraseñas",
                "tokens OAuth",
                "cookies",
                "client_secrets",
                "contadores diarios",
            ],
        }
        fd, temporary_name = tempfile.mkstemp(
            prefix=".zoom2txt-history-", suffix=".zip", dir=destination.parent
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
                archive.write(snapshot, "state.sqlite3")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return destination


def _inspect(source: Path) -> tuple[dict, bytes]:
    source = Path(source)
    try:
        with zipfile.ZipFile(source, "r") as archive:
            names = [item.filename for item in archive.infolist()]
            if len(names) != len(set(names)) or set(names) != _ALLOWED:
                raise ValueError("El ZIP no tiene la estructura de un respaldo Zoom2TXT.")
            if sum(item.file_size for item in archive.infolist()) > _MAX_UNCOMPRESSED:
                raise ValueError("El respaldo supera el tamaño de seguridad permitido.")
            manifest = json.loads(archive.read("manifest.json"))
            database = archive.read("state.sqlite3")
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("El respaldo no es un ZIP válido de Zoom2TXT.") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("app") != APP_NAME
        or manifest.get("format_version") != FORMAT_VERSION
        or not isinstance(manifest.get("source_namespace"), str)
    ):
        raise ValueError("El respaldo pertenece a otra aplicación o versión de formato.")
    return manifest, database


def restore_history(settings, source: Path) -> tuple[int, Path]:
    """Merge a backup profile into the currently configured local profile."""

    manifest, database = _inspect(Path(source))
    current = StateStore(settings)
    safety_copy = current.backup()
    with tempfile.TemporaryDirectory(prefix="zoom2txt-restore-") as temp:
        imported = Path(temp) / "state.sqlite3"
        imported.write_bytes(database)
        try:
            with closing(sqlite3.connect(imported)) as source_db:
                rows = source_db.execute(
                    "SELECT kind,id,payload,updated FROM records WHERE namespace=?",
                    (manifest["source_namespace"],),
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise ValueError("La base interna del respaldo está dañada.") from exc
    validated: list[tuple[str, str, str, float]] = []
    try:
        for kind, key, payload, updated in rows:
            if not all(isinstance(value, str) and value for value in (kind, key, payload)):
                raise ValueError
            parsed = json.loads(payload)
            timestamp = float(updated)
            if not isinstance(parsed, dict) or timestamp < 0:
                raise ValueError
            if kind not in {"quota", "daily_admissions"}:
                validated.append((kind, key, payload, timestamp))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("El historial del respaldo contiene datos inválidos.") from exc
    try:
        with current.connect() as target:
            target.execute("BEGIN IMMEDIATE")
            for kind, key, payload, updated in validated:
                target.execute(
                    "INSERT OR REPLACE INTO records VALUES(?,?,?,?,?)",
                    (current.namespace, kind, key, payload, max(updated, time.time())),
                )
    except sqlite3.DatabaseError as exc:
        raise ValueError("El historial del respaldo contiene datos inválidos.") from exc
    return len(validated), safety_copy
