# Changelog

## 3.0.0 — Evolución

El flujo no cambia: Blackboard → Zoom → YouTube → subtítulos → TXT verificado → limpieza. Todo lo que lo rodea se rehízo.

### Interfaz

- Sistema de diseño nuevo («grafito y esmeralda») en modo claro y oscuro, con contraste AA verificado, iconografía Lucide en toda la app y componentes propios.
- Barra lateral por áreas (Flujo · Estudio · Control) con contador de pendientes y cupo diario siempre visible.
- **Inicio**: qué falta, qué sigue, avance S1–S16 del ciclo, cupo del día y próximas clases por transcribir.
- **Progreso real por paso**: porcentaje, velocidad y tiempo restante de la descarga de Zoom y de la subida a YouTube, y cuenta atrás hasta la siguiente consulta de subtítulos.
- **Paleta de comandos (Ctrl+K)**: ir a cualquier sección, lanzar acciones y buscar texto dentro de todas las clases y materiales. Atajos Ctrl+1…8, F5 y Ctrl+,.
- Avisos no bloqueantes al terminar cada clase o tanda; estados de cada fila como pastillas de color.
- **Biblioteca** con búsqueda por nombre, curso o contenido, orden por columnas y **lector integrado** con búsqueda resaltada.
- Sin desbordes horizontales en el tamaño mínimo de ventana (880 × 560).

### Asistente de estudio

- **Modelos al día y enrutado automático**: la app descubre qué modelos admite tu clave, usa uno ligero para conversar y uno más capaz para guías y simulacros, y salta los que agotaron su cuota hasta que se renueve. Las preferencias con modelos retirados vuelven a Automático.
- **Contexto completo cuando cabe**: con pocas fuentes marcadas el modelo lee el texto íntegro en vez de fragmentos; si excede el límite gratuito por minuto, reintenta solo con los pasajes clave.
- **Memoria de conversación** y varias conversaciones por curso.
- **Citas numeradas y verificables**: cada `[n]` abre el pasaje original (clase, página, diapositiva o minuto aproximado) y la fuente en el lector.
- **Respuesta en streaming** con botón Detener.
- **Tarjetas de repaso** y **simulacros** de opción múltiple con nota, explicación y exportación (CSV para Anki, Markdown); quedan guardados para repetirlos sin gastar cuota.
- Resumen, guía de estudio, conceptos clave y fechas/entregas en un clic.
- Modo «Solo mis fuentes» (estricto) o «Fuentes + conocimiento general» señalado aparte.
- Recuperación híbrida BM25 + embeddings con fusión de puntuaciones normalizadas y tipos de tarea de consulta/documento.

### Motor y fiabilidad

- Esquema SQLite v3 con migración atómica y copia de seguridad automática previa.
- Registro de actividad en disco con datos sensibles ocultos y rotación.
- Inventario, pendientes y portada calculados en una sola lectura; huellas SHA-256 memorizadas por identidad de archivo. La suite pasó de ~97 s a ~47 s con 44 pruebas más.
- Corregido: la sincronización de carpetas reasignaba el curso de una fuente por su nombre de archivo.
- Corregido: carrera al abrir por primera vez la base desde varios hilos.
- Corregido: el filtro Parcial/Final del asistente no reconocía la semana en nombres con fecha (`…_Semana05_2026-09-22_…`).
- Las clases se muestran con nombres legibles («Semana 5 · 22/09/2026») en fuentes y citas; el nombre real sigue en el tooltip y en disco.
- Dependencias auditadas: Pillow 13.0.0 por avisos de seguridad; cierre de ejecución regenerado.
- Opción para buscar clases nuevas al abrir la aplicación.
- 238 pruebas automatizadas con HTTP real bloqueado.

## 2.8.0 — Búsqueda de grabaciones por curso y Asistente IA en la Nube

- **Asistente IA de Estudio en la Nube (Estilo NotebookLM):** pestaña integrada con motor RAG sobre SQLite FTS5 (BM25) + Gemini Free Tier (`gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-embedding-001`) bajo esquema BYOK (Costo $0, sin tarjeta y 0% de ejecución de IA local en la PC).
- Ingesta automática de transcripciones verificadas y carga de materiales de curso (`.pdf`, `.pptx`, `.docx`, `.txt`, `.md`), con selector de ámbito por curso o global, filtro por semanas (Parcial/Final), botones de estudio rápido en 1 clic y exportación a Markdown.
- Selector de un curso o todos los cursos en la pantalla Clases.
- Actualización de cursos desde Blackboard sin abrir Zoom ni preparar grabaciones.
- El curso elegido se filtra antes de consultar Zoom y aplicar los límites de la tanda.
- El filtro por curso se puede combinar con una o varias semanas; sin semanas se revisa el ciclo configurado.
- Al cambiar de curso se retiran los resultados anteriores y se muestran solo sus trabajos guardados.
- Los cursos se conservan para la próxima apertura. Si un curso deja de estar disponible, la búsqueda avisa y no pasa a todos los cursos.
- Demostración sin conexión compatible con la selección de cursos y semanas.
- Pruebas de regresión para aislamiento por ID, semanas, tandas, sesiones caducadas y recuperación local.
- Cierre de la comprobación del ejecutable sin ejecutar callbacks pendientes sobre controles que se están eliminando.

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
