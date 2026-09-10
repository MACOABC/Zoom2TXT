"""Punto de entrada de Zoom2TXT.

Si faltan credenciales o algún archivo está corrupto, abre el wizard
de configuración en lugar de fallar; los errores inesperados se
muestran en un diálogo, nunca como traceback en consola.
"""

from __future__ import annotations

import sys
import traceback


def install_exception_handler(app):
    """Qt slots do not propagate into the try around app.exec()."""
    from PySide6.QtWidgets import QMessageBox

    from zoom2txt.config import DEBUG_DIR
    from zoom2txt.security import redact
    from zoom2txt.storage import atomic_write

    def report(exc_type, value, tb):
        if getattr(app, "_reporting_error", False):
            return
        app._reporting_error = True
        try:
            try:
                diagnostic = "".join(traceback.format_exception(exc_type, value, tb, limit=5))
                atomic_write(DEBUG_DIR / "interface-error.txt", redact(diagnostic))
            except OSError:
                diagnostic = ""
            QMessageBox.critical(
                app.activeWindow(),
                "No se pudo completar esta acción",
                "Se produjo un error inesperado. Revisa el Historial antes de repetir un trabajo. "
                "Se guardó un diagnóstico con datos sensibles ocultos en la carpeta de datos de Zoom2TXT.\n\n"
                + redact(str(value))[:500],
            )
        finally:
            app._reporting_error = False

    sys.excepthook = report
    return report


