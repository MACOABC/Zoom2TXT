"""Ventana de login de Blackboard con navegador integrado.

Restaura cookies cifradas y usa credenciales del campus si se guardaron
explícitamente. El acceso SSO o MFA se completa en la página institucional.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from zoom2txt.config import BB_COOKIES_FILE, WEBENGINE_DIR


class BlackboardLoginDialog(QDialog):
    """exec() → Accepted deja las cookies en self.cookies."""

    def __init__(self, base_url: str, parent=None, user="", password=None):
        super().__init__(parent)
        self.setWindowTitle("Iniciar sesión en Blackboard")
        self.resize(980, 720)
        self._base_url = base_url.rstrip("/")
        self.cookies: list[dict] = []
        self._done = False
        self._user = user
        from zoom2txt.auth.credentials import get_blackboard_password

        self._password = (
            password if password is not None else get_blackboard_password(base_url, user)
        )
        self._submitted = False
        self._checks = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        hint = QLabel(self._initial_hint())
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        hint.setContentsMargins(24, 12, 24, 12)
        hint.setMinimumHeight(56)
        lay.addWidget(hint)
        self._hint = hint
        self._credentials_panel = QWidget(self)
        panel_layout = QVBoxLayout(self._credentials_panel)
        panel_layout.setContentsMargins(24, 4, 24, 12)
        form = QFormLayout()
        self._user_edit = QLineEdit(user)
        self._pass_edit = QLineEdit()
        self._pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._pass_edit.setPlaceholderText(
            "Contraseña de Blackboard · se guarda en el almacén seguro"
        )
        form.addRow("Usuario del campus", self._user_edit)
        form.addRow("Contraseña", self._pass_edit)
        panel_layout.addLayout(form)
        buttons = QHBoxLayout()
        self._save_login_btn = QPushButton("Guardar y conectar automáticamente")
        self._save_login_btn.clicked.connect(self._save_and_connect)
        buttons.addWidget(self._save_login_btn)
        from zoom2txt.auth.credentials import get_zoom_password

        self._legacy_btn = QPushButton("Usar la contraseña guardada en la versión anterior")
        self._legacy_btn.clicked.connect(self._use_legacy_password)
        self._legacy_btn.setVisible(bool(get_zoom_password(user)))
        buttons.addWidget(self._legacy_btn)
        panel_layout.addLayout(buttons)
        lay.addWidget(self._credentials_panel)
        self._credentials_panel.setVisible(not bool(self._password))

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
        self._view.loadFinished.connect(lambda _: self._try_login())
        lay.addWidget(self._view, 1)

        self._restore_cookies()
        self._login_timer = QTimer(self)
        self._login_timer.setInterval(750)
        self._login_timer.timeout.connect(self._try_login)
        if self._password:
            self._login_timer.start()

        self._view.load(QUrl(self._base_url))

    def _initial_hint(self):
        if not self._password:
            return (
                "Todavía no hay una contraseña guardada para este Blackboard. "
                "Si la ingresaste en la versión anterior, pulsa «Usar la contraseña guardada» "
                "para reutilizarla si es la misma que usas en el campus. También puedes guardarla aquí una sola vez."
            )
        return (
            "Reconectando automáticamente con tu sesión y contraseña guardadas de Blackboard. "
            "Si la universidad exige una verificación adicional, complétala en su página."
        )

    def _use_legacy_password(self):
        from zoom2txt.auth.credentials import get_zoom_password

        previous = get_zoom_password(self._user)
        if not previous:
            self._hint.setText(
                "No se encontró la credencial anterior. Guarda tu acceso al campus una sola vez."
            )
            return
        self._store_and_connect(self._user, previous)

    def _save_and_connect(self):
        user = self._user_edit.text().strip()
        password = self._pass_edit.text()
        if not user or not password:
            self._hint.setText(
                "Completa tu usuario y contraseña de Blackboard para guardarlos y conectar."
            )
            return
        if user != self._user:
            self._hint.setText(
                "Para cambiar de cuenta institucional, usa Ajustes → Usuario institucional. Así se mantiene separado el historial de cada cuenta."
            )
            return
        self._store_and_connect(user, password)

    def _store_and_connect(self, user, password):
        from zoom2txt.auth.credentials import set_blackboard_password

        try:
            set_blackboard_password(self._base_url, user, password)
        except Exception:
            self._hint.setText(
                "No se pudo guardar el acceso en el almacén seguro. Puedes entrar en la página del campus; no se guardará la contraseña en texto plano."
            )
            return
        self._user = user
        self._password = password
        self._submitted = False
        self._checks = 0
        self._pass_edit.clear()
        self._credentials_panel.hide()
        self._hint.setText("Acceso de Blackboard guardado. Conectando automáticamente…")
        self._login_timer.start()
        self._try_login()

    def _restore_cookies(self):
        from zoom2txt.security import read_private_json

        try:
            saved = read_private_json(BB_COOKIES_FILE)
            if not isinstance(saved, dict) or saved.get("base") != self._base_url:
                return
            from PySide6.QtCore import QDateTime

            for entry in saved.get("cookies", []):
                host = QUrl(self._base_url).host().lower()
                domain = entry.get("domain", "").lstrip(".").lower()
                if not domain or not (host == domain or host.endswith("." + domain)):
                    continue
                cookie = QNetworkCookie(entry["name"].encode(), entry["value"].encode())
                cookie.setDomain(entry["domain"])
                cookie.setPath(entry.get("path", "/"))
                cookie.setSecure(entry.get("secure", True))
                cookie.setHttpOnly(entry.get("httponly", True))
                if entry.get("expires"):
                    cookie.setExpirationDate(QDateTime.fromSecsSinceEpoch(entry["expires"]))
                self._profile.cookieStore().setCookie(cookie, QUrl(self._base_url))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            return

    def _try_login(self):
        if self._done or self._submitted or not self._password or self._checks >= 30:
            self._login_timer.stop()
            return
        self._checks += 1
        from zoom2txt.blackboard.browser_login import native_login_script

        self._page.runJavaScript(
            native_login_script(self._base_url, self._user, self._password), self._login_result
        )

    def _login_result(self, result):
        if result == "submitted":
            self._submitted = True
            self._password = None
            self._login_timer.stop()
            self._hint.setText(
                "Acceso automático enviado. Si el campus solicita otra comprobación, complétala aquí. No se repetirán intentos con una contraseña rechazada."
            )
        elif result == "external":
            self._hint.setText(
                "Tu universidad usa un proveedor de acceso externo. Inicia sesión o completa su verificación aquí; la contraseña del campus no se enviará automáticamente a otro sitio."
            )
        elif result == "missing" and self._checks >= 30:
            self._hint.setText(
                "El formulario no está disponible para completar el acceso automático. Puedes iniciar sesión en la página; se conservará la sesión para las próximas búsquedas."
            )

    def done(self, result):
        self._login_timer.stop()
        self._password = None
        self._view.stop()
        super().done(result)

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
