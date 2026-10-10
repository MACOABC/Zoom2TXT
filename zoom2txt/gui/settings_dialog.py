"""Desktop settings: accessible scroll layout and conservative choices."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from zoom2txt.auth.credentials import set_blackboard_password, set_zoom_password
from zoom2txt.config import BB_COOKIES_FILE, DATA_DIR
from zoom2txt.gui.setup_wizard import SetupWizard


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Ajustes · Zoom2TXT")
        self.resize(760, 680)
        self.setMinimumSize(560, 440)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(8)
        title = QLabel("Tu espacio de trabajo")
        title.setObjectName("h1")
        outer.addWidget(title)
        intro = QLabel(
            "Carpetas, privacidad y preferencias. Las sesiones permanecen protegidas en esta PC."
        )
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        outer.addWidget(intro)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 16, 12, 16)
        lay.setSpacing(24)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        def section(label):
            group = QVBoxLayout()
            group.setSpacing(12)
            heading = QLabel(label)
            heading.setObjectName("h2")
            group.addWidget(heading)
            form = QFormLayout()
            form.setVerticalSpacing(12)
            form.setHorizontalSpacing(16)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            group.addLayout(form)
            lay.addLayout(group)
            return form

        form = section("Archivos")
        self.videos_edit = QLineEdit(str(settings.videos_dir))
        self.trans_edit = QLineEdit(str(settings.transcripts_dir))
        self.mat_edit = QLineEdit(str(settings.materials_dir))
        self.drive_edit = QLineEdit(settings.drive_dir)
        self.drive_edit.setPlaceholderText("Opcional · segunda copia local")
        for label, edit in (
            ("Videos temporales", self.videos_edit),
            ("Transcripciones", self.trans_edit),
            ("Material de cursos", self.mat_edit),
            ("Carpeta de copia", self.drive_edit),
        ):
            form.addRow(label, self._with_browse(edit))
        note = QLabel(
            "Una carpeta de Drive es una copia local: esta app no confirma la llegada a la nube. Puedes reintentar copias pendientes desde Historial."
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        lay.addWidget(note)
        form = section("Procesamiento y privacidad")
        self.batch = QSpinBox()
        self.batch.setMaximumWidth(120)
        self.batch.setRange(1, 6)
        self.batch.setValue(settings.batch_size)
        form.addRow("Clases por tanda", self.batch)
        self.longest = QCheckBox("Proponer la grabación más larga de cada día")
        self.longest.setChecked(settings.longest_per_day)
        self.longest.setToolTip(
            "No borra ni omite las demás. Desactívalo para ver todas las sesiones."
        )
        form.addRow("", self.longest)
        self.visibility = QComboBox()
        self.visibility.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.visibility.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.visibility.setMinimumContentsLength(24)
        self.visibility.addItem("Privado · consultas autorizadas con cuota API", True)
        self.visibility.addItem("No listado · cualquier persona con enlace puede verlo", False)
        self.visibility.setCurrentIndex(0 if settings.youtube_private else 1)
        form.addRow("Video en YouTube", self.visibility)
        privacy_note = QLabel(
            "Se aplica a las nuevas subidas, no a videos existentes ni a sesiones interrumpidas. "
            "Google puede restringir a Privado las subidas de proyectos API no verificados."
        )
        privacy_note.setWordWrap(True)
        privacy_note.setObjectName("muted")
        form.addRow("", privacy_note)
        self.del_video_chk = QCheckBox("Eliminar video local tras verificar TXT y copia solicitada")
        self.del_video_chk.setChecked(settings.delete_local_video)
        form.addRow("", self.del_video_chk)
        self.delete_youtube = QCheckBox(
            "Eliminar automáticamente el video remoto tras verificar su TXT"
        )
        self.delete_youtube.setChecked(True)
        self.delete_youtube.setEnabled(False)
        self.delete_youtube.setToolTip(
            "Obligatorio tras guardar y verificar el TXT. Los fallos de conexión quedan pendientes con reintentos."
        )
        form.addRow("", self.delete_youtube)
        note = QLabel(
            "Máximo de 6 clases nuevas por día en esta app, sumando todas las tandas. Se renueva a las 00:00 de Lima, aunque cierres la app. Reintentar la misma clase ese día no consume otra plaza. Este límite no es la cuota de Google; las consultas de subtítulos privados sí usan cuota API."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        lay.addWidget(note)
        form = section("Cuenta y apariencia")
        self.user_edit = QLineEdit(settings.zoom_user)
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_edit.setPlaceholderText("Dejar vacío para conservar")
        self.bb_edit = QLineEdit(settings.blackboard_url)
        self.bb_pass_edit = QLineEdit()
        self.bb_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.bb_pass_edit.setPlaceholderText(
            "Opcional · guardar para el acceso automático al campus"
        )
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Oscuro", "dark")
        self.theme_combo.addItem("Claro", "light")
        self.theme_combo.setCurrentIndex(1 if settings.theme == "light" else 0)
        form.addRow("Usuario institucional", self.user_edit)
        form.addRow("Contraseña de Zoom · solo modo manual", self.pass_edit)
        form.addRow("Contraseña de Blackboard", self.bb_pass_edit)
        form.addRow("Blackboard (HTTPS)", self.bb_edit)
        form.addRow("Tema", self.theme_combo)
        for label, callback in (
            ("Abrir asistente de cuentas", self._reopen_wizard),
            ("Olvidar sesión de Blackboard", self._reset_blackboard),
            ("Abrir carpeta de datos", self._open_data_dir),
        ):
            button = QPushButton(label)
            if label == "Olvidar sesión de Blackboard":
                button.setProperty("variant", "danger")
            button.clicked.connect(callback)
            lay.addWidget(button)

        backup_form = section("Respaldo y recuperación")
        backup_note = QLabel(
            "Guarda el historial, la cola y los estados de trabajo para migrarlos o recuperarlos. "
            "No incluye videos, transcripciones, contraseñas, cookies ni credenciales de Google."
        )
        backup_note.setObjectName("muted")
        backup_note.setWordWrap(True)
        backup_form.addRow(backup_note)
        backup_actions = QWidget()
        backup_row = QHBoxLayout(backup_actions)
        backup_row.setContentsMargins(0, 0, 0, 0)
        backup_row.setSpacing(8)
        export_backup = QPushButton("Exportar historial")
        export_backup.clicked.connect(self._export_history)
        restore_backup = QPushButton("Restaurar historial")
        restore_backup.clicked.connect(self._restore_history)
        backup_row.addWidget(export_backup)
        backup_row.addWidget(restore_backup)
        backup_row.addStretch()
        backup_form.addRow(backup_actions)
        lay.addStretch()
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 8, 0, 0)
        buttons.addStretch()
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Guardar cambios")
        save.setProperty("variant", "primary")
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        outer.addLayout(buttons)

    def _with_browse(self, edit):
        wrap = QWidget()
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        btn = QPushButton("Elegir…")
        btn.clicked.connect(lambda: self._browse(edit))
        row.addWidget(edit, 1)
        row.addWidget(btn)
        return wrap

    def _browse(self, edit):
        chosen = QFileDialog.getExistingDirectory(
            self, "Elige una carpeta", edit.text() or str(Path.home())
        )
        if chosen:
            edit.setText(chosen)

    def _reopen_wizard(self):
        SetupWizard(self.settings, self).exec()
        self.user_edit.setText(self.settings.zoom_user)

    def _reset_blackboard(self):
        if (
            QMessageBox.question(
                self,
                "Cerrar sesión",
                "Se eliminarán las copias de sesión de Blackboard de esta PC. Tus clases e historial se conservan.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        BB_COOKIES_FILE.unlink(missing_ok=True)
        from zoom2txt.auth.credentials import delete_blackboard_password

        delete_blackboard_password(self.settings.blackboard_url, self.settings.zoom_user)
        (DATA_DIR / "yt_cookies.txt").unlink(missing_ok=True)
        from zoom2txt.storage import StateStore

        folder = DATA_DIR / "sessions" / StateStore(self.settings).namespace
        if folder.is_dir():
            for file in folder.glob("*.json"):
                file.unlink()
        QMessageBox.information(
            self, "Sesión cerrada", "La próxima búsqueda pedirá iniciar sesión."
        )

    def _open_data_dir(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(DATA_DIR)))

    def _export_history(self):
        from zoom2txt.backup import export_history

        folder = Path.home() / "Documents"
        if not folder.is_dir():
            folder = Path.home()
        default = folder / (f"Zoom2TXT-historial-{datetime.now(UTC):%Y-%m-%d}.zip")
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar historial sin credenciales",
            str(default),
            "Respaldo Zoom2TXT (*.zip)",
        )
        if not selected:
            return
        try:
            destination = export_history(self.settings, Path(selected))
            QMessageBox.information(
                self,
                "Respaldo creado",
                "Se guardó el historial sin credenciales ni archivos de clase en:\n"
                + str(destination),
            )
        except Exception as exc:  # noqa: BLE001 — GUI boundary
            from zoom2txt.security import redact

            QMessageBox.warning(
                self,
                "No se pudo exportar",
                "El historial local no cambió. Elige otra ubicación.\n\n" + redact(exc),
            )

    def _restore_history(self):
        from zoom2txt.backup import restore_history

        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Restaurar historial de Zoom2TXT",
            str(Path.home()),
            "Respaldo Zoom2TXT (*.zip)",
        )
        if not selected:
            return
        if (
            QMessageBox.question(
                self,
                "Restaurar historial",
                "Se combinarán la cola y el historial del respaldo con este perfil. "
                "No se reemplazan credenciales ni archivos de clase. ¿Continuar?",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            count, safety_copy = restore_history(self.settings, Path(selected))
            QMessageBox.information(
                self,
                "Historial restaurado",
                f"Se incorporaron {count} registros. Zoom2TXT creó antes una copia de seguridad local:\n"
                + str(safety_copy),
            )
        except Exception as exc:  # noqa: BLE001 — untrusted backup boundary
            from zoom2txt.security import redact

            QMessageBox.warning(
                self,
                "Respaldo no restaurado",
                "No se modificó el historial. Verifica que sea un ZIP exportado por Zoom2TXT.\n\n"
                + redact(exc),
            )

    def _save(self):
        try:
            edits = [self.videos_edit, self.trans_edit, self.mat_edit]
            if any(not e.text().strip() or not Path(e.text().strip()).is_absolute() for e in edits):
                raise ValueError(
                    "Elige carpetas completas para videos, transcripciones y material."
                )
            drive = self.drive_edit.text().strip()
            if drive and (not Path(drive).is_absolute() or not Path(drive).is_dir()):
                raise ValueError(
                    "La carpeta de copia debe existir y estar conectada, o puedes dejarla vacía."
                )
            base = self.bb_edit.text().strip().rstrip("/")
            parsed = urlparse(base)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError(
                    "La dirección de Blackboard debe ser HTTPS y no incluir credenciales."
                )
            user = self.user_edit.text().strip()
            if not user:
                raise ValueError("Escribe tu usuario institucional.")
            changed = user != self.settings.zoom_user or base != self.settings.blackboard_url
            if (
                changed
                and QMessageBox.question(
                    self,
                    "Cambiar perfil",
                    "Se usará un historial separado y se cerrará la sesión de Blackboard. No se eliminarán tus archivos. ¿Continuar?",
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            updated = replace(
                self.settings,
                videos_dir=Path(edits[0].text().strip()).resolve(),
                transcripts_dir=Path(edits[1].text().strip()).resolve(),
                materials_dir=Path(edits[2].text().strip()).resolve(),
                zoom_user=user,
                blackboard_url=base,
                drive_dir=drive,
                theme=self.theme_combo.currentData(),
                batch_size=self.batch.value(),
                longest_per_day=self.longest.isChecked(),
                youtube_private=self.visibility.currentData(),
                auto_delete_youtube=True,
                delete_local_video=self.del_video_chk.isChecked(),
            )
            if self.pass_edit.text():
                set_zoom_password(user, self.pass_edit.text())
            if self.bb_pass_edit.text():
                set_blackboard_password(base, user, self.bb_pass_edit.text())
            updated.save()
            if changed:
                BB_COOKIES_FILE.unlink(missing_ok=True)
            self.settings.__dict__.update(updated.__dict__)
            self.accept()
        except Exception as exc:  # noqa: BLE001 — GUI validation boundary
            from zoom2txt.security import redact

            QMessageBox.warning(self, "Revisa los ajustes", redact(exc))
