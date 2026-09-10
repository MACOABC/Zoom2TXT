# Auditoría previa a publicación

Fecha del snapshot: 2026-09-10.

## Alcance

Se creó una copia pública nueva a partir de archivos fuente seleccionados. El directorio original no era un repositorio Git, por lo que esta publicación empieza con historial limpio y no hereda commits antiguos.

## Material excluido

- Ejecutables, ZIP y carpetas de PyInstaller.
- Bases SQLite y resultados de self-test.
- Cookies, tokens OAuth, `client_secrets.json`, configuración y respaldos.
- Videos, audios, VTT, transcripciones y materiales académicos.
- Herramientas internas de desarrollo que no pertenecen al producto.
- Cualquier captura procedente de una cuenta real.

## Correcciones de anonimización

- Se eliminó la URL institucional predeterminada.
- El wizard ahora solicita una URL HTTPS de Blackboard y rechaza URLs con credenciales.
- El demo usa exclusivamente dominios reservados `.invalid`, IDs sintéticos y directorios temporales.
- El binario de Deno se descarga por separado y se valida mediante SHA-256.

## Controles ejecutables

`scripts/privacy_audit.py` falla si encuentra nombres de archivos sensibles, medios académicos, bases de datos, rutas de perfiles Windows, credenciales en URLs, claves privadas, tokens comunes, IDs OAuth reales, correos no ficticios o un host concreto de Blackboard.

GitHub Actions agrega:

- auditoría de privacidad en cada push y pull request;
- Gitleaks sobre el historial completo;
- Ruff y suite de regresión;
- pip-audit sobre dependencias fijadas.

## Resultado local

La copia preparada supera el auditor de privacidad sin hallazgos. Las capturas incluidas fueron generadas desde el modo demo. Este resultado certifica el snapshot inspeccionado, no copias externas que otra persona pudiera haber creado fuera de este repositorio.

## Regla de publicación

Nunca se debe copiar la carpeta de datos del usuario al repositorio. Si un secreto llegara a un commit, eliminar el archivo en otro commit no basta: se debe revocar la credencial, limpiar el historial y volver a ejecutar Gitleaks.
