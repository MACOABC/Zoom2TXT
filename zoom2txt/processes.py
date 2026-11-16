"""Bounded child processes with cooperative cancellation."""

import subprocess
import threading
import time

from zoom2txt.security import redact


def run_process(args, cancel=None, timeout=3600, on_line=None):
    """Ejecuta un proceso hijo con cancelación cooperativa y límite de tiempo.

    Con ``on_line`` la salida estándar se entrega línea a línea mientras el
    proceso corre (avance de descargas); sin él se comporta como siempre."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    started = time.monotonic()
    with subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags
    ) as proc:
        try:
            if on_line is not None:
                return _run_streaming(args, proc, cancel, timeout, started, on_line)
            while True:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError(
                        "Operación cancelada. Puedes retomarla desde el historial."
                    )
                if time.monotonic() - started > timeout:
                    raise TimeoutError(
                        "La operación tardó demasiado. No se descartó el resultado anterior."
                    )
                try:
                    out, err = proc.communicate(timeout=0.3)
                    return subprocess.CompletedProcess(
                        args,
                        proc.returncode,
                        out,
                        redact(err.decode("utf-8", "replace")[-4000:]),
                    )
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            raise


def _run_streaming(args, proc, cancel, timeout, started, on_line):
    tail: list[bytes] = []
    errors: list[bytes] = []

    def pump_stdout():
        for raw in iter(proc.stdout.readline, b""):
            tail.append(raw)
            if len(tail) > 400:
                del tail[:200]
            try:
                on_line(raw.decode("utf-8", "replace").rstrip("\r\n"))
            except Exception:  # noqa: BLE001, S110 — el avance nunca detiene la descarga
                pass

    def pump_stderr():
        errors.append(proc.stderr.read() or b"")

    readers = [
        threading.Thread(target=pump_stdout, daemon=True),
        threading.Thread(target=pump_stderr, daemon=True),
    ]
    for reader in readers:
        reader.start()
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Operación cancelada. Puedes retomarla desde el historial.")
        if time.monotonic() - started > timeout:
            raise TimeoutError(
                "La operación tardó demasiado. No se descartó el resultado anterior."
            )
        time.sleep(0.2)
    for reader in readers:
        reader.join(timeout=5)
    return subprocess.CompletedProcess(
        args,
        proc.returncode,
        b"".join(tail),
        redact(b"".join(errors).decode("utf-8", "replace")[-4000:]),
    )
