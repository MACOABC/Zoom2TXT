"""Fail closed when a public snapshot contains private or generated artifacts."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

BLOCKED_NAMES = {
    ".env",
    "bb_cookies.json",
    "client_secrets.json",
    "processed.json",
    "settings.json",
    "state.sqlite3",
    "token.json",
    "yt_cookies.txt",
}
BLOCKED_SUFFIXES = {
    ".db",
    ".m4a",
    ".mp3",
    ".mp4",
    ".sqlite",
    ".sqlite3",
    ".srt",
    ".vtt",
    ".wav",
    ".webm",
    ".zip",
}
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".py",
    ".spec",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
PATTERNS = {
    "ruta absoluta de perfil Windows": re.compile(
        r"(?i)\b[A-Z]:\\Users\\(?!example(?:\\|\b)|user(?:\\|\b))[^\\\s]+"
    ),
    "credencial incrustada en URL": re.compile(
        r"https?://[^\s/:]+:[^\s/@]+@(?![^/\s]+\.invalid(?:[/:]|$))"
    ),
    "token de Telegram": re.compile(r"\bbot\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    "token de GitHub": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "clave privada": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "ID OAuth de Google": re.compile(
        r"\b\d{8,}-[A-Za-z0-9_-]{20,}\.apps\.googleusercontent\.com\b"
    ),
    "URL concreta de Blackboard": re.compile(r"https://[A-Za-z0-9.-]+\.blackboard\.com", re.I),
    "correo no ficticio": re.compile(
        r"\b[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|invalid)\b|universidad\.edu\b)"
        r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        re.I,
    ),
}


def repository_files(root: Path) -> list[Path]:
    try:
        git = shutil.which("git")
        if not git:
            raise OSError("git is not installed")
        result = subprocess.run(
            [git, "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        names = [name for name in result.stdout.decode().split("\0") if name]
        return [root / name for name in names]
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
        return [path for path in root.rglob("*") if path.is_file() and ".git" not in path.parts]


def audit(root: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path in repository_files(root):
        relative = path.relative_to(root).as_posix()
        lower = path.name.lower()
        if lower in BLOCKED_NAMES or path.suffix.lower() in BLOCKED_SUFFIXES:
            findings.append({"path": relative, "category": "artefacto privado o generado"})
            continue
        if lower.startswith(("transcript", "transcripcion")) and path.suffix.lower() == ".txt":
            findings.append({"path": relative, "category": "posible transcripción"})
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES or relative == "scripts/privacy_audit.py":
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for category, pattern in PATTERNS.items():
            candidate = content
            if category == "correo no ficticio":
                candidate = re.sub(r"https?://[^\s\"'<>]+", "", candidate)
            if pattern.search(candidate):
                findings.append({"path": relative, "category": category})
    return sorted(findings, key=lambda item: (item["path"], item["category"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    findings = audit(args.root.resolve())
    if args.json:
        print(json.dumps({"passed": not findings, "findings": findings}, indent=2))
    elif findings:
        print("Auditoría de privacidad: FALLÓ")
        for finding in findings:
            print(f"- {finding['path']}: {finding['category']}")
    else:
        print("Auditoría de privacidad: OK — no se detectaron artefactos prohibidos.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
