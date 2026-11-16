"""Atomic files and transactional, account-scoped local work journal."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
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


_DIGEST_CACHE: dict[tuple, str] = {}


def digest_cached(path: Path) -> str:
    """SHA-256 memorizado por identidad del archivo (ruta, tamaño, mtime, inode).

    Pensado para lecturas repetidas de la interfaz (inventario, pendientes). El
    pipeline sigue usando ``digest`` para verificar antes de borrar o reanudar."""
    p = Path(path)
    info = p.stat()
    key = (str(p), info.st_size, info.st_mtime_ns, getattr(info, "st_ino", 0))
    cached = _DIGEST_CACHE.get(key)
    if cached is None:
        cached = digest(p)
        if len(_DIGEST_CACHE) > 20000:
            _DIGEST_CACHE.clear()
        _DIGEST_CACHE[key] = cached
    return cached


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


def file_ok(path, checksum: str = "", cached: bool = False) -> bool:
    try:
        p = Path(path)
        if not (p.is_file() and p.stat().st_size > 0):
            return False
        if not checksum:
            return True
        return (digest_cached(p) if cached else digest(p)) == checksum
    except (OSError, TypeError, ValueError):
        return False


SCHEMA_VERSION = 3
# Una sola migración a la vez dentro del proceso; BEGIN IMMEDIATE cubre a otros procesos.
_MIGRATION_LOCK = threading.Lock()


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
                if int(db.execute("PRAGMA user_version").fetchone()[0]) < SCHEMA_VERSION:
                    self._upgrade(db)
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(
                "El historial local necesita reparación. No se borró ningún dato. Conserva state.sqlite3 y restaura una copia state-backup desde la carpeta de datos."
            ) from exc

    def _upgrade(self, db: sqlite3.Connection) -> None:
        """Lleva el esquema a la versión actual de forma atómica. Si dos hilos o
        dos procesos abren a la vez una base antigua, solo uno migra y el otro
        encuentra el trabajo hecho."""
        with _MIGRATION_LOCK:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version >= SCHEMA_VERSION:
                return
            has_data = bool(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'"
                ).fetchone()
            )
            if has_data:
                # Copia de seguridad única antes de cambiar el esquema.
                self._backup_before_upgrade(db, version)
            db.execute("BEGIN IMMEDIATE")
            if int(db.execute("PRAGMA user_version").fetchone()[0]) < SCHEMA_VERSION:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS records (namespace TEXT, kind TEXT, id TEXT, payload TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(namespace,kind,id))"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_records_kind ON records(namespace, kind, updated)"
                )
                self._migrate_rag_schema(db)
                self._migrate_v3(db)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            db.execute("COMMIT")

    @staticmethod
    def _run_script(db: sqlite3.Connection, script: str) -> None:
        """Ejecuta un guion sentencia a sentencia, sin el COMMIT implícito de
        ``executescript`` (que rompería la transacción de la migración)."""
        buffer = ""
        for line in script.splitlines():
            buffer += line + "\n"
            if sqlite3.complete_statement(buffer):
                db.execute(buffer)
                buffer = ""
        if buffer.strip():
            db.execute(buffer)

    @classmethod
    def _migrate_rag_schema(cls, db: sqlite3.Connection) -> None:
        cls._run_script(
            db,
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
            """,
        )

    def _backup_before_upgrade(self, db: sqlite3.Connection, version: int) -> None:
        destination = self.path.with_name(
            f"state-backup-v{version}-antes-de-v{SCHEMA_VERSION}.sqlite3"
        )
        if destination.exists():
            return
        try:
            with closing(sqlite3.connect(destination)) as target:
                db.backup(target)
        except (OSError, sqlite3.Error):
            destination.unlink(missing_ok=True)

    @staticmethod
    def _migrate_v3(db: sqlite3.Connection) -> None:
        """v3: métricas por fuente y metadatos por mensaje (uso, modo, citas numeradas)."""

        def columns(table: str) -> set[str]:
            return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}

        if "word_count" not in columns("knowledge_sources"):
            db.execute(
                "ALTER TABLE knowledge_sources ADD COLUMN word_count INTEGER NOT NULL DEFAULT 0"
            )
        if "meta_json" not in columns("chat_messages"):
            db.execute("ALTER TABLE chat_messages ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'")
        if "pinned" not in columns("chat_sessions"):
            db.execute("ALTER TABLE chat_sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated ON chat_sessions(namespace, updated_at)"
        )

    @staticmethod
    def _enable_wal(db: sqlite3.Connection) -> None:
        """Activa WAL tolerando la carrera del primer arranque: cambiar el modo de
        diario exige un bloqueo exclusivo y SQLite no aplica ``busy_timeout`` en
        ese paso, así que se reintenta brevemente."""
        for attempt in range(80):
            try:
                db.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                time.sleep(0.02 * (1 + attempt % 6))
        db.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA busy_timeout=30000")
            self._enable_wal(db)
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
            SELECT id, course_id, course_name, source_type, title, file_path, file_hash, is_active, created_at, word_count
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
                "word_count": int(r[9] or 0),
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

    def set_sources_active(self, states: dict[int, bool]) -> None:
        """Actualiza muchas fuentes en una sola transacción."""
        if not states:
            return
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "UPDATE knowledge_sources SET is_active = ? WHERE id = ?",
                [(1 if active else 0, int(sid)) for sid, active in states.items()],
            )

    def delete_knowledge_source(self, source_id: int) -> None:
        """Quita una fuente del índice (no borra el archivo del disco)."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM knowledge_chunks WHERE source_id = ?", (int(source_id),))
            db.execute(
                "DELETE FROM knowledge_sources WHERE id = ? AND (namespace = ? OR namespace = '')",
                (int(source_id), self.namespace),
            )

    def knowledge_stats(self) -> dict:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(word_count), 0),
                       COALESCE(SUM(source_type = 'transcript'), 0)
                FROM knowledge_sources WHERE namespace = ? OR namespace = ''
                """,
                (self.namespace,),
            ).fetchone()
            chunks = db.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(kc.embedding_blob IS NOT NULL), 0)
                FROM knowledge_chunks kc JOIN knowledge_sources ks ON ks.id = kc.source_id
                WHERE ks.namespace = ? OR ks.namespace = ''
                """,
                (self.namespace,),
            ).fetchone()
        return {
            "sources": int(row[0]),
            "words": int(row[1]),
            "transcripts": int(row[2]),
            "chunks": int(chunks[0]),
            "embedded": int(chunks[1]),
        }

    def delete(self, kind, key) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM records WHERE namespace=? AND kind=? AND id=?",
                (self.namespace, kind, str(key)),
            )

    def get_many(self, kind) -> dict[str, dict]:
        """Todos los registros de un tipo, indexados por id, en una sola consulta."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, payload FROM records WHERE namespace=? AND kind=?",
                (self.namespace, kind),
            ).fetchall()
        return {str(row[0]): json.loads(row[1]) for row in rows}

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

    def create_chat_session(
        self, course_id: str | None = None, all_courses: bool = False, title: str = ""
    ) -> int:
        """Abre una conversación nueva en el ámbito indicado (pasa a ser la activa)."""
        from datetime import UTC, datetime

        scope_course = "__ALL__" if all_courses else str(course_id or "")
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute(
                """
                INSERT INTO chat_sessions (namespace, course_id, all_courses, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (self.namespace, scope_course, 1 if all_courses else 0, title, now, now),
            )
            return int(cur.lastrowid)

    def list_chat_sessions(
        self, course_id: str | None = None, all_courses: bool = False, limit: int = 30
    ) -> list[dict]:
        scope_course = "__ALL__" if all_courses else str(course_id or "")
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT s.id, s.title, s.updated_at,
                       (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id),
                       (SELECT content FROM chat_messages m WHERE m.session_id = s.id
                         AND m.role = 'user' ORDER BY m.id ASC LIMIT 1)
                FROM chat_sessions s
                WHERE s.namespace = ? AND s.course_id = ? AND s.all_courses = ?
                ORDER BY s.id DESC LIMIT ?
                """,
                (self.namespace, scope_course, 1 if all_courses else 0, int(limit)),
            ).fetchall()
        return [
            {
                "id": int(r[0]),
                "title": str(r[1] or ""),
                "updated_at": str(r[2] or ""),
                "messages": int(r[3] or 0),
                "first_question": str(r[4] or ""),
            }
            for r in rows
        ]

    def rename_chat_session(self, session_id: int, title: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE chat_sessions SET title = ? WHERE id = ? AND namespace = ?",
                (str(title)[:120], int(session_id), self.namespace),
            )

    def delete_chat_session(self, session_id: int) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM chat_messages WHERE session_id = ?", (int(session_id),))
            db.execute(
                "DELETE FROM chat_sessions WHERE id = ? AND namespace = ?",
                (int(session_id), self.namespace),
            )

    def append_chat_message(
        self,
        session_id: int,
        role: str,
        content: str,
        citations: list | None = None,
        model_used: str = "",
        meta: dict | None = None,
    ) -> int:
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute(
                """
                INSERT INTO chat_messages (session_id, role, content, citations_json, model_used, created_at, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(session_id),
                    str(role),
                    str(content),
                    json.dumps(citations or [], ensure_ascii=False),
                    str(model_used),
                    now,
                    json.dumps(meta or {}, ensure_ascii=False),
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
                SELECT id, role, content, citations_json, model_used, created_at, meta_json
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
            try:
                meta = json.loads(r[6]) if r[6] else {}
            except (TypeError, ValueError):
                meta = {}
            result.append(
                {
                    "id": r[0],
                    "role": r[1],
                    "content": r[2],
                    "citations": citations,
                    "model_used": r[4],
                    "created_at": r[5],
                    "meta": meta if isinstance(meta, dict) else {},
                }
            )
        return result

    def delete_chat_message(self, message_id: int) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM chat_messages WHERE id = ?", (int(message_id),))

    def clear_chat_session(self, session_id: int) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM chat_messages WHERE session_id = ?", (int(session_id),))
