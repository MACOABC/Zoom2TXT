# Arquitectura

Zoom2TXT separa la interfaz de las integraciones y persiste cada transición crítica. La meta no es solo automatizar el camino feliz: es poder continuar después de cerrar la aplicación, perder conexión o encontrar una subida incierta.

## Componentes

```mermaid
flowchart TB
    GUI[GUI PySide6] --> Workers[Workers QThread]
    Workers --> Pipeline[Pipeline recuperable]
    Workers --> Blackboard[Cliente Blackboard]
    Blackboard --> Zoom[Zoom LTI]
    Pipeline --> Downloader[Descargador yt-dlp]
    Pipeline --> YouTube[YouTube Data API]
    Pipeline --> Captions[Extractor de subtítulos]
    Pipeline --> Store[(SQLite + FTS5 + estado cifrado)]
    Captions --> Files[TXT verificado]
    Files --> RAG[Índice local + contexto y citas]
    Store --> RAG
    RAG --> Gateway[Puerta Gemini · enrutado de modelos]
    Store --> Keyring[Keyring del sistema]
```

### Asistente de estudio

Tres módulos con responsabilidades separadas:

- **`ai_models.py` — catálogo y enrutado.** Google retira modelos varias veces al año y las cuotas gratuitas son de pocas decenas de solicitudes diarias en los modelos más capaces. La app parte de un catálogo conocido, lo completa con `models.list()` de la clave del usuario y, en modo Automático, ordena por tarea (ligero para conversar, capaz para guías y simulacros). Un 429 diario marca el modelo como agotado hasta la medianoche del Pacífico; un 404 lo retira un día. Lleva un contador local de uso, que no es el saldo real.
- **`ai_gateway.py` — puerta única.** Generación normal, en streaming y JSON; embeddings; clasificación de errores en categorías accionables y mensajes en lenguaje claro. Si un modelo falla a mitad de una respuesta, la interfaz descarta lo parcial y continúa con el siguiente.
- **`rag_engine.py` — conocimiento.** Ingesta por hash en `knowledge_sources`/`knowledge_chunks` con índice FTS5; recuperación BM25 con re-ranking semántico opcional (combinación convexa de puntuaciones normalizadas, embeddings en caché). `build_context` decide qué recibe el modelo:

| Situación | Contexto |
|---|---|
| Las fuentes marcadas caben en el presupuesto | Texto íntegro de cada fuente (`full`) |
| No caben y es una pregunta | Fragmentos más relevantes, fusionando los contiguos (`retrieval`) |
| No caben y es una guía o simulacro | Muestra pareja de todas las fuentes (`sample`) |
| Sin fuentes | Conocimiento general, avisado en la respuesta (`general`) |

Cada pasaje lleva un número; el modelo cita con `[n]` y solo los números que existen se convierten en enlaces al texto original. Las tarjetas y simulacros se piden como JSON, se validan y se guardan para repetirlos sin gastar cuota.

### GUI

`zoom2txt/gui/` contiene el sistema de diseño (`style.py`, `widgets.py`), la ventana principal, Inicio, Biblioteca con lector, Asistente, diálogos de estudio, paleta de comandos, wizard inicial y recuperación. Las operaciones largas salen del hilo de interfaz mediante `QThread`.

El avance real de descarga y subida viaja junto al callback de log (`progress.py`): las funciones del pipeline conservan su firma y la interfaz recibe porcentaje, velocidad y tiempo restante.

### Blackboard y Zoom

`zoom2txt/blackboard/` consulta cursos y contenido con la sesión del WebEngine. El lanzamiento LTI de Zoom se valida antes de seguir redirecciones o descargar adjuntos. La selección distingue MP4 de audio independiente y, cuando existe más de una grabación de una clase, puede conservar la de mayor duración.

### Pipeline

`zoom2txt/core/pipeline.py` coordina el trabajo sin ocultar estados intermedios:

1. Verifica si ya existe un TXT íntegro.
2. Comprueba un ID de YouTube guardado o reconcilia una subida incierta.
3. Reutiliza un MP4 local cuyo hash coincide.
4. Descarga solo cuando no existe una copia válida.
5. Sube y guarda el ID antes de avanzar.
6. Espera subtítulos, consolida el texto y verifica el archivo.
7. Ejecuta la copia o limpieza solicitada únicamente después de confirmar el TXT.

### Persistencia

- `platformdirs` elige la carpeta de datos del usuario.
- SQLite guarda inventario, historial, estados, planificación, el índice del asistente y las conversaciones. Las migraciones de esquema son atómicas y dejan una copia previa.
- Los JSON con URLs o cookies se cifran ligados al usuario del sistema.
- La contraseña de Zoom reside en el keyring; no se serializa en configuración.
- Las escrituras importantes usan reemplazo atómico.

## Límites de confianza

| Límite | Medida |
|---|---|
| URL introducida por el usuario | HTTPS obligatorio, sin credenciales ni fragmentos |
| Lanzamiento LTI | Hosts esperados y redirecciones validadas |
| OAuth | Credencial de tipo `installed`, endpoints oficiales y token local |
| Descargas | Rutas confinadas, nombres seguros y comprobación de tamaño/hash |
| Recuperación | Una respuesta parcial nunca autoriza una copia nueva |
| Diagnósticos | Redacción de correos, tokens, cookies y parámetros sensibles |
| Respaldo | Excluye secretos y archivos académicos |

## Modo demo

`main.py --demo` crea un perfil temporal, carga tres cursos ficticios y reemplaza el procesamiento remoto por una simulación local. Las secciones que requieren cuentas están deshabilitadas. Al cerrar, el directorio temporal se elimina.

## Decisiones conscientes

- YouTube se utiliza por la calidad de sus subtítulos automáticos, no como almacenamiento permanente obligatorio.
- La reconciliación prefiere conservar un trabajo pendiente antes que arriesgar una subida duplicada.
- El TXT conserva las palabras producidas por YouTube; solo elimina estructura VTT y repeticiones técnicas.
- El límite diario de la app es una decisión operativa independiente de la cuota de Google.
