# Contribuir

## Preparación

1. Crea un entorno virtual con Python 3.12.
2. Instala `requirements-dev.txt`.
3. Trabaja siempre con el modo demo o fixtures sintéticos.

## Antes de enviar un cambio

```powershell
python scripts/privacy_audit.py
python -m ruff check .
python -m unittest discover -s tests -q
```

No adjuntes logs sin revisar, capturas de cuentas reales, videos, transcripciones, cookies, tokens ni archivos OAuth. Un cambio de integración debe incluir una prueba que bloquee tráfico real y reproduzca el caso con respuestas simuladas.

Los pull requests deben explicar el problema, la decisión técnica, cómo se verificó y qué limitación permanece.
