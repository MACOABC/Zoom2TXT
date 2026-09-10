"""Read-only reconciliation within the authenticated user's uploads playlist."""

import re


def owned_channel(youtube):
    response = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = response.get("items") if isinstance(response, dict) else None
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise RuntimeError(
            "No se pudo comprobar tu canal. Vuelve a continuar cuando YouTube responda."
        )
    channel = items[0]
    playlist = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    if not isinstance(channel.get("id"), str) or not channel["id"] or not playlist:
        raise RuntimeError("La respuesta del canal está incompleta. No se inició otra subida.")
    return channel["id"]


def video_details(youtube, video_id):
    response = youtube.videos().list(part="status,processingDetails,snippet", id=video_id).execute()
    items = response.get("items") if isinstance(response, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("YouTube no devolvió un estado válido del video. Vuelve a continuar.")
    if not items:
        return None
    if len(items) != 1 or not isinstance(items[0], dict) or items[0].get("id") != video_id:
        raise RuntimeError("YouTube devolvió un video inesperado. Se conserva el trabajo.")
    if not isinstance(items[0].get("status"), dict):
        raise RuntimeError("YouTube no devolvió el estado del video. Se conserva el trabajo.")
    return items[0]


def find_existing_upload(youtube, title, job_key, cancel, max_pages=4, *, strict=False):
    """Return one unambiguous owned video, otherwise fail closed.

    Modern jobs carry an exact marker; legacy names must include the job's
    hash suffix. An arbitrary repeated lesson title is not a safe identity.
    """
    channels = youtube.channels().list(part="contentDetails", mine=True).execute()
    if not isinstance(channels, dict) or not isinstance(channels.get("items"), list):
        raise RuntimeError("No se pudo revisar el canal de YouTube. No se iniciará otra subida.")
    playlists = [
        c.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        for c in channels.get("items", [])
    ]
    if len(playlists) != 1 or not playlists[0]:
        raise RuntimeError(
            "No se pudo identificar un único canal propio. Vincula el ID de la clase desde Historial; no se repetirá la subida."
        )
    marker = "Zoom2TXT: " + job_key
    legacy = bool(
        re.fullmatch(r"[0-9a-f]{24}", job_key)
        and title.endswith("_" + job_key[:8])
        and len(title) <= 100
    )
    matches = set()
    page = None
    visited = set()
    count = 0
    for _ in range(max_pages):
        if cancel.is_set():
            raise InterruptedError("Revisión de YouTube pausada.")
        params = dict(part="snippet,contentDetails", playlistId=playlists[0], maxResults=50)
        if page:
            params["pageToken"] = page
        result = youtube.playlistItems().list(**params).execute()
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise RuntimeError(
                "La revisión del canal está incompleta. Vuelve a continuar; no se inicia otra subida."
            )
        count += len(result["items"])
        for item in result["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("snippet"), dict):
                raise RuntimeError("YouTube devolvió datos incompletos. Vuelve a continuar.")
            snippet = item.get("snippet", {})
            if strict and (
                not isinstance(snippet.get("title"), str)
                or not isinstance(snippet.get("description"), str)
            ):
                raise RuntimeError(
                    "Faltan datos para identificar las clases de tu canal. Vuelve a continuar."
                )
            exact_marker = marker in snippet.get("description", "").splitlines()
            if exact_marker or (legacy and snippet.get("title") == title):
                video_id = item.get("contentDetails", {}).get("videoId", "")
                if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                    matches.add(video_id)
        page = result.get("nextPageToken")
        if page and (not isinstance(page, str) or page in visited):
            raise RuntimeError(
                "La revisión del canal fue parcial: YouTube repitió una página. Vuelve a continuar."
            )
        if page:
            visited.add(page)
        if not page:
            total = result.get("pageInfo", {}).get("totalResults")
            if strict and isinstance(total, int) and count < total:
                raise RuntimeError(
                    "La revisión del canal fue parcial: faltan videos por revisar. Vuelve a continuar."
                )
            break
    if page:
        raise RuntimeError(
            f"La revisión del canal fue parcial ({max_pages * 50} videos). No se iniciará otra subida."
        )
    if len(matches) > 1:
        raise RuntimeError(
            "Hay varios videos que coinciden con esta clase. Revisa YouTube Studio y usa Vincular ID; no se elegirá ni subirá otro automáticamente."
        )
    return next(iter(matches), None)
