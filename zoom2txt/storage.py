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
                self._migrate_rag_schema(db)
                db.execute("PRAGMA user_version=2")
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(
                "El historial local necesita reparación. No se borró ningún dato. Conserva state.sqlite3 y restaura una copia state-backup desde la carpeta de datos."
            ) from exc

    @staticmethod
    def _migrate_rag_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS courses (
                id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL DEFAULT '',
                course_id TEXT NOT NULL,
                course_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_courses_course_id ON courses(course_id);
            CREATE INDEX IF NOT EXISTS idx_courses_namespace ON courses(namespace, course_id);

            CREATE TABLE IF NOT EXISTS knowledge_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace TEXT NOT NULL DEFAULT '',
                course_id TEXT NOT NULL,
                course_name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                title TEXT NOT NULL,
                file_path TEXT UNIQUE NOT NULL,
                file_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_sources_course_id ON knowledge_sources(course_id);
            CREATE INDEX IF NOT EXISTS idx_knowledge_sources_ns_course ON knowledge_sources(namespace, course_id);

            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
                course_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                embedding_blob BLOB NULL
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_course_id ON knowledge_chunks(course_id);
            CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_source_id ON knowledge_chunks(source_id);

            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5(
                content,
                course_id UNINDEXED,
                source_id UNINDEXED,
                chunk_id UNINDEXED
            );

            CREATE TRIGGER IF NOT EXISTS trg_knowledge_chunks_ai AFTER INSERT ON knowledge_chunks BEGIN
                INSERT INTO knowledge_chunks_fts(rowid, content, course_id, source_id, chunk_id)
                VALUES (new.id, new.content, new.course_id, new.source_id, new.id);
            END;

            CREATE TRIGGER IF NOT EXISTS trg_knowledge_chunks_ad AFTER DELETE ON knowledge_chunks BEGIN
                DELETE FROM knowledge_chunks_fts WHERE rowid = old.id;
            END;

            CREATE TRIGGER IF NOT EXISTS trg_knowledge_chunks_au AFTER UPDATE OF content, course_id, source_id ON knowledge_chunks BEGIN
                DELETE FROM knowledge_chunks_fts WHERE rowid = old.id;
                INSERT INTO knowledge_chunks_fts(rowid, content, course_id, source_id, chunk_id)
                VALUES (new.id, new.content, new.course_id, new.source_id, new.id);
            END;

            CREATE TABLE IF NOT EXISTS chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace TEXT NOT NULL DEFAULT '',
                course_id TEXT,
                all_courses INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chat_sessions_scope ON chat_sessions(namespace, course_id, all_courses);

            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                citations_json TEXT NOT NULL DEFAULT '[]',
                model_used TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id);
            """
        )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
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

    # ── RAG & Chat Persistence (BEGIN IMMEDIATE) ──────────────────
    def upsert_course(self, course_id: str, course_name: str, created_at: str = "") -> None:
        from datetime import UTC, datetime

        stamp = created_at or datetime.now(UTC).isoformat()
        cid = str(course_id).strip()
        cname = str(course_name).strip() or cid
        pk = f"{self.namespace}:{cid}"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT INTO courses (id, namespace, course_id, course_name, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    course_name = excluded.course_name
                """,
                (pk, self.namespace, cid, cname, stamp),
            )

    def list_rag_courses(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT course_id, course_name FROM courses
                WHERE namespace = ? OR namespace = ''
                UNION
                SELECT DISTINCT course_id, course_name FROM knowledge_sources
                WHERE namespace = ? OR namespace = ''
                ORDER BY course_name COLLATE NOCASE ASC
                """,
                (self.namespace, self.namespace),
            ).fetchall()
        seen: dict[str, str] = {}
        for cid, cname in rows:
            if cid and (cid not in seen or len(str(cname)) > len(seen[cid])):
                seen[str(cid)] = str(cname or cid)
        return [{"id": cid, "name": cname} for cid, cname in seen.items()]

    def list_knowledge_sources(
        self, course_id: str | None = None, all_courses: bool = False, active_only: bool = False
    ) -> list[dict]:
        query = """
            SELECT id, course_id, course_name, source_type, title, file_path, file_hash, is_active, created_at
            FROM knowledge_sources
            WHERE (namespace = ? OR namespace = '')
        """
        params: list = [self.namespace]
        if not all_courses and course_id is not None:
            query += " AND course_id = ?"
            params.append(str(course_id))
        if active_only:
            query += " AND is_active = 1"
        query += " ORDER BY course_name COLLATE NOCASE ASC, title COLLATE NOCASE ASC"
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [
            {
                "id": r[0],
                "course_id": r[1],
                "course_name": r[2],
                "source_type": r[3],
                "title": r[4],
                "file_path": r[5],
                "file_hash": r[6],
                "is_active": bool(r[7]),
                "created_at": r[8],
            }
            for r in rows
        ]

    def set_source_active(self, source_id: int, is_active: bool) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE knowledge_sources SET is_active = ? WHERE id = ?",
                (1 if is_active else 0, int(source_id)),
            )

    def get_or_create_chat_session(
        self, course_id: str | None = None, all_courses: bool = False, title: str = ""
    ) -> int:
        from datetime import UTC, datetime

        scope_course = "__ALL__" if all_courses else str(course_id or "")
        flag = 1 if all_courses else 0
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT id FROM chat_sessions
                WHERE namespace = ? AND course_id = ? AND all_courses = ?
                ORDER BY id DESC LIMIT 1
                """,
                (self.namespace, scope_course, flag),
            ).fetchone()
            if row:
                return int(row[0])
            cur = db.execute(
                """
                INSERT INTO chat_sessions (namespace, course_id, all_courses, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.namespace,
                    scope_course,
                    flag,
                    title or ("Todos los cursos" if all_courses else scope_course),
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def append_chat_message(
        self,
        session_id: int,
        role: str,
        content: str,
        citations: list | None = None,
        model_used: str = "",
    ) -> int:
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute(
                """
                INSERT INTO chat_messages (session_id, role, content, citations_json, model_used, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(session_id),
                    str(role),
                    str(content),
                    json.dumps(citations or [], ensure_ascii=False),
                    str(model_used),
                    now,
                ),
            )
            db.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                (now, int(session_id)),
            )
            return int(cur.lastrowid)

    def get_chat_messages(self, session_id: int) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT id, role, content, citations_json, model_used, created_at
                FROM chat_messages
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (int(session_id),),
            ).fetchall()
        result = []
        for r in rows:
            try:
                citations = json.loads(r[3]) if r[3] else []
            except (TypeError, ValueError):
                citations = []
            result.append(
                {
                    "id": r[0],
                    "role": r[1],
                    "content": r[2],
                    "citations": citations,
                    "model_used": r[4],
                    "created_at": r[5],
                }
            )
        return result

    def clear_chat_session(self, session_id: int) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM chat_messages WHERE session_id = ?", (int(session_id),))
