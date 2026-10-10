"""Wizard de configuración inicial.

Se dispara automáticamente cuando faltan credenciales del usuario
actual. Guía: sesión de Blackboard → proyecto de Google Cloud →
client_secrets.json validado → autorización OAuth → carpetas.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from zoom2txt.auth.credentials import get_blackboard_password, set_blackboard_password
from zoom2txt.config import (
    BB_COOKIES_FILE,
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
                "Configuremos el acceso a tu campus y a tu cuenta de transcripción. "
                "Tus credenciales y sesiones se guardan protegidas en esta PC.",
            )
        )
        pasos = QLabel(
            "1.  Acceso a Blackboard y sesión reutilizable\n"
            "2.  Tu propio proyecto de Google Cloud con la API de YouTube\n"
            "3.  Autorización de tu cuenta de YouTube\n"
            "4.  Carpetas para videos y transcripciones\n\n"
            "Las grabaciones se procesan temporalmente en tu cuenta de YouTube. "
            "El video remoto se elimina automáticamente tras verificar el TXT. "
            "Si no hay conexión, el borrado queda pendiente y se reintenta al abrir la app."
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
                "Conecta tu Blackboard",
                "Usa tu cuenta del campus. La sesión se guarda cifrada y se reutiliza al buscar clases. "
                "Puedes guardar la contraseña para el acceso automático al formulario de Blackboard. "
                "Si tu universidad usa SSO, MFA o CAPTCHA, completa ese paso en su página. "
                "La contraseña de una grabación de Zoom no es necesaria para buscar clases.",
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
        existing = (
            get_blackboard_password(settings.blackboard_url, settings.zoom_user)
            if settings.zoom_user
            else None
        )
        self.pass_edit.setPlaceholderText(
            "•••••••• (ya guardada, escribe solo para cambiarla)"
            if existing
            else "Opcional · contraseña del campus para acceso automático"
        )
        form.addRow("Blackboard (HTTPS)", self.bb_edit)
        form.addRow("Usuario", self.user_edit)
        form.addRow("Contraseña de Blackboard", self.pass_edit)
        lay.addLayout(form)
        self.connect_btn = QPushButton("Conectar Blackboard ahora")
        self.connect_btn.clicked.connect(self._connect_blackboard)
        lay.addWidget(self.connect_btn)
        self.connection_status = QLabel(
            "Conecta una vez para guardar tu sesión. Si ya existe y sigue vigente, buscar clases no pedirá entrar de nuevo."
        )
        self.connection_status.setWordWrap(True)
        lay.addWidget(self.connection_status)
        lay.addStretch()
        self.bb_edit.textChanged.connect(self.completeChanged)
        self.user_edit.textChanged.connect(self.completeChanged)
        self.pass_edit.textChanged.connect(self.completeChanged)

    def isComplete(self) -> bool:
        user = self.user_edit.text().strip()
        if not user or not validate_blackboard_url(self.bb_edit.text())[0]:
            return False
        return True

    def validatePage(self) -> bool:
        user = self.user_edit.text().strip()
        base = self.bb_edit.text().strip().rstrip("/")
        valid, message = validate_blackboard_url(base)
        if not valid:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(self, "Dirección de Blackboard inválida", message)
            return False
        if user != self._settings.zoom_user or base != self._settings.blackboard_url:
            BB_COOKIES_FILE.unlink(missing_ok=True)
        self._settings.zoom_user = user
        self._settings.blackboard_url = base
        if self.pass_edit.text():
            try:
                set_blackboard_password(base, user, self.pass_edit.text())
            except Exception:
                from PySide6.QtWidgets import QMessageBox

                QMessageBox.warning(
                    self,
                    "Almacén seguro no disponible",
                    "No se pudo guardar la contraseña en el almacén seguro del sistema. Configúralo antes de continuar; no se guardará en texto plano.",
                )
                return False
        return True

    def _connect_blackboard(self):
        if not self.isComplete() or not self.validatePage():
            self.connection_status.setText(
                "Escribe la dirección HTTPS de Blackboard y tu usuario institucional."
            )
            return
        from zoom2txt.blackboard.client import BlackboardClient
        from zoom2txt.gui.login_webview import BlackboardLoginDialog

        dialog = BlackboardLoginDialog(
            self._settings.blackboard_url, self, user=self._settings.zoom_user
        )
        if dialog.exec() == dialog.DialogCode.Accepted:
            client = BlackboardClient(self._settings.blackboard_url)
            try:
                client.store_cookies(dialog.cookies)
            finally:
                client.session.close()
            self.connection_status.setText(
                "Sesión de Blackboard guardada. Se reutilizará automáticamente mientras sea válida."
            )
        else:
            self.connection_status.setText(
                "Acceso pendiente. Puedes volver a conectar ahora o al buscar clases."
            )


class GoogleCloudPage(QWizardPage):
    """Guía de Google Cloud + selección y validación de client_secrets.json."""

    def __init__(self):
        super().__init__()
        self._valid = (
            CLIENT_SECRETS_FILE.exists() and validate_client_secrets(CLIENT_SECRETS_FILE)[0]
        )
        from zoom2txt.gui.cloud_setup import STEPS, configured_project

        lay = QVBoxLayout(self)
        lay.addLayout(
            _header(
                "Configura Google, paso a paso",
                "Completa cada paso en Google Cloud y vuelve aquí. Los botones abren la sección indicada.",
            )
        )
        self.project_edit = QLineEdit(configured_project())
        self.project_edit.setPlaceholderText(
            "ID del proyecto, por ejemplo: zoom2txt-laptop (opcional)"
        )
        lay.addWidget(QLabel("ID de proyecto · úsalo para abrir siempre el mismo proyecto"))
        lay.addWidget(self.project_edit)
        self.step_label = QLabel()
        self.step_label.setObjectName("h2")
        lay.addWidget(self.step_label)
        self.steps = QStackedWidget()
        for _title, _, text in STEPS:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            body = QWidget()
            content = QVBoxLayout(body)
            label = QLabel(text)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.RichText)
            content.addWidget(label)
            content.addStretch()
            scroll.setWidget(body)
            self.steps.addWidget(scroll)
        lay.addWidget(self.steps, 1)
        self.open_step_btn = QPushButton("Abrir este paso en Google Cloud")
        self.open_step_btn.clicked.connect(self._open_step)
        lay.addWidget(self.open_step_btn)
        navigation = QHBoxLayout()
        self.prev_step = QPushButton("Paso anterior")
        self.next_step = QPushButton("Paso siguiente")
        self.prev_step.clicked.connect(lambda: self._show_step(self.steps.currentIndex() - 1))
        self.next_step.clicked.connect(lambda: self._show_step(self.steps.currentIndex() + 1))
        navigation.addWidget(self.prev_step)
        navigation.addWidget(self.next_step)
        lay.addLayout(navigation)
        self.pick_btn = QPushButton("Seleccionar archivo JSON")
        self.pick_btn.setProperty("variant", "primary")
        self.pick_btn.clicked.connect(self._pick_file)
        lay.addWidget(self.pick_btn)
        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self._show_step(len(STEPS) - 1 if self._valid else 0)
        if self._valid:
            self._set_status_ok(
                "Credencial de escritorio guardada. Puedes continuar o revisar los pasos anteriores."
            )

    def _show_step(self, index):
        from zoom2txt.gui.cloud_setup import STEPS

        self.steps.setCurrentIndex(index)
        self.step_label.setText(f"Paso {index + 1} de {len(STEPS)} · {STEPS[index][0]}")
        self.prev_step.setEnabled(index > 0)
        self.next_step.setEnabled(index < len(STEPS) - 1)
        self.pick_btn.setVisible(index == len(STEPS) - 1)
        self.open_step_btn.setVisible(index != len(STEPS) - 1)

    def _open_step(self):
        from zoom2txt.gui.cloud_setup import STEPS, console_url

        QDesktopServices.openUrl(
            QUrl(console_url(STEPS[self.steps.currentIndex()][1], self.project_edit.text().strip()))
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
                "de Google y autorices el procesamiento temporal de tus grabaciones en YouTube. "
                "Cada video se elimina automáticamente después de guardar y verificar su texto. "
                "Usa la misma cuenta que agregaste como usuario de prueba. "
                "En modo En pruebas, Google puede pedir renovar el acceso a los 7 días.",
            )
        )
        row = QHBoxLayout()
        self.auth_btn = QPushButton("Conectar con YouTube")
        self.auth_btn.setProperty("variant", "primary")
        self.auth_btn.clicked.connect(self._start_oauth)
        row.addWidget(self.auth_btn)
        row.addStretch()
        lay.addLayout(row)
        help_text = QLabel(
            "Si aparece «403: access_denied» o «solo verificadores aprobados», agrega tu correo en Público → Usuarios de prueba → Agregar usuarios → Guardar, aunque seas el creador. Luego vuelve a conectar. Si Google muestra «app no verificada», comprueba que sea tu proyecto antes de continuar."
        )
        help_text.setWordWrap(True)
        lay.addWidget(help_text)
        self.audience_btn = QPushButton("Abrir Usuarios de prueba de mi proyecto")
        self.audience_btn.clicked.connect(self._open_audience)
        lay.addWidget(self.audience_btn)
        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        lay.addStretch()

    def _open_audience(self):
        from zoom2txt.gui.cloud_setup import configured_project, console_url

        QDesktopServices.openUrl(QUrl(console_url("auth/audience", configured_project())))

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
        self.setMinimumSize(720, 620)
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
