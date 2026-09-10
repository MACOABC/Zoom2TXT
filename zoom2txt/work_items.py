"""Local continuation candidates shared by discovery and the Clases screen."""

from pathlib import Path

from zoom2txt.config import DATA_DIR
from zoom2txt.security import read_private_json
from zoom2txt.storage import StateStore
from zoom2txt.workflow import WorkLedger


def continuation_data(settings, key):
    state = StateStore(settings)
    job = state.get("job", key)
    local = any(job.get(field) and Path(job[field]).is_file() for field in ("video", "txt"))
    private_path = DATA_DIR / "jobs" / state.namespace / (key + ".json")
    private = read_private_json(private_path) if private_path.is_file() else {}
    if not (local or job.get("video_id") or private.get("upload_uri")):
        return None
    cookies = DATA_DIR / "sessions" / state.namespace / (key + ".json")
    return dict(
        play_url=private.get("zoom_url", ""), cookies_file=str(cookies) if cookies.is_file() else ""
    )


def local_continuations(settings, ranges=()):
    ledger = WorkLedger(settings)
    result = []
    for record in ledger.pending():
        stamp = record["start_time"]
        if ranges and not any(lo <= stamp[:10] <= hi for lo, hi in ranges):
            continue
        try:
            data = continuation_data(settings, record["id"])
        except (OSError, ValueError):
            continue
        if data is None:
            continue
        result.append(
            dict(
                course_id=record["course_id"],
                course_name=record["course_name"],
                meeting_id=record["meeting_id"],
                job_key=record["id"],
                name=record["name"],
                start_time=stamp,
                sort_key=stamp,
                **data,
            )
        )
    return result[: settings.batch_size]


def job_status(job):
    if job.get("status") in ("done", "transcribed"):
        return "TXT disponible"
    if job.get("status") == "sync_pending":
        return "Copia pendiente"
    if job.get("video_id"):
        return "Revisar YouTube"
    if job.get("status") in ("uploading", "upload_unknown"):
        return "Recuperar subida"
    if job.get("video"):
        return "Video en esta PC"
    return "Por descargar"
