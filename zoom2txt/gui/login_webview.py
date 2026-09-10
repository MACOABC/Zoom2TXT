"""Ventana de login de Blackboard con navegador integrado.

El usuario escribe sus credenciales directamente en la página real de
su universidad (la app nunca las ve); al detectar que la sesión quedó
iniciada, se capturan las cookies y la ventana se cierra sola. El
perfil es persistente, así que los siguientes logins suelen ser
automáticos.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout

from zoom2txt.config import WEBENGINE_DIR


class BlackboardLoginDialog(QDialog):
    """exec() → Accepted deja las cookies en self.cookies."""

    def __init__(self, base_url: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Iniciar sesión en Blackboard")
        self.resize(980, 720)
        self._base_url = base_url.rstrip("/")
        self.cookies: list[dict] = []
        self._done = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        hint = QLabel(
            "  Inicia sesión con tu cuenta institucional. La ventana se cerrará sola al entrar."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        hint.setContentsMargins(24, 12, 24, 12)
        hint.setMinimumHeight(56)
        lay.addWidget(hint)

        WEBENGINE_DIR.mkdir(parents=True, exist_ok=True)
        self._profile = QWebEngineProfile(self)
        self._profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
        )
        self._profile.cookieStore().cookieAdded.connect(self._on_cookie)

        self._view = QWebEngineView(self)
        self._page = QWebEnginePage(self._profile, self._view)
        self._view.setPage(self._page)
        self._view.urlChanged.connect(self._on_url_changed)
        lay.addWidget(self._view, 1)

        self._view.load(QUrl(self._base_url))

    def _on_cookie(self, cookie) -> None:
        host = QUrl(self._base_url).host().lower()
        domain = cookie.domain().lstrip(".").lower()
        if not domain or not (host == domain or host.endswith("." + domain)):
            return
        entry = {
            "name": bytes(cookie.name()).decode("utf-8", "replace"),
            "value": bytes(cookie.value()).decode("utf-8", "replace"),
            "domain": cookie.domain(),
            "path": cookie.path() or "/",
            "secure": cookie.isSecure(),
            "httponly": cookie.isHttpOnly(),
            "expires": None
            if cookie.isSessionCookie()
            else cookie.expirationDate().toSecsSinceEpoch(),
        }
        # reemplaza si ya existía (mismo nombre+dominio)
        self.cookies = [
            c
            for c in self.cookies
            if not (
                c["name"] == entry["name"]
                and c["domain"] == entry["domain"]
                and c["path"] == entry["path"]
            )
        ]
        self.cookies.append(entry)

    def _on_url_changed(self, url: QUrl) -> None:
        # Tras el login, Blackboard Ultra aterriza en /ultra/...
        if (
            self._done
            or not url.path().startswith("/ultra")
            or url.host() != QUrl(self._base_url).host()
            or url.scheme() != "https"
        ):
            return
        self._done = True
        # pequeño margen para que lleguen las últimas cookies de sesión
        QTimer.singleShot(1200, self.accept)
