"""Conversión fiel de subtítulos WebVTT de YouTube a texto legible.

YouTube publica subtítulos automáticos como una secuencia progresiva: cada
cue suele repetir la línea anterior y añadir la siguiente. Copiar ese VTT a
un archivo ``.txt`` multiplica artificialmente el contenido. Este módulo
reproduce el texto consolidado que entrega YouTube Studio sin corregir,
resumir ni reescribir las palabras reconocidas.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from pathlib import Path

from zoom2txt.storage import atomic_write

Log = Callable[[str], None]

_TIMESTAMP = re.compile(r"^\s*(?:\d{2}:)?\d{2}:\d{2}\.\d{3}\s+-->\s+")
_VTT_TAG = re.compile(r"<[^>]*>")


def vtt_a_texto(data: bytes | str) -> str:
    """Devuelve el texto visible conservando exactamente sus palabras.

    Los cues vacíos delimitan los mismos párrafos que YouTube Studio. De
    cada cue progresivo se emite solamente su línea activa (la última). Una
    línea activa repetida sí se conserva cuando sigue a una pausa o aparece
    como segunda línea; así se distingue el habla real —por ejemplo,
    ``siguiente, siguiente``— de los duplicados técnicos de transición.
    """

    if isinstance(data, bytes):
        contenido = data.decode("utf-8-sig")
    else:
        contenido = data.lstrip("\ufeff")

    cues: list[list[str]] = []
    actual: list[str] | None = None
    for linea in contenido.splitlines():
        if _TIMESTAMP.match(linea):
            if actual is not None:
                cues.append(actual)
            actual = []
        elif actual is not None:
            actual.append(linea)
    if actual is not None:
        cues.append(actual)

    fragmentos: list[str] = []
    parrafos: list[str] = []
    parrafo_actual: list[str] = []
    despues_de_pausa = True

    for cue in cues:
        lineas_visibles = [html.unescape(_VTT_TAG.sub("", linea)).strip() for linea in cue]
        lineas_visibles = [linea for linea in lineas_visibles if linea]

        if not lineas_visibles:
            if parrafo_actual:
                parrafos.append(" ".join(parrafo_actual))
                parrafo_actual = []
            despues_de_pausa = True
            continue

        activa = lineas_visibles[-1]
        es_texto_nuevo = (
            not fragmentos
            or activa != fragmentos[-1]
            or len(lineas_visibles) >= 2
            or despues_de_pausa
        )
        if es_texto_nuevo:
            fragmentos.append(activa)
            parrafo_actual.append(activa)
        despues_de_pausa = False

    if parrafo_actual:
        parrafos.append(" ".join(parrafo_actual))

    resultado = "\n\n".join(parrafos).strip()
    if not resultado:
        raise ValueError("El WebVTT no contiene texto visible.")
    return resultado


def convertir_vtt_guardado(txt_file: Path) -> bool:
    """Migra atómicamente un TXT antiguo que todavía contiene WebVTT."""

    txt_file = Path(txt_file)
    try:
        data = txt_file.read_bytes()
    except OSError:
        return False
    if not data.lstrip(b"\xef\xbb\xbf\r\n\t ").startswith(b"WEBVTT"):
        return False
    atomic_write(txt_file, vtt_a_texto(data))
    return True


def migrar_transcripciones_anteriores(carpeta: Path, *, recursive: bool = True) -> list[Path]:
    """Convierte solo salidas de Zoom2TXT que aún contienen WebVTT crudo."""

    carpeta = Path(carpeta)
    if not carpeta.is_dir():
        return []
    convertidas: list[Path] = []
    archivos = (
        carpeta.rglob("*_Para_NotebookLM.txt")
        if recursive
        else carpeta.glob("*_Para_NotebookLM.txt")
    )
    for archivo in archivos:
        try:
            if convertir_vtt_guardado(archivo):
                convertidas.append(archivo)
        except (OSError, UnicodeError, ValueError):
            # Nunca impedir el inicio de la app por una salida antigua dañada.
            # El archivo queda intacto gracias a atomic_write y puede revisarse.
            continue
    return convertidas


def limpiar_vtt_a_txt(vtt_file: Path, txt_file: Path, log: Log = print) -> Path:
    """Punto compatible para llamadas que todavía proporcionan un VTT."""

    log("[*] Consolidando subtítulos para NotebookLM…")
    atomic_write(txt_file, vtt_a_texto(Path(vtt_file).read_bytes()))
    log(f"[+] Archivo de texto listo: {txt_file}")
    Path(vtt_file).unlink(missing_ok=True)
    return Path(txt_file)
