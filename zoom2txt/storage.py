"""Atomic files and transactional, account-scoped local work journal."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path


def atomic_write(path: Path, content: bytes | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content.encode("utf-8") if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def safe_name(value: str, limit: int = 100) -> str:
    name = re.sub(r'[<>:"/\\|?*%\x00-\x1f]', "_", str(value)).strip(". ")
    if not name:
        name = "Sin_nombre"
    if name.split(".")[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *[f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)],
    }:
        name = "_" + name
    if len(name) > limit:
        name = name[: limit - 9] + "_" + hashlib.sha256(name.encode()).hexdigest()[:8]
    return name


def file_ok(path, checksum: str = "") -> bool:
    try:
        p = Path(path)
        return p.is_file() and p.stat().st_size > 0 and (not checksum or digest(p) == checksum)
    except (OSError, TypeError, ValueError):
        return False


class StateStore:
    """Short-lived SQLite connections; merge updates inside BEGIN IMMEDIATE."""

    def __init__(self, settings=None, path=None):
        from zoom2txt.config import DATA_DIR, Settings

        self.settings = settings or Settings.load()
        self.path = Path(path) if path else DATA_DIR / "state.sqlite3"
        self.namespace = hashlib.sha256(
            (
                self.settings.blackboard_url.lower().rstrip("/")
                + "\n"
                + self.settings.zoom_user.lower().strip()
            ).encode()
        ).hexdigest()[:20]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.connect() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS records (namespace TEXT, kind TEXT, id TEXT, payload TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(namespace,kind,id))"
                )
                db.execute("PRAGMA user_version=1")
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(
                "El historial local necesita reparación. No se borró ningún dato. Conserva state.sqlite3 y restaura una copia state-backup desde la carpeta de datos."
            ) from exc

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        try:
            db.execute("PRAGMA busy_timeout=20000")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def get(self, kind, key):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM records WHERE namespace=? AND kind=? AND id=?",
                (self.namespace, kind, str(key)),
            ).fetchone()
        return json.loads(row[0]) if row else {}

    def put(self, kind, key, **values):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM records WHERE namespace=? AND kind=? AND id=?",
                (self.namespace, kind, str(key)),
            ).fetchone()
            payload = json.loads(row[0]) if row else {}
            payload.update(values)
            payload["id"] = str(key)
            payload["updated"] = time.time()
            db.execute(
                "INSERT OR REPLACE INTO records VALUES(?,?,?,?,?)",
                (
                    self.namespace,
                    kind,
                    str(key),
                    json.dumps(payload, ensure_ascii=False),
                    time.time(),
                ),
            )
        return payload

    def list(self, kind):
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM records WHERE namespace=? AND kind=? ORDER BY updated DESC",
                (self.namespace, kind),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def claim(self, kind, key, **values):
        """Atomically create a receipt; False means already claimed."""
        with self.connect() as db:
            result = db.execute(
                "INSERT OR IGNORE INTO records VALUES(?,?,?,?,?)",
                (self.namespace, kind, str(key), json.dumps(values), time.time()),
            )
            return result.rowcount == 1

    def backup(self):
        destination = self.path.with_name(f"state-backup-{time.time_ns()}.sqlite3")
        with self.connect() as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
        return destination

    def reserve(self, key, cost, limit):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM records WHERE namespace=? AND kind=? AND id=?",
                (self.namespace, "quota", key),
            ).fetchone()
            used = json.loads(row[0]).get("used", 0) if row else 0
            if used + cost > limit:
                return False
            db.execute(
                "INSERT OR REPLACE INTO records VALUES(?,?,?,?,?)",
                (
                    self.namespace,
                    "quota",
                    key,
                    json.dumps({"used": used + cost}),
                    time.time(),
                ),
            )
            return True
