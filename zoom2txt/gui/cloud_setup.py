"""Google Cloud setup instructions shared by the wizard and OAuth help."""

import json
from urllib.parse import urlencode

from zoom2txt.config import CLIENT_SECRETS_FILE, DATA_DIR


def configured_project():
    for path in (DATA_DIR / "client_secrets.pending.json", CLIENT_SECRETS_FILE):
        try:
            project = json.loads(path.read_text(encoding="utf-8"))["installed"].get(
                "project_id", ""
            )
            if isinstance(project, str) and project:
                return project
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return ""


def console_url(path, project=""):
    return (
        "https://console.cloud.google.com/"
        + path
        + ("?" + urlencode({"project": project}) if project else "")
    )


STEPS = [
    (
        "Crear o elegir el proyecto",
        "projectcreate",
        "Pulsa <b>Crear proyecto</b>, escribe un nombre como <b>Zoom2TXT laptop</b> "
        "y pulsa <b>Crear</b>. Si ya tienes uno, selecciónalo en la barra superior. "
        "Copia su <b>ID de proyecto</b> (no el nombre) en el campo de arriba para que "
        "los siguientes botones abran ese mismo proyecto.",
    ),
    (
        "Habilitar la API",
        "apis/library/youtube.googleapis.com",
        "Comprueba que arriba esté tu proyecto. La página debe decir <b>YouTube Data API v3</b>. "
        "Pulsa <b>Habilitar</b>. Si aparece <b>Administrar</b>, ya está habilitada. "
        "Regresa a esta ventana y continúa.",
    ),
    (
        "Configurar el consentimiento",
        "auth/overview",
        "En <b>Google Auth Platform</b>, pulsa <b>Comenzar</b> si aparece. "
        "En información de la aplicación escribe <b>Zoom2TXT</b> y elige tu correo de asistencia. "
        "En <b>Público</b> elige <b>Externo</b>. En contacto escribe tu correo. "
        "Revisa las condiciones, acéptalas si estás de acuerdo y pulsa <b>Crear</b>. "
        "Si ya lo configuraste, revisa estos datos en <b>Desarrollo de la marca</b> y <b>Público</b>.",
    ),
    (
        "Agregar tu cuenta como usuario de prueba",
        "auth/audience",
        "En <b>Público → Usuarios de prueba → Agregar usuarios</b>, escribe el correo "
        "de la cuenta de YouTube que usarás y pulsa <b>Guardar</b>. "
        "<b>Debes agregarte aunque seas el creador del proyecto.</b> Mantén el estado <b>En pruebas</b>. "
        "Si autorizas otra cuenta, también debe estar en esta lista. "
        "Este paso evita el error <b>403: access_denied</b> que dice que solo pueden entrar usuarios de prueba.",
    ),
    (
        "Crear el cliente y descargar el JSON",
        "auth/clients",
        "Pulsa <b>Crear cliente</b>. En tipo de aplicación elige <b>Aplicación de escritorio</b>, "
        "escribe <b>Zoom2TXT laptop</b> y pulsa <b>Crear</b>. Descarga el archivo <b>JSON</b> "
        "desde el diálogo o desde el detalle del cliente. "
        "No elijas Aplicación web ni crees una clave de API. "
        "Si ya existe el cliente de escritorio, puedes usar su JSON guardado.",
    ),
    (
        "Seleccionar el archivo descargado",
        "auth/clients",
        "Pulsa <b>Seleccionar archivo JSON</b> abajo. Busca el archivo descargado, "
        "normalmente en <b>Descargas</b>, y ábrelo. Su nombre puede empezar por <b>client_secret_</b>; "
        "no necesitas renombrarlo ni editarlo. La app validará que sea de escritorio. "
        "Después pulsa <b>Siguiente</b> para conectar tu cuenta de YouTube.",
    ),
]
