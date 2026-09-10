"""Bounded child processes with cooperative cancellation."""

import subprocess
import time

from zoom2txt.security import redact


def run_process(args, cancel=None, timeout=3600):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    started = time.monotonic()
    with subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags
    ) as proc:
        try:
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
