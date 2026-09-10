"""Persistent daily admissions and observed course/week inventory.

Six is a local product rule, never a statement about Google's quota.
Inventory contains metadata only; signed URLs stay in encrypted job/session files.
"""

import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from zoom2txt.blackboard import weeks
from zoom2txt.blackboard.registry import Registry, recording_key
from zoom2txt.storage import StateStore

DAILY_LIMIT = 6
LOCAL_ZONE = ZoneInfo("America/Lima")


class DailyLimitReached(RuntimeError):
    pass


def local_now():
    return datetime.now(LOCAL_ZONE)


class WorkLedger:
    def __init__(self, settings, store=None):
        self.settings = settings
        self.store = store or StateStore(settings)

    def daily(self, now=None):
        now = now or local_now()
        day = now.astimezone(LOCAL_ZONE).date().isoformat()
        row = self.store.get("daily_admissions", day)
        admitted = set(row.get("jobs", []))
        return {
            "day": day,
            "used": len(admitted),
            "remaining": max(0, DAILY_LIMIT - len(admitted)),
            "jobs": admitted,
            "next_day": (now.astimezone(LOCAL_ZONE).date() + timedelta(days=1)).isoformat(),
        }

    def admit(self, key, now=None):
        day = self.daily(now)["day"]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM records WHERE namespace=? AND kind=? AND id=?",
                (self.store.namespace, "daily_admissions", day),
            ).fetchone()
            data = json.loads(row[0]) if row else {"jobs": []}
            jobs = set(data["jobs"])
            if key in jobs:
                return True
            if len(jobs) >= DAILY_LIMIT:
                return False
            jobs.add(key)
            db.execute(
                "INSERT OR REPLACE INTO records VALUES(?,?,?,?,?)",
                (
                    self.store.namespace,
                    "daily_admissions",
                    day,
                    json.dumps({"jobs": sorted(jobs)}),
                    time.time(),
                ),
            )
        return True

    def remember_course(self, course_id, name, records, now=None):
        stamp = (now or local_now()).timestamp()
        seen = set()
        for record in records:
            key = recording_key(course_id, record["meeting_id"])
            seen.add(key)
            self.store.put(
                "catalog",
                key,
                course_id=course_id,
                course_name=name,
                meeting_id=record["meeting_id"],
                start_time=record["start_time"],
                duration=record.get("duration", 0),
                name=record["name"],
                term_start=self.settings.term_start,
                available=True,
            )
        for old in self.store.list("catalog"):
            if old.get("course_id") == course_id and old["id"] not in seen:
                self.store.put("catalog", old["id"], available=False)
        self.store.put(
            "course",
            course_id,
            name=name,
            term_start=self.settings.term_start,
            scanned_at=stamp,
            error="",
        )

    def course_error(self, course_id, name, message):
        self.store.put(
            "course",
            course_id,
            name=name,
            term_start=self.settings.term_start,
            error=str(message),
        )

    def remember_courses(self, courses):
        self.store.put(
            "meta",
            "current_courses",
            ids=[c["id"] for c in courses],
            term_start=self.settings.term_start,
        )

    def courses(self):
        meta = self.store.get("meta", "current_courses")
        ids = set(meta.get("ids", []))
        return sorted(
            [
                c
                for c in self.store.list("course")
                if c["id"] in ids and c.get("term_start") == self.settings.term_start
            ],
            key=lambda c: c.get("name", ""),
        )

    def inventory(self):
        ids = {c["id"] for c in self.courses()}
        rows = [
            r
            for r in self.store.list("catalog")
            if r.get("course_id") in ids
            and r.get("available")
            and r.get("term_start") == self.settings.term_start
        ]
        monday = weeks.parse_start(self.settings.term_start)
        if monday:
            end = weeks.week_bounds(monday, 16)[1].isoformat()
            rows = [r for r in rows if monday.isoformat() <= r["start_time"][:10] <= end]
        if self.settings.longest_per_day:
            chosen = {}
            for row in sorted(rows, key=lambda r: r["id"]):
                key = row["course_id"], row["start_time"][:10]
                if key not in chosen or row.get("duration", 0) > chosen[key].get("duration", 0):
                    chosen[key] = row
            rows = list(chosen.values())
        return sorted(rows, key=lambda r: (r["start_time"], r["id"]))

    def pending(self):
        registry = Registry(self.settings)
        return [
            r
            for r in self.inventory()
            if not registry.is_processed(r["course_id"], r["meeting_id"])
        ]

    def set_expected(self, course_id, count):
        if not isinstance(count, int) or not 0 <= count <= 14:
            raise ValueError("Elige de 0 a 14 sesiones por semana.")
        self.store.put("weekly_plan", course_id, count=count, term_start=self.settings.term_start)

    def weekly(self, now=None):
        now = now or local_now()
        today = now.astimezone(LOCAL_ZONE).date()
        monday = weeks.parse_start(self.settings.term_start)
        if not monday:
            return []
        inventory = self.inventory()
        registry = Registry(self.settings)
        result = []
        for course in self.courses():
            plan = self.store.get("weekly_plan", course["id"])
            expected = (
                plan.get("count") if plan.get("term_start") == self.settings.term_start else None
            )
            for week in range(1, 17):
                lo, hi = weeks.week_bounds(monday, week)
                if lo > today:
                    break
                rows = [
                    r
                    for r in inventory
                    if r["course_id"] == course["id"]
                    and lo.isoformat() <= r["start_time"][:10] <= hi.isoformat()
                ]
                pending = [
                    r for r in rows if not registry.is_processed(r["course_id"], r["meeting_id"])
                ]
                closed = hi < today
                fresh = (
                    bool(course.get("scanned_at"))
                    and datetime.fromtimestamp(course["scanned_at"], LOCAL_ZONE).date() > hi
                    and not course.get("error")
                )
                missing = (
                    max(0, expected - len(rows))
                    if closed and fresh and expected is not None
                    else None
                )
                result.append(
                    dict(
                        course_id=course["id"],
                        course=course["name"],
                        week=week,
                        published=len(rows),
                        pending=len(pending),
                        expected=expected,
                        missing=missing,
                        closed=closed,
                        fresh=fresh,
                        error=course.get("error", ""),
                    )
                )
        return result
