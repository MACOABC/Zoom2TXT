"""Transactional history. Omissions and verified local files are distinct."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

from zoom2txt.config import REGISTRY_FILE, Settings
from zoom2txt.storage import StateStore, digest, file_ok, safe_name


def short_name_for(course_name: str) -> str:
    value = unicodedata.normalize("NFKD", course_name)
    value = "".join(c for c in value if not unicodedata.combining(c))
    return safe_name("".join(w.capitalize() for w in re.findall(r"[A-Za-z0-9]+", value)), 70)


def recording_key(course_id, meeting_id):
    return hashlib.sha256(f"{course_id}\n{meeting_id}".encode()).hexdigest()[:24]


class Registry:
    def __init__(self, settings=None):
        self.settings = settings or Settings.load()
        self.store = StateStore(self.settings)
        if not self.store.get("meta", "legacy_imported"):
            self._import_legacy()

    def _import_legacy(self):
        try:
            raw = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            # Legacy JSON has no account ID: import it into only one profile.
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT payload FROM records WHERE namespace='__system__' AND kind='migration' AND id='legacy_owner'"
                ).fetchone()
                if row and json.loads(row[0])["namespace"] != self.store.namespace:
                    return
                if not row:
                    import time

                    db.execute(
                        "INSERT INTO records VALUES(?,?,?,?,?)",
                        (
                            "__system__",
                            "migration",
                            "legacy_owner",
                            json.dumps({"namespace": self.store.namespace}),
                            time.time(),
                        ),
                    )
            for cid, course in raw.items():
                if not isinstance(course, dict):
                    continue
                for mid, name in course.get("meetings", {}).items():
                    if not isinstance(name, str):
                        continue
                    key = recording_key(cid, mid)
                    if self.store.get("recording", key):
                        continue
                    path = self.settings.transcripts_dir / f"{safe_name(name)}_Para_NotebookLM.txt"
                    valid = file_ok(path)
                    self.store.put(
                        "recording",
                        key,
                        course_id=cid,
                        meeting_id=mid,
                        name=name,
                        status="done" if valid else "legacy_review",
                        path=str(path) if valid else "",
                        checksum=digest(path) if valid else "",
                        note="Importado de v2.2. Revisa las omisiones antiguas antes de reprocesar.",
                    )
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self.store.put("meta", "legacy_imported", complete=True)

    @property
    def _data(self):
        data = {}
        for row in self.store.list("recording"):
            course = data.setdefault(row["course_id"], {"next_num": 1, "meetings": {}})
            if row.get("status") in ("done", "omitted", "duplicate", "legacy_review"):
                course["meetings"][row["meeting_id"]] = row["name"]
        return data

    def records(self):
        return self.store.list("recording")

    def is_processed(self, course_id, meeting_id):
        row = self.store.get("recording", recording_key(course_id, meeting_id))
        if row.get("status") == "done":
            return file_ok(row.get("path"), row.get("checksum", ""))
        return row.get("status") in ("omitted", "duplicate", "legacy_review")

    def next_number(self, course_id):
        return 1 + len(
            [r for r in self.records() if r["course_id"] == course_id and r.get("status") == "done"]
        )

    def used_names(self, course_id):
        return {r["name"] for r in self.records() if r["course_id"] == course_id}

    def mark_skipped(self, course_id, meeting_id, note, name=""):
        self.store.put(
            "recording",
            recording_key(course_id, meeting_id),
            course_id=course_id,
            meeting_id=meeting_id,
            name=name or note,
            status="omitted",
            note=note,
        )

    def mark_done(self, course_id, meeting_id, name, path=None):
        path = Path(path) if path else self.settings.transcripts_dir / f"{name}_Para_NotebookLM.txt"
        if not file_ok(path):
            raise ValueError("No se puede completar una clase sin una transcripción válida.")
        self.store.put(
            "recording",
            recording_key(course_id, meeting_id),
            course_id=course_id,
            meeting_id=meeting_id,
            name=name,
            status="done",
            path=str(path),
            checksum=digest(path),
            note="",
        )

    def restore(self, key):
        self.store.put("recording", key, status="pending", note="Restaurada por el usuario")

    def reset(self):
        backup = self.store.backup()
        for row in self.records():
            if row.get("status") != "done":
                self.restore(row["id"])
        return backup
