"""Wizard de configuración inicial.

Se dispara automáticamente cuando faltan credenciales del usuario
actual. Guía: cuenta Zoom (keyring) → proyecto de Google Cloud →
client_secrets.json validado → autorización OAuth → carpetas.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from zoom2txt.auth.credentials import get_zoom_password, set_zoom_password
from zoom2txt.config import (
    CLIENT_SECRETS_FILE,
    DATA_DIR,
    TOKEN_FILE,
    Settings,
    validate_blackboard_url,
    validate_client_secrets,
)
from zoom2txt.gui import style
from zoom2txt.gui.workers import OAuthWorker
from zoom2txt.storage import atomic_write


def _header(title: str, subtitle: str) -> QVBoxLayout:
    box = QVBoxLayout()
    box.setSpacing(8)
    h1 = QLabel(title)
    h1.setObjectName("h1")
    h1.setWordWrap(True)
    sub = QLabel(subtitle)
    sub.setObjectName("muted")
    sub.setWordWrap(True)
    box.addWidget(h1)
    box.addWidget(sub)
    box.addSpacing(16)
    return box


class WelcomePage(QWizardPage):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.addLayout(
            _header(
                "Bienvenido a Zoom2TXT",
                "Configuremos tu cuenta en unos minutos. Todo lo que ingreses "
                "se guarda solo en esta PC, para tu usuario: nadie más "
                "necesita compartirte credenciales ni tú compartir las tuyas.",
            )
        )
        pasos = QLabel(
            "1.  Tu cuenta de Zoom (la contraseña va al almacén seguro del sistema)\n"
            "2.  Tu propio proyecto de Google Cloud con la API de YouTube\n"
            "3.  Autorización de tu cuenta de YouTube\n"
            "4.  Carpetas para videos y transcripciones"
        )
        pasos.setWordWrap(True)
        pasos.setStyleSheet(f"color: {style.MUTED};")
        lay.addWidget(pasos)
        lay.addStretch()


class ZoomPage(QWizardPage):
    def __init__(self, settings: Settings):
        super().__init__()
        self._settings = settings
        lay = QVBoxLayout(self)
        lay.addLayout(
            _header(
                "Cuenta de Zoom",
                "Usuario institucional y contraseña de las grabaciones de Zoom. "
                "La contraseña se guarda cifrada en el almacén de credenciales "
                "de tu sistema operativo, nunca en archivos de texto.",
            )
        )
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.bb_edit = QLineEdit(settings.blackboard_url)
        self.bb_edit.setPlaceholderText("https://campus.example.edu")
        self.user_edit = QLineEdit(settings.zoom_user)
        self.user_edit.setPlaceholderText("usuario@universidad.edu")
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        existing = get_zoom_password(settings.zoom_user) if settings.zoom_user else None
        self.pass_edit.setPlaceholderText(
            "•••••••• (ya guardada, escribe solo para cambiarla)"
            if existing
            else "Contraseña de la grabación / cuenta"
        )
        form.addRow("Blackboard (HTTPS)", self.bb_edit)
        form.addRow("Usuario", self.user_edit)
        form.addRow("Contraseña", self.pass_edit)
        lay.addLayout(form)
        lay.addStretch()
        self.bb_edit.textChanged.connect(self.completeChanged)
        self.user_edit.textChanged.connect(self.completeChanged)
        self.pass_edit.textChanged.connect(self.completeChanged)

    def isComplete(self) -> bool:
        user = self.user_edit.text().strip()
        if not user or not validate_blackboard_url(self.bb_edit.text())[0]:
            return False
        return bool(self.pass_edit.text()) or get_zoom_password(user) is not None

    def validatePage(self) -> bool:
        user = self.user_edit.text().strip()
        base = self.bb_edit.text().strip().rstrip("/")
        valid, message = validate_blackboard_url(base)
        if not valid:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(self, "Dirección de Blackboard inválida", message)
            return False
        self._settings.zoom_user = user
        self._settings.blackboard_url = base
        if self.pass_edit.text():
            try:
                set_zoom_password(user, self.pass_edit.text())
            except Exception:
                from PySide6.QtWidgets import QMessageBox

                QMessageBox.warning(
                    self,
                    "Almacén seguro no disponible",
                    "No se pudo guardar la contraseña en el almacén seguro del sistema. Configúralo antes de continuar; no se guardará en texto plano.",
                )
                return False
        return True


class GoogleCloudPage(QWizardPage):
    """Guía de Google Cloud + selección y validación de client_secrets.json."""

    def __init__(self):
        super().__init__()
        self._valid = (
            CLIENT_SECRETS_FILE.exists() and validate_client_secrets(CLIENT_SECRETS_FILE)[0]
        )
        lay = QVBoxLayout(self)
        lay.addLayout(
            _header(
                "Tu proyecto de Google Cloud",
                "Cada persona usa su propio proyecto gratuito de Google Cloud. "
                "Hazlo una sola vez, en este orden:",
            )
        )
        guia = QLabel(
            "<ol style='margin-left:-18px; line-height:145%;'>"
            f"<li>Entra a <a style='color:{style.LINK}; text-decoration:underline;' "
            "href='https://console.cloud.google.com/projectcreate'>"
            "console.cloud.google.com</a> y crea un <b>proyecto nuevo</b>.</li>"
            "<li>En «APIs y servicios → Biblioteca», busca y <b>habilita "
            "YouTube Data API v3</b>.</li>"
            "<li>En «Pantalla de consentimiento OAuth»: tipo <b>Externo</b>, "
            "y en «Usuarios de prueba» <b>agrega tu propio correo</b>.</li>"
            "<li>En «Credenciales → Crear credencial → ID de cliente OAuth», "
            "elige <b>Aplicación de escritorio</b> y <b>descarga el JSON</b>.</li>"
            "</ol>"
        )
        guia.setWordWrap(True)
        guia.setOpenExternalLinks(True)
        lay.addWidget(guia)
        warning = QLabel(
            "Importante: en modo Testing Google puede pedir nueva autorización a los 7 días. Los proyectos no verificados pueden forzar videos privados; recuperar sus subtítulos usa consultas autorizadas y cuota API. No se garantiza la generación de subtítulos."
        )
        warning.setWordWrap(True)
        warning.setObjectName("muted")
        lay.addWidget(warning)
        lay.addSpacing(16)

        row = QHBoxLayout()
        self.pick_btn = QPushButton("Seleccionar archivo client_secrets.json")
        self.pick_btn.setProperty("variant", "primary")
        self.pick_btn.clicked.connect(self._pick_file)
        row.addWidget(self.pick_btn)
        row.addStretch()
        lay.addLayout(row)

        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        lay.addStretch()
        if self._valid:
            self._set_status_ok(
                "Ya hay una credencial válida guardada. Puedes continuar o reemplazarla."
            )

    def _set_status_ok(self, msg: str) -> None:
        self.status.setText("✓  " + msg)
        self.status.setStyleSheet(f"color: {style.SUCCESS};")

    def _set_status_error(self, msg: str) -> None:
        self.status.setText("✕  " + msg)
        self.status.setStyleSheet(f"color: {style.ERROR};")

    def _pick_file(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Selecciona tu client_secrets.json", str(Path.home()), "JSON (*.json)"
        )
        if not path_str:
            return
        origen = Path(path_str)
        ok, error = validate_client_secrets(origen)
        if not ok:
            self._valid = False
            self._set_status_error(error)
        else:
            try:
                candidate = DATA_DIR / "client_secrets.pending.json"
                atomic_write(candidate, origen.read_bytes())
                self._valid = True
                self._set_status_ok(
                    "Credencial válida preparada. Se activará solo cuando completes la autorización en el siguiente paso; tu cuenta anterior se conserva."
                )
            except OSError:
                self._set_status_error(
                    "No se pudo preparar la credencial. Revisa el acceso a la carpeta de datos."
                )
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self._valid


class OAuthPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self._worker: OAuthWorker | None = None
        lay = QVBoxLayout(self)
        lay.addLayout(
            _header(
                "Autoriza tu cuenta de YouTube",
                "Se abrirá tu navegador para que inicies sesión con TU cuenta "
                "de Google y autorices a la app a subir (y, si tú lo pides al "
                "terminar cada clase, eliminar) videos. Si Google avisa que "
                "la app no está verificada, usa «Continuar» — es tu propio "
                "proyecto y tú eres el usuario de prueba.",
            )
        )
        row = QHBoxLayout()
        self.auth_btn = QPushButton("Conectar con YouTube")
        self.auth_btn.setProperty("variant", "primary")
        self.auth_btn.clicked.connect(self._start_oauth)
        row.addWidget(self.auth_btn)
        row.addStretch()
        lay.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        lay.addStretch()

    def initializePage(self) -> None:
        from zoom2txt.transcription.youtube_engine import token_is_valid

        if not (DATA_DIR / "client_secrets.pending.json").exists() and token_is_valid(TOKEN_FILE):
            self.status.setText(
                "✓  Ya hay una cuenta de YouTube autorizada. Puedes continuar o volver a conectar."
            )
            self.status.setStyleSheet(f"color: {style.SUCCESS};")
        elif TOKEN_FILE.exists():
            self.status.setText(
                "La autorización guardada es de una versión "
                "anterior y ya no alcanza (ahora la app también "
                "puede eliminar videos). Conecta de nuevo."
            )
            self.status.setStyleSheet(f"color: {style.WARNING};")

    def _start_oauth(self) -> None:
        self.auth_btn.setEnabled(False)
        self.status.setText("Esperando la autorización en tu navegador…")
        self.status.setStyleSheet(f"color: {style.MUTED};")
        pending = DATA_DIR / "client_secrets.pending.json"
        self._worker = OAuthWorker(
            client_secrets=pending if pending.exists() else CLIENT_SECRETS_FILE,
            token=DATA_DIR / "token.pending.json",
            parent=self,
        )
        self._worker.succeeded.connect(self._on_ok)
        self._worker.failed.connect(self._on_fail)
        self._worker.start()

    def _on_ok(self) -> None:
        pending = DATA_DIR / "client_secrets.pending.json"
        token = DATA_DIR / "token.pending.json"
        try:
            from zoom2txt.oauth_storage import commit_oauth_change

            commit_oauth_change(pending, token)
        except OSError:
            self._on_fail("No se pudo guardar la autorización. Revisa permisos y espacio de disco.")
            return
        self.auth_btn.setEnabled(True)
        self.status.setText(
            "✓  Cuenta de YouTube conectada. Tu token quedó "
            "guardado en tu carpeta de datos de usuario."
        )
        self.status.setStyleSheet(f"color: {style.SUCCESS};")
        self.completeChanged.emit()

    def _on_fail(self, error: str) -> None:
        self.auth_btn.setEnabled(True)
        self.status.setText(
            "✕  No se pudo completar la autorización: "
            + error
            + "\nRevisa que agregaste tu correo como usuario de prueba y "
            "vuelve a intentar."
        )
        self.status.setStyleSheet(f"color: {style.ERROR};")

    def isComplete(self) -> bool:
        from zoom2txt.transcription.youtube_engine import token_is_valid

        return not (DATA_DIR / "client_secrets.pending.json").exists() and token_is_valid(
            TOKEN_FILE
        )


class FoldersPage(QWizardPage):
    def __init__(self, settings: Settings):
        super().__init__()
        self._settings = settings
        lay = QVBoxLayout(self)
        lay.addLayout(
            _header(
                "Carpetas de trabajo",
                "Dónde guardar los videos descargados y las transcripciones "
                "finales. Puedes cambiarlas luego en Ajustes.",
            )
        )
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.videos_edit = QLineEdit(str(settings.videos_dir))
        self.trans_edit = QLineEdit(str(settings.transcripts_dir))
        form.addRow("Videos", self._with_browse(self.videos_edit))
        form.addRow("Transcripciones", self._with_browse(self.trans_edit))
        lay.addLayout(form)
        lay.addStretch()

    def _with_browse(self, edit: QLineEdit):
        from PySide6.QtWidgets import QWidget

        wrap = QWidget()
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        btn = QPushButton("Examinar…")
        btn.clicked.connect(lambda: self._browse(edit))
        row.addWidget(edit, 1)
        row.addWidget(btn)
        return wrap

    def _browse(self, edit: QLineEdit) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Elige una carpeta", edit.text() or str(Path.home())
        )
        if chosen:
            edit.setText(chosen)

    def validatePage(self) -> bool:
        if any(
            not edit.text().strip() or not Path(edit.text().strip()).is_absolute()
            for edit in (self.videos_edit, self.trans_edit)
        ):
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(
                self,
                "Carpetas inválidas",
                "Selecciona una carpeta completa para videos y transcripciones.",
            )
            return False
        self._settings.videos_dir = Path(self.videos_edit.text().strip())
        self._settings.transcripts_dir = Path(self.trans_edit.text().strip())
        return True


class SetupWizard(QWizard):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("Zoom2TXT — Configuración inicial")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setMinimumSize(680, 560)
        self.setButtonText(QWizard.WizardButton.NextButton, "Siguiente  ›")
        self.setButtonText(QWizard.WizardButton.BackButton, "‹  Atrás")
        self.setButtonText(QWizard.WizardButton.FinishButton, "Terminar")
        self.setButtonText(QWizard.WizardButton.CancelButton, "Cancelar")

        self.addPage(WelcomePage())
        self.addPage(ZoomPage(settings))
        self.addPage(GoogleCloudPage())
        self.addPage(OAuthPage())
        self.addPage(FoldersPage(settings))

    def accept(self) -> None:
        self._settings.wizard_done = True
        self._settings.save()
        super().accept()

    def reject(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        if any(w.isRunning() for w in self.findChildren(OAuthWorker)):
            QMessageBox.information(
                self,
                "Autorización en curso",
                "Completa la autorización en el navegador o espera su límite de 3 minutos antes de cerrar.",
            )
            return
        (DATA_DIR / "client_secrets.pending.json").unlink(missing_ok=True)
        (DATA_DIR / "token.pending.json").unlink(missing_ok=True)
        super().reject()
