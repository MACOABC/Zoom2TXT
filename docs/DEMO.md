# Modo demo

El modo demo existe para entrevistas, revisión de código y capturas de portafolio. Enseña el producto sin requerir una cuenta institucional.

## Ejecutar

```powershell
python main.py --demo
```

## Garantías

- Perfil temporal nuevo en cada ejecución.
- Tres cursos e identificadores ficticios.
- Ninguna llamada a Blackboard, Zoom, Google o YouTube.
- Configuración, historial y credenciales reales inaccesibles desde la demo.
- Secciones que requieren cuentas deshabilitadas.
- Archivos sintéticos eliminados al cerrar.

## Regenerar imágenes públicas

```powershell
python scripts/capture_portfolio.py
```

El script crea las imágenes de `docs/images/` utilizando exactamente el modo demo. Antes de publicar nuevas capturas, ejecuta también `python scripts/privacy_audit.py`.
