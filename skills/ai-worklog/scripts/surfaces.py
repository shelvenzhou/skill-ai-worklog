"""Codex/Cursor surface-specific install metadata."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


SKILL_NAME = "ai-worklog"

CODEX_MINIMAL_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "SubagentStop",
    "Stop",
]
CODEX_FULL_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "SubagentStop",
    "Stop",
]
CURSOR_MINIMAL_EVENTS = [
    "sessionStart",
    "beforeSubmitPrompt",
    "postToolUse",
    "postToolUseFailure",
    "afterAgentResponse",
    "subagentStop",
    "stop",
]
CURSOR_FULL_EVENTS = [
    "workspaceOpen",
    "sessionStart",
    "sessionEnd",
    "beforeSubmitPrompt",
    "preToolUse",
    "postToolUse",
    "postToolUseFailure",
    "subagentStart",
    "subagentStop",
    "beforeShellExecution",
    "afterShellExecution",
    "beforeMCPExecution",
    "afterMCPExecution",
    "beforeReadFile",
    "afterFileEdit",
    "beforeTabFileRead",
    "afterTabFileEdit",
    "afterAgentResponse",
    "afterAgentThought",
    "preCompact",
    "stop",
]


@dataclass(frozen=True)
class SurfaceSpec:
    name: str
    env_home: str
    default_home_name: str
    hook_schema_versioned: bool
    minimal_events: tuple[str, ...]
    full_events: tuple[str, ...]
    enable_codex_hooks_feature: bool = False

    @property
    def home(self) -> Path:
        return Path(os.environ.get(self.env_home) or Path.home() / self.default_home_name).expanduser()

    @property
    def skill_dir(self) -> Path:
        return self.home / "skills" / SKILL_NAME

    @property
    def hooks_path(self) -> Path:
        return self.home / "hooks.json"

    @property
    def config_toml_path(self) -> Path | None:
        if self.enable_codex_hooks_feature:
            return self.home / "config.toml"
        return None

    def events(self, hook_set: str) -> list[str]:
        return list(self.full_events if hook_set == "full" else self.minimal_events)

    @property
    def smoke_event(self) -> str:
        return "UserPromptSubmit" if self.name == "codex" else "beforeSubmitPrompt"


CODEX = SurfaceSpec(
    name="codex",
    env_home="CODEX_HOME",
    default_home_name=".codex",
    hook_schema_versioned=False,
    minimal_events=tuple(CODEX_MINIMAL_EVENTS),
    full_events=tuple(CODEX_FULL_EVENTS),
    enable_codex_hooks_feature=True,
)

CURSOR = SurfaceSpec(
    name="cursor",
    env_home="CURSOR_HOME",
    default_home_name=".cursor",
    hook_schema_versioned=True,
    minimal_events=tuple(CURSOR_MINIMAL_EVENTS),
    full_events=tuple(CURSOR_FULL_EVENTS),
)


def surface_specs(selection: str) -> list[SurfaceSpec]:
    if selection == "both":
        return [CODEX, CURSOR]
    if selection == "codex":
        return [CODEX]
    if selection == "cursor":
        return [CURSOR]
    raise ValueError(f"unknown surface: {selection}")


def get_surface(name: str) -> SurfaceSpec:
    matches = surface_specs(name)
    if len(matches) != 1:
        raise ValueError(f"expected a single surface, got {name}")
    return matches[0]
