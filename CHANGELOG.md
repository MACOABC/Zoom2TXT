# Changelog

## 2.7.1 — Recuperación del acceso guardado

- La ventana de acceso explica cuándo todavía falta la credencial de Blackboard.
- Permite reutilizar explícitamente la contraseña guardada por la versión anterior sin volver a escribirla.
- Permite guardar el acceso del campus desde la propia ventana de conexión.
- El acceso automático utiliza el botón nativo de inicio de sesión para conservar sus campos y controladores.

## 2.7.0 — Configuración guiada y procesamiento en lote

- Asistente de Google Cloud en seis pasos con enlaces al proyecto seleccionado.
- Instrucciones explícitas para usuarios de prueba y recuperación del error 403.
- Preparación de toda la tanda antes de consultar subtítulos por turnos; una clase lenta no bloquea las demás.
- Eliminación remota obligatoria tras verificar el TXT, independiente de la copia opcional, con reintentos persistentes.
- Acceso a Blackboard separado de la clave de grabaciones de Zoom, sesión cifrada reutilizable y acceso automático al formulario nativo cuando se guarda la contraseña del campus.
- Los fallos de red no se confunden con una sesión caducada; SSO y verificaciones institucionales mantienen su intervención necesaria.
- Estados de progreso simplificados y destino de procesamiento explicado en la configuración.
- Pruebas de regresión sin conexiones reales para cola, limpieza, configuración y acceso.

## 2.6.0 — Portfolio edition

- Recuperación automática integrada en la pantalla Clases.
- Reutilización de MP4 local y reconciliación de subidas inciertas.
- Estado persistente por clase y continuación en lote.
- Modo demo completamente sintético y sin red.
- URL de Blackboard configurada por cada usuario, sin institución predeterminada.
- Auditoría de privacidad, CI, Gitleaks y documentación de arquitectura.
