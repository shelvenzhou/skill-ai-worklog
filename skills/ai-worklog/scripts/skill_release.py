"""Release metadata for the AI Worklog skill."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_NAME = "ai-worklog"
DEFAULT_VERSION = "0.3.0"
DEFAULT_EVENT_SCHEMA_VERSION = "0.3"


def skill_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def manifest_path() -> Path:
    return skill_dir() / "skill-version.json"


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    target = path or manifest_path()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


MANIFEST = load_manifest()
NAME = str(MANIFEST.get("name") or DEFAULT_NAME)
VERSION = str(MANIFEST.get("version") or DEFAULT_VERSION)
EVENT_SCHEMA_VERSION = str(MANIFEST.get("event_schema_version") or DEFAULT_EVENT_SCHEMA_VERSION)
