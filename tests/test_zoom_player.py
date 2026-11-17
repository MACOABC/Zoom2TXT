"""Descarga directa del reproductor nuevo de Zoom y respaldo con yt-dlp.
Sesiones HTTP simuladas; ninguna petición real."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import requests
import test_hardening as base

from zoom2txt.download import zoom_downloader as zd
from zoom2txt.progress import ProgressLog

PLAY_URL = "https://campus.zoom.us/rec/play/TOKEN-de-prueba"
API_URL = "https://us02web.zoom.us/nws/recording/2.0/share/ABC?recFileId=F1&pageType=web"
MP4_URL = "https://ssrweb.zoom.us/cmr/replay/2026/11/clase.mp4?Policy=p&Signature=s"
NEW_PAGE = (
    "<html><script>window.__zm_unified_player__ = { enableUseCommonZak: false, "
    'apiDomain: "https://us02web.zoom.us", '
    'playInfoApi: "\\/nws/recording/2.0/share/ABC?recFileId=F1&pageType=web", '
    'isLogin: false, authType: "nak" };</script>You need to enable JavaScript</html>'
)
CLASSIC_PAGE = "<html><script>window.__data__ = {fileId: 'x'}</script></html>"
VIDEO = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 40  # 10 252 bytes con cabecera MP4


class FakeResponse:
    def __init__(self, status=200, text="", payload=None, headers=None, body=b"", fail_at=None):
        self.status_code = status
        self.text = text
        self._payload = payload
        self.headers = headers or {}
        self._body = body
        self._fail_at = fail_at

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def iter_content(self, _size):
        step = 3000
        for start in range(0, len(self._body), step):
            if self._fail_at is not None and start >= self._fail_at:
                raise requests.ConnectionError("conexión cortada")
            yield self._body[start : start + step]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeSession:
    """Sirve la página, la API y el MP4; admite rangos y fallos programados."""

    def __init__(self, page=NEW_PAGE, mp4_url=MP4_URL, video=VIDEO):
        self.page, self.mp4_url, self.video = page, mp4_url, video
        self.calls: list[tuple[str, dict]] = []
        self.forbidden_once = False
        self.fail_at: int | None = None
        self.content_type = "video/mp4"
        self.closed = False

    def get(self, url, headers=None, stream=False, timeout=None):
        headers = dict(headers or {})
        self.calls.append((url, headers))
        if url == PLAY_URL:
            return FakeResponse(text=self.page)
        if url == API_URL:
            return FakeResponse(
                payload={
                    "status": True,
                    "errorCode": 0,
                    "result": {
                        "mediaInfo": {
                            "playUrl": self.mp4_url,
                            "mediaTopic": "Curso de prueba",
                            "mediaDuration": 120,
                        }
                    },
                }
            )
        if url == self.mp4_url:
            if not headers.get("Referer", "").startswith("https://campus.zoom.us"):
                return FakeResponse(status=403, headers={"Content-Type": "text/html"})
            if self.forbidden_once:
                self.forbidden_once = False
                return FakeResponse(status=403, headers={"Content-Type": "text/html"})
            start = 0
            if "Range" in headers:
                start = int(headers["Range"].removeprefix("bytes=").rstrip("-"))
            fail_at, self.fail_at = self.fail_at, None
            total = len(self.video)
            if start >= total:
                return FakeResponse(status=416, headers={"Content-Type": "text/html"})
            return FakeResponse(
                status=206 if start else 200,
                headers={
                    "Content-Type": self.content_type,
                    "Content-Length": str(total - start),
                    **({"Content-Range": f"bytes {start}-{total - 1}/{total}"} if start else {}),
                },
                body=self.video[start:],
                fail_at=fail_at,
            )
        raise AssertionError("petición inesperada: " + url)

    def close(self):
        self.closed = True

    def urls(self):
        return [url for url, _ in self.calls]


class ZoomPlayerTests(unittest.TestCase):
    setUp = base.HardeningTests.setUp
    tearDown = base.HardeningTests.tearDown

    def run_download(self, session, cancel=None, ytdlp=None, name="clase"):
        lines, events = [], []
        log = ProgressLog(lines.append, lambda *args: events.append(args))
        runner = ytdlp or (lambda *a, **k: self.fail("yt-dlp no debía ejecutarse"))
        with (
            patch.object(zd, "_zoom_session", return_value=session),
            patch.object(zd, "run_process", side_effect=runner),
            patch.object(zd, "yt_dlp_path", return_value="yt-dlp"),
        ):
            path = zd.descargar_zoom(PLAY_URL, "", name, self.s.videos_dir, log, None, cancel)
        return path, lines, events

    def test_recognizes_zoom_hosts_and_the_new_player_page(self):
        self.assertTrue(zd.is_zoom_url("https://ulima-edu-pe.zoom.us/rec/play/x"))
        self.assertTrue(zd.is_zoom_url("https://zoom.us/rec/share/x"))
        for bad in (
            "http://campus.zoom.us/rec/play/x",
            "https://zoom.us.example.org/rec/play/x",
            "https://notzoom.us/rec/play/x",
            "https://" + "usuario:clave" + "@campus.zoom.us/rec/play/x",
            "",
        ):
            self.assertFalse(zd.is_zoom_url(bad), bad)
        config = zd.parse_player_config(NEW_PAGE)
        self.assertEqual(config["api_domain"], "https://us02web.zoom.us")
        self.assertEqual(
            config["play_info"], "/nws/recording/2.0/share/ABC?recFileId=F1&pageType=web"
        )
        self.assertIsNone(zd.parse_player_config(CLASSIC_PAGE))
        self.assertIsNone(zd.parse_player_config(""))

    def test_new_player_downloads_directly_without_yt_dlp(self):
        session = FakeSession()
        path, lines, events = self.run_download(session)
        self.assertEqual(path.read_bytes(), VIDEO)
        self.assertEqual(session.urls(), [PLAY_URL, API_URL, MP4_URL])
        self.assertEqual(session.calls[1][1]["Referer"], PLAY_URL)
        self.assertEqual(session.calls[2][1]["Referer"], "https://campus.zoom.us/")
        self.assertTrue(any("reproductor nuevo" in line for line in lines))
        self.assertEqual(lines[-1], "[+] Descarga completada.")
        self.assertEqual(events[-1][1], 1.0)
        self.assertFalse(list(self.s.videos_dir.glob("*.z2tpart")))
        self.assertTrue(session.closed)

    def test_partial_file_is_resumed_with_a_range_request(self):
        part = self.s.videos_dir / "clase.mp4.z2tpart"
        part.write_bytes(VIDEO[:4000])
        session = FakeSession()
        path, lines, _ = self.run_download(session)
        self.assertEqual(session.calls[2][1]["Range"], "bytes=4000-")
        self.assertEqual(path.read_bytes(), VIDEO)
        self.assertTrue(any("retomando" in line for line in lines))

    def test_dropped_connection_resumes_and_expired_link_is_renewed(self):
        session = FakeSession()
        session.fail_at = 6000
        session.forbidden_once = True
        cancel = NS(is_set=lambda: False, wait=lambda _seconds: False)
        path, lines, _ = self.run_download(session, cancel=cancel)
        self.assertEqual(path.read_bytes(), VIDEO)
        # 403 → se vuelve a pedir el enlace; corte → se retoma con Range.
        self.assertEqual(session.urls().count(API_URL), 2)
        ranges = [h.get("Range") for url, h in session.calls if url == MP4_URL]
        self.assertEqual(ranges, [None, None, "bytes=6000-"])
        self.assertTrue(any("retomando la descarga (1/" in line for line in lines))

    def test_pause_keeps_the_partial_file_and_never_falls_back(self):
        session = FakeSession()
        state = {"checks": 0}

        def is_set():
            state["checks"] += 1
            return state["checks"] > 3

        with self.assertRaises(InterruptedError):
            self.run_download(session, cancel=NS(is_set=is_set, wait=lambda _s: True))
        part = self.s.videos_dir / "clase.mp4.z2tpart"
        self.assertTrue(part.is_file())
        self.assertGreater(part.stat().st_size, 0)
        self.assertFalse((self.s.videos_dir / "clase.mp4").exists())

    def test_classic_page_still_uses_yt_dlp_with_the_same_arguments(self):
        session = FakeSession(page=CLASSIC_PAGE)
        seen = {}

        def ytdlp(command, cancel=None, timeout=None, on_line=None):
            seen["command"] = command
            Path(command[command.index("-o") + 1]).write_bytes(VIDEO)
            on_line("[z2t]|10252|10252|NA|1000|0")
            return NS(returncode=0, stderr="")

        path, lines, events = self.run_download(session, ytdlp=ytdlp)
        self.assertEqual(path.read_bytes(), VIDEO)
        self.assertEqual(session.urls(), [PLAY_URL])
        self.assertEqual(seen["command"][:2], ["yt-dlp", PLAY_URL])
        self.assertIn("--progress-template", seen["command"])
        self.assertFalse(any("reproductor nuevo" in line for line in lines))
        self.assertEqual(events[-1][1], 1.0)

    def test_failure_reports_the_real_reasons_without_long_tokens(self):
        session = FakeSession()
        session.content_type = "text/html"  # Zoom devuelve una página en vez del video
        token = "FakeZoomRecordingToken_" + "x" * 28  # identificador largo inventado

        def ytdlp(command, cancel=None, timeout=None, on_line=None):
            return NS(
                returncode=1,
                stderr=f"WARNING: versión antigua\nERROR: [zoom] {token}: Unable to extract data; "
                "please report this issue on https://github.com/yt-dlp/yt-dlp/issues",
            )

        with self.assertRaises(RuntimeError) as ctx:
            self.run_download(session, ytdlp=ytdlp)
        message = str(ctx.exception)
        self.assertTrue(message.startswith("La descarga de Zoom falló."))
        self.assertIn("página en lugar del video", message)
        self.assertIn("Unable to extract data", message)
        self.assertNotIn(token, message)
        self.assertNotIn("github.com", message)

    def test_untrusted_video_host_is_never_contacted(self):
        session = FakeSession(mp4_url="https://videos.example.org/clase.mp4")
        with self.assertRaises(RuntimeError) as ctx:
            self.run_download(session, ytdlp=lambda *a, **k: NS(returncode=1, stderr="ERROR: no"))
        self.assertIn("servidor de video no reconocido", str(ctx.exception))
        self.assertNotIn("https://videos.example.org/clase.mp4", session.urls())

    def test_html_instead_of_video_does_not_leave_a_fake_mp4(self):
        session = FakeSession(video=b"<html>error</html>" * 50)
        with self.assertRaises(RuntimeError):
            self.run_download(session, ytdlp=lambda *a, **k: NS(returncode=1, stderr="ERROR: x"))
        self.assertFalse((self.s.videos_dir / "clase.mp4").exists())
        self.assertFalse(list(self.s.videos_dir.glob("*.z2tpart")))


if __name__ == "__main__":
    unittest.main()