def main() -> int:
    # QtWebEngine (login de Blackboard) debe importarse antes de crear
    # la QApplication, y requiere compartir contextos OpenGL.
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    import PySide6.QtWebEngineWidgets  # noqa: F401

    app = QApplication(sys.argv)
    app.setApplicationName("Zoom2TXT")

    from zoom2txt.gui.icon import make_app_icon
    from zoom2txt.gui.style import apply_theme

    if "--demo" in sys.argv:
        import os
        import tempfile
        from pathlib import Path

        demo_session = tempfile.TemporaryDirectory(prefix="zoom2txt-demo-")
        os.environ["ZOOM2TXT_DATA_DIR"] = demo_session.name
        app._demo_session = demo_session
        install_exception_handler(app)
        from zoom2txt.demo import DemoMainWindow, demo_settings

        settings = demo_settings()
        apply_theme(app, "dark")
        window = DemoMainWindow(settings)
        available = app.primaryScreen().availableGeometry()
        window.resize(min(1180, available.width() - 40), min(780, available.height() - 40))
        window.show()
        result = app.exec()
        demo_session.cleanup()
        return result

    app.setWindowIcon(make_app_icon())
    if "--self-test" in sys.argv:
        import hashlib
        import json
        import os
        from pathlib import Path

        if not os.environ.get("ZOOM2TXT_DATA_DIR"):
            return 2
        from zoneinfo import ZoneInfo

        from PySide6.QtGui import QFontDatabase

        from zoom2txt import __version__
        from zoom2txt.config import DATA_DIR, Settings
        from zoom2txt.gui.main_window import MainWindow
        from zoom2txt.gui.settings_dialog import SettingsDialog
        from zoom2txt.security import read_private_json, write_private_json
        from zoom2txt.storage import StateStore, atomic_write

        settings = Settings(
            zoom_user="self-test",
            videos_dir=DATA_DIR / "videos",
            transcripts_dir=DATA_DIR / "txt",
            materials_dir=DATA_DIR / "material",
        )
        apply_theme(app, "dark")
        window = MainWindow(settings)
        dialog = SettingsDialog(settings, window)
        secret = DATA_DIR / "dummy-secret.json"
        write_private_json(secret, {"test": "dummy"})
        state = StateStore(settings)
        state.put("test", "one", ok=True)
        from zoom2txt.workflow import WorkLedger

        ledger = WorkLedger(settings)
        window._navigate(4)
        window._refresh_backlog_notice()
        from zoom2txt.transcription.js_runtime import deno_path

        runtime = deno_path()
        runtime_ok = False
        if runtime:
            try:
                manifest = json.loads(
                    (runtime.parent / "manifest.json").read_text(encoding="utf-8")
                )
                with runtime.open("rb") as stream:
                    runtime_ok = (
                        hashlib.file_digest(stream, "sha256").hexdigest()
                        == manifest["executable_sha256"]
                    )
            except (OSError, KeyError, TypeError, ValueError):
                runtime_ok = False
        result = {
            "version": __version__,
            "font": "Inter" in QFontDatabase.families(),
            "encrypted_storage": read_private_json(secret) == {"test": "dummy"},
            "database": state.get("test", "one").get("ok"),
            "timezone": str(ZoneInfo("America/Lima")),
            "gui_created": True,
            "backlog_created": window.stack.currentWidget() is window.backlog,
            "health_created": window.health.table.columnCount() == 3,
            "daily_budget": ledger.daily()["remaining"] == 6,
            "js_runtime": runtime_ok,
        }
        target = Path(sys.argv[sys.argv.index("--self-test") + 1])
        passed = all(
            result[k]
            for k in (
                "font",
                "encrypted_storage",
                "database",
                "gui_created",
                "backlog_created",
                "health_created",
                "daily_budget",
                "js_runtime",
            )
        )
        result["passed"] = passed
        atomic_write(target, json.dumps(result, indent=2))
        if getattr(window, "tray", None):
            window.tray.hide()
        dialog.deleteLater()
        window.deleteLater()
        app.processEvents()
        return 0 if passed else 1

    install_exception_handler(app)
    try:
        from PySide6.QtCore import QLockFile

        from zoom2txt.config import DATA_DIR, Settings, credentials_status
        from zoom2txt.gui.main_window import MainWindow
        from zoom2txt.gui.setup_wizard import SetupWizard

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(DATA_DIR / "application.lock"))
        if not lock.tryLock(0):
            QMessageBox.information(
                None,
                "Zoom2TXT ya está abierta",
                "Usa la ventana que ya está abierta. Se evita ejecutar la misma clase desde dos instancias.",
            )
            return 0
        app._instance_lock = lock
        from zoom2txt.oauth_storage import recover_oauth_change

        recover_oauth_change()

        settings = Settings.load()
        # v2.4.3: las versiones anteriores guardaban el WebVTT progresivo
        # dentro de un .txt. Migrarlo aquí corrige también la biblioteca ya
        # existente, sin volver a descargar ni subir ninguna clase.
        from pathlib import Path as FilePath

        from zoom2txt.cleaning.subtitle_cleaner import (
            migrar_transcripciones_anteriores,
        )
        from zoom2txt.storage import StateStore, digest

        state = StateStore(settings)
        convertidas = migrar_transcripciones_anteriores(settings.transcripts_dir / state.namespace)
        convertidas += migrar_transcripciones_anteriores(settings.transcripts_dir, recursive=False)
        if settings.drive_dir:
            migrar_transcripciones_anteriores(FilePath(settings.drive_dir), recursive=False)
        if convertidas:
            rutas = {archivo.resolve() for archivo in convertidas}
            for trabajo in state.list("job"):
                try:
                    txt = FilePath(trabajo.get("txt", ""))
                    if txt.resolve() in rutas:
                        state.put("job", trabajo["id"], txt_hash=digest(txt))
                except (OSError, TypeError, ValueError):
                    continue
        from zoom2txt.blackboard.registry import Registry

        Registry(settings)  # Establish ownership of the legacy history before account changes.
        apply_theme(app, settings.theme)
        if credentials_status(settings):
            wizard = SetupWizard(settings)
            if wizard.exec() != wizard.DialogCode.Accepted:
                return 0

        window = MainWindow(settings)
        available = app.primaryScreen().availableGeometry()
        window.resize(min(1180, available.width() - 40), min(780, available.height() - 40))
        window.show()
        return app.exec()
    except Exception:  # noqa: BLE001 — último recurso: diálogo, no traceback
        from zoom2txt.config import DEBUG_DIR
        from zoom2txt.security import redact
        from zoom2txt.storage import atomic_write

        try:
            atomic_write(DEBUG_DIR / "startup-error.txt", redact(traceback.format_exc(limit=5)))
        except OSError:
            pass
        QMessageBox.critical(
            None,
            "Zoom2TXT — no se pudo iniciar",
            "No se borraron tus clases. Revisa permisos, espacio de disco y el almacén seguro de Windows. "
            "El diagnóstico sin credenciales está en la carpeta de datos de Zoom2TXT.\n\n"
            + redact(str(sys.exc_info()[1])),
        )
        return 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        import os

        try:
            exit_code = main()
        except Exception:
            # Report early Qt/import failures instead of leaving an unattended
            # self-test blocked on the windowed bootloader's error dialog.
            import json
            from pathlib import Path

            target = Path(sys.argv[sys.argv.index("--self-test") + 1])
            target.write_text(
                json.dumps({"passed": False, "error": traceback.format_exc(limit=8)}),
                encoding="utf-8",
            )
            exit_code = 1
        # Qt WebEngine can retain helper state while a windowed one-file build
        # tears down. This flag is an isolated package probe, so terminate only
        # after the complete JSON result has been atomically written.
        os._exit(exit_code)
    else:
        raise SystemExit(main())
