"""Platform-specific hook command generation and runtime checks."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys

import platform_io


def shell_quote(value: str) -> str:
    if not value:
        return "''"
    safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+-=.,/:@%"
    if all(ch in safe for ch in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def cmd_file_literal(value: str) -> str:
    return value.replace("%", "%%")


def windows_config_literal(path: Path) -> str:
    expanded = path.expanduser()
    try:
        home = Path.home().resolve()
        relative = expanded.resolve().relative_to(home)
    except (NotImplementedError, OSError, RuntimeError, ValueError):
        return cmd_file_literal(str(expanded))
    parts = "\\".join(relative.parts)
    return "%USERPROFILE%" + (f"\\{parts}" if parts else "")


def windows_python_launcher_lines(python: str, script_args: str) -> str:
    lines = []
    lines.append('if defined AI_WORKLOG_PYTHON if exist "%AI_WORKLOG_PYTHON%" (\r\n')
    lines.append(f'  "%AI_WORKLOG_PYTHON%" {script_args}\r\n')
    lines.append("  if not errorlevel 1 exit /b 0\r\n")
    lines.append(")\r\n")
    if python.isascii():
        lines.append(f'if exist "{cmd_file_literal(python)}" (\r\n')
        lines.append(f'  "{cmd_file_literal(python)}" {script_args}\r\n')
        lines.append("  if not errorlevel 1 exit /b 0\r\n")
        lines.append(")\r\n")
    lines.append("where py >nul 2>nul\r\n")
    lines.append("if not errorlevel 1 (\r\n")
    lines.append(f"  py -3 {script_args}\r\n")
    lines.append("  if not errorlevel 1 exit /b 0\r\n")
    lines.append(")\r\n")
    lines.append("where python >nul 2>nul\r\n")
    lines.append("if not errorlevel 1 (\r\n")
    lines.append(f"  python {script_args}\r\n")
    lines.append("  if not errorlevel 1 exit /b 0\r\n")
    lines.append(")\r\n")
    lines.append('set "AI_WORKLOG_ERROR_DIR=%USERPROFILE%\\.ai-worklog\\errors"\r\n')
    lines.append('if not exist "%AI_WORKLOG_ERROR_DIR%" mkdir "%AI_WORKLOG_ERROR_DIR%" >nul 2>nul\r\n')
    lines.append(
        '>> "%AI_WORKLOG_ERROR_DIR%\\runtime.log" echo [%date% %time%] '
        "No Python runtime found for ai-worklog Cursor hook. Set AI_WORKLOG_PYTHON or install Python.\r\n"
    )
    lines.append("exit /b 0\r\n")
    return "".join(lines)


@dataclass(frozen=True)
class PythonRuntime:
    ok: bool
    command: str | None
    source: str | None
    message: str


@dataclass(frozen=True)
class PlatformSpec:
    name: str
    is_windows: bool

    def detect_python_runtime(self) -> PythonRuntime:
        if self.is_windows:
            explicit = os.environ.get("AI_WORKLOG_PYTHON")
            if explicit and Path(explicit).expanduser().exists():
                return PythonRuntime(True, explicit, "AI_WORKLOG_PYTHON", "Python runtime found via AI_WORKLOG_PYTHON.")
            if sys.executable and Path(sys.executable).exists():
                return PythonRuntime(True, sys.executable, "current", "Python runtime found from installer process.")
            if shutil.which("py"):
                return PythonRuntime(True, "py -3", "py", "Python runtime found via Windows py launcher.")
            python = shutil.which("python")
            if python:
                return PythonRuntime(True, python, "python", "Python runtime found on PATH.")
            return PythonRuntime(
                False,
                None,
                None,
                "No Python runtime found. Install Python or set AI_WORKLOG_PYTHON to python.exe.",
            )
        return PythonRuntime(True, sys.executable or "python3", "current", "Python runtime found from current process.")

    def write_windows_hook_launcher(self, skill_dir: Path, surface: str, config_path: Path, skill_name: str) -> Path:
        launcher = skill_dir / "scripts" / f"{skill_name}-hook-{surface}.cmd"
        python = sys.executable or "python"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        script_args = (
            '"%AI_WORKLOG_SCRIPT%" '
            f'--surface "{cmd_file_literal(surface)}" '
            '--config "%AI_WORKLOG_CONFIG%" '
            f'--source-id "{cmd_file_literal(skill_name)}"'
        )
        content = (
            "@echo off\r\n"
            "setlocal\r\n"
            "set \"PYTHONUTF8=1\"\r\n"
            "set \"PYTHONIOENCODING=utf-8\"\r\n"
            "set \"AI_WORKLOG_SCRIPT=%~dp0journal.py\"\r\n"
            f"set \"AI_WORKLOG_CONFIG={windows_config_literal(config_path)}\"\r\n"
            "if not exist \"%AI_WORKLOG_SCRIPT%\" exit /b 0\r\n"
            f"{windows_python_launcher_lines(str(python), script_args)}"
        )
        platform_io.write_text(launcher, content, newline="")
        return launcher

    def hook_command(self, skill_dir: Path, surface: str, config_path: Path, skill_name: str) -> str:
        if self.is_windows:
            launcher = self.write_windows_hook_launcher(skill_dir, surface, config_path, skill_name)
            return subprocess.list2cmdline([str(launcher)])

        journal = skill_dir / "scripts" / "journal.py"
        python = sys.executable or "python3"
        return (
            "/bin/sh -c "
            + shell_quote('test -f "$1" || exit 0; exec "$2" "$1" --surface "$3" --config "$4" --source-id "$5"')
            + f" {shell_quote(skill_name + '-hook')}"
            + f" {shell_quote(str(journal))}"
            + f" {shell_quote(python)}"
            + f" {shell_quote(surface)}"
            + f" {shell_quote(str(config_path))}"
            + f" {shell_quote(skill_name)}"
        )

    def repair_skill_acl(self, path: Path) -> tuple[bool, str]:
        if not self.is_windows:
            return True, "not required"
        completed = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:e",
                "/grant:r",
                "*S-1-5-11:(OI)(CI)RX",
                "/T",
                "/C",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return completed.returncode == 0, completed.stdout.strip()

    def skill_acl_is_readable(self, path: Path) -> tuple[bool, str]:
        if not self.is_windows:
            return True, "not required"
        completed = subprocess.run(
            ["icacls", str(path)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = completed.stdout or ""
        if completed.returncode != 0:
            return False, output.strip()
        markers = (
            "Authenticated Users",
            "CodexSandboxUsers",
            f"\\{os.environ.get('USERNAME', '')}:",
            "*S-1-5-11",
        )
        return any(marker and marker in output for marker in markers), output.strip()


def current_platform() -> PlatformSpec:
    return PlatformSpec(name="windows" if os.name == "nt" else "posix", is_windows=os.name == "nt")
