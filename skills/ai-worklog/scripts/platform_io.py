"""Cross-platform text and process I/O helpers for hook scripts."""

from __future__ import annotations

from dataclasses import dataclass
import locale
import os
from pathlib import Path
import subprocess
import sys
from typing import Mapping, Sequence


UTF8 = "utf-8"
UTF8_SIG = "utf-8-sig"


def _dedupe(values: list[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value:
            continue
        key = value.lower().replace("_", "-")
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def text_decode_encodings() -> list[str]:
    encodings = [
        UTF8_SIG,
        UTF8,
        locale.getpreferredencoding(False),
        sys.getfilesystemencoding(),
    ]
    if os.name == "nt":
        encodings.extend(["mbcs", "cp65001", "gb18030"])
    return _dedupe(encodings)


def decode_text(data: bytes, *, errors: str = "replace") -> str:
    for encoding in text_decode_encodings():
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode(UTF8, errors=errors)


def read_text(path: Path, *, encoding: str | None = UTF8, errors: str = "strict") -> str:
    if encoding is None:
        return decode_text(path.read_bytes(), errors=errors)
    return path.read_text(encoding=encoding, errors=errors)


def write_text(path: Path, text: str, *, encoding: str = UTF8, newline: str = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=encoding, newline=newline) as fh:
        fh.write(text)


def configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding=UTF8, errors="replace")
            except Exception:
                pass


def utf8_subprocess_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    child_env = dict(os.environ if env is None else env)
    child_env.setdefault("PYTHONUTF8", "1")
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    return child_env


@dataclass(frozen=True)
class TextCompletedProcess:
    args: Sequence[str]
    returncode: int
    stdout: str


def run_capture_text(
    args: Sequence[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
) -> TextCompletedProcess:
    completed = subprocess.run(
        list(args),
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )
    return TextCompletedProcess(
        args=args,
        returncode=int(completed.returncode),
        stdout=decode_text(completed.stdout or b""),
    )
