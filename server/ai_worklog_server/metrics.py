from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


CODE_EXTENSIONS = {
    ".c",
    ".cc",
    ".clj",
    ".cljs",
    ".cpp",
    ".cs",
    ".css",
    ".dart",
    ".ex",
    ".exs",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".lua",
    ".m",
    ".mm",
    ".php",
    ".pl",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".sh",
    ".sql",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
}
CODE_FILENAMES = {
    "Dockerfile",
    "Makefile",
    "Rakefile",
    "Gemfile",
    "go.mod",
    "go.sum",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "tsconfig.json",
}
POST_WRITE_HOOKS = {
    "posttooluse",
    "afterfileedit",
    "aftertabfileedit",
}
PATH_KEYS = {"path", "file_path", "filepath", "filename", "file"}
CONTENT_KEYS = {"content", "contents", "new_content", "newContent", "text"}


def is_code_path(path: str | None) -> bool:
    if not path:
        return False
    name = Path(path).name
    if name in CODE_FILENAMES:
        return True
    return Path(path).suffix.lower() in CODE_EXTENSIONS


def normalize_diff_path(raw: str) -> str | None:
    value = raw.strip()
    if not value or value == "/dev/null":
        return None
    if "\t" in value:
        value = value.split("\t", 1)[0]
    if " " in value:
        value = value.split(" ", 1)[0]
    if value.startswith("a/") or value.startswith("b/"):
        value = value[2:]
    return value or None


def empty_counts() -> dict[str, int]:
    return {"additions": 0, "deletions": 0, "files": 0, "events": 0}


def merge_file_counts(files: dict[str, dict[str, int]], path: str, additions: int, deletions: int) -> None:
    if not is_code_path(path):
        return
    item = files.setdefault(path, {"additions": 0, "deletions": 0})
    item["additions"] += additions
    item["deletions"] += deletions


def parse_patch_text(text: str) -> dict[str, dict[str, int]]:
    files: dict[str, dict[str, int]] = {}
    current_path: str | None = None
    in_hunk = False
    pending_old_path: str | None = None

    for line in text.splitlines():
        if line.startswith("*** Add File: ") or line.startswith("*** Update File: "):
            current_path = line.split(": ", 1)[1].strip()
            in_hunk = True
            continue
        if line.startswith("*** Delete File: "):
            current_path = line.split(": ", 1)[1].strip()
            in_hunk = True
            continue
        if line.startswith("diff --git "):
            parts = line.split()
            current_path = normalize_diff_path(parts[-1]) if parts else None
            in_hunk = False
            continue
        if line.startswith("--- "):
            pending_old_path = normalize_diff_path(line[4:])
            continue
        if line.startswith("+++ "):
            current_path = normalize_diff_path(line[4:]) or pending_old_path
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if line.startswith("*** End Patch"):
            current_path = None
            in_hunk = False
            continue
        if not current_path or not in_hunk:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            merge_file_counts(files, current_path, 1, 0)
        elif line.startswith("-"):
            merge_file_counts(files, current_path, 0, 1)

    return files


def count_lines(value: str) -> int:
    if not value:
        return 0
    return value.count("\n") + (0 if value.endswith("\n") else 1)


def iter_path_content_pairs(value: Any) -> list[tuple[str, str]]:
    if isinstance(value, dict):
        path = next((str(value[key]) for key in PATH_KEYS if isinstance(value.get(key), str)), None)
        content = next((value[key] for key in CONTENT_KEYS if isinstance(value.get(key), str)), None)
        pairs: list[tuple[str, str]] = []
        if path and isinstance(content, str):
            pairs.append((path, content))
        for child in value.values():
            pairs.extend(iter_path_content_pairs(child))
        return pairs
    if isinstance(value, list):
        pairs = []
        for child in value:
            pairs.extend(iter_path_content_pairs(child))
        return pairs
    return []


def iter_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        strings: list[str] = []
        for child in value.values():
            strings.extend(iter_strings(child))
        return strings
    if isinstance(value, list):
        strings = []
        for child in value:
            strings.extend(iter_strings(child))
        return strings
    return []


def looks_like_patch(text: str) -> bool:
    return (
        "*** Begin Patch" in text
        or "diff --git " in text
        or bool(re.search(r"(?m)^@@ .+ @@", text))
    )


def merge_files(target: dict[str, dict[str, int]], source: dict[str, dict[str, int]]) -> None:
    for path, counts in source.items():
        merge_file_counts(target, path, counts.get("additions", 0), counts.get("deletions", 0))


def copy_file_counts(source: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    return {
        path: {"additions": int(counts.get("additions") or 0), "deletions": int(counts.get("deletions") or 0)}
        for path, counts in source.items()
    }


def generated_files_from_event(record: dict[str, Any]) -> dict[str, dict[str, int]]:
    hook = str(record.get("hook_event_name") or "").lower()
    if hook not in POST_WRITE_HOOKS:
        return {}
    operation = record.get("operation")
    if isinstance(operation, dict) and operation.get("success") is False:
        return {}

    candidates: list[Any] = []
    content = record.get("content")
    if isinstance(content, dict):
        candidates.extend([content.get("tool_input"), content.get("tool_response")])
    raw = record.get("raw_hook_input")
    if raw is not None:
        candidates.append(raw)

    files: dict[str, dict[str, int]] = {}
    seen_patch_hashes: set[str] = set()
    seen_file_write_hashes: set[str] = set()
    for candidate in candidates:
        for path, file_content in iter_path_content_pairs(candidate):
            digest = hashlib.sha256(f"{path}\0{file_content}".encode("utf-8", errors="replace")).hexdigest()
            if digest in seen_file_write_hashes:
                continue
            seen_file_write_hashes.add(digest)
            merge_file_counts(files, path, count_lines(file_content), 0)
        for text in iter_strings(candidate):
            if not looks_like_patch(text):
                continue
            digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
            if digest in seen_patch_hashes:
                continue
            seen_patch_hashes.add(digest)
            merge_files(files, parse_patch_text(text))
    return files


def code_totals_from_files(files: dict[str, dict[str, int]]) -> dict[str, int]:
    return {
        "additions": sum(int(item.get("additions") or 0) for item in files.values()),
        "deletions": sum(int(item.get("deletions") or 0) for item in files.values()),
        "files": len(files),
    }


def workspace_code_files(record: dict[str, Any]) -> dict[str, dict[str, int]]:
    diff = record.get("workspace_diff")
    if not isinstance(diff, dict):
        return {}
    files: dict[str, dict[str, int]] = {}
    for item in diff.get("files") or []:
        if not isinstance(item, dict) or not item.get("is_code"):
            continue
        path = item.get("path")
        if not isinstance(path, str):
            continue
        merge_file_counts(files, path, int(item.get("additions") or 0), int(item.get("deletions") or 0))
    return files


def successful_git_commit(record: dict[str, Any]) -> bool:
    operation = record.get("operation")
    if isinstance(operation, dict) and operation.get("success") is False:
        return False

    tool = record.get("tool")
    command = tool.get("command") if isinstance(tool, dict) else None
    if not isinstance(command, str):
        content = record.get("content")
        tool_input = content.get("tool_input") if isinstance(content, dict) else None
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return False

    return bool(re.search(r"(^|&&|\|\||;)\s*git\s+commit(\s|$)", command))


def git_commit_summary(record: dict[str, Any]) -> dict[str, int] | None:
    candidates: list[Any] = []
    content = record.get("content")
    if isinstance(content, dict):
        candidates.append(content.get("tool_response"))
    raw = record.get("raw_hook_input")
    if isinstance(raw, dict):
        candidates.append(raw.get("tool_response"))

    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        for line in candidate.splitlines():
            if " changed" not in line:
                continue
            files_match = re.search(r"(?P<files>\d+)\s+files?\s+changed", line)
            if not files_match:
                continue
            insertions_match = re.search(r"(?P<insertions>\d+)\s+insertions?\(\+\)", line)
            deletions_match = re.search(r"(?P<deletions>\d+)\s+deletions?\(-\)", line)
            return {
                "additions": int(insertions_match.group("insertions")) if insertions_match else 0,
                "deletions": int(deletions_match.group("deletions")) if deletions_match else 0,
                "files": int(files_match.group("files")),
            }
    return None


def event_sort_key(record: dict[str, Any]) -> str:
    return str(record.get("received_at") or record.get("client_received_at") or record.get("_server_ingested_at") or "")


def compute_code_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_session: dict[str, dict[str, Any]] = {}
    generated_events = 0
    latest_adopted_event: dict[str, dict[str, Any]] = {}

    for record in sorted(records, key=event_sort_key):
        if record.get("record_type") != "event":
            continue
        session_id = str(record.get("session_id") or "unknown")
        session = by_session.setdefault(
            session_id,
            {
                "generated": empty_counts(),
                "adopted": empty_counts(),
                "generated_files": {},
                "adopted_files": {},
                "uncommitted_files": {},
                "committed_generated_files": {},
                "committed_summary": empty_counts(),
                "latest_git_commit_code": empty_counts(),
                "pending_generated_files": {},
                "generated_events": 0,
                "uncommitted_events": 0,
                "pending_generated_events": 0,
                "git_commit_events": 0,
            },
        )

        generated_files = generated_files_from_event(record)
        if generated_files:
            generated_events += 1
            session["generated_events"] += 1
            session["uncommitted_events"] += 1
            session["pending_generated_events"] += 1
            merge_files(session["generated_files"], generated_files)
            merge_files(session["pending_generated_files"], generated_files)

        has_workspace_diff = isinstance(record.get("workspace_diff"), dict)
        workspace_files = workspace_code_files(record)
        if has_workspace_diff:
            current = latest_adopted_event.get(session_id)
            if current is None or event_sort_key(record) >= event_sort_key(current):
                latest_adopted_event[session_id] = record
                session["uncommitted_files"] = workspace_files
                session["latest_workspace_diff_event_id"] = record.get("event_id")
                session["latest_workspace_diff_received_at"] = record.get("received_at")
                if workspace_files:
                    session["adopted_files"] = workspace_files
                    session["adoption_source"] = "workspace_diff"

        if successful_git_commit(record):
            session["git_commit_events"] += 1
            summary = git_commit_summary(record)
            if summary:
                session["committed_summary"]["additions"] += summary["additions"]
                session["committed_summary"]["deletions"] += summary["deletions"]
                session["committed_summary"]["files"] += summary["files"]
                session["committed_summary"]["events"] += 1
            session["committed_generated_files"] = copy_file_counts(session["generated_files"])
            session["pending_generated_files"] = {}
            session["pending_generated_events"] = 0
            current = session.get("latest_git_commit_event")
            if current is None or event_sort_key(record) >= event_sort_key(current):
                session["latest_git_commit_event"] = record
                if summary:
                    session["latest_git_commit_code"] = {**summary, "events": 1}
                else:
                    session["latest_git_commit_code"] = {**code_totals_from_files(session["generated_files"]), "events": 1}

    total_generated_files: dict[str, dict[str, int]] = {}
    total_uncommitted_files_by_session: list[dict[str, dict[str, int]]] = []
    total_adopted_counts_by_session: list[dict[str, int]] = []
    for session in by_session.values():
        if not session["adopted_files"] and session["git_commit_events"] and session["committed_summary"]["events"]:
            commit_event = session.get("latest_git_commit_event") or {}
            session["latest_git_commit_event_id"] = commit_event.get("event_id")
            session["latest_git_commit_received_at"] = commit_event.get("received_at")
            session["adoption_source"] = "git_commit_summary"
        elif not session["adopted_files"] and session["git_commit_events"] and session["committed_generated_files"]:
            session["adopted_files"] = session["committed_generated_files"]
            commit_event = session.get("latest_git_commit_event") or {}
            session["latest_git_commit_event_id"] = commit_event.get("event_id")
            session["latest_git_commit_received_at"] = commit_event.get("received_at")
            session["adoption_source"] = "git_commit_generated_code"

        if not session["uncommitted_files"] and session["pending_generated_files"]:
            session["uncommitted_files"] = session["pending_generated_files"]

        session["generated"] = {**code_totals_from_files(session["generated_files"]), "events": session["generated_events"]}
        if session["committed_summary"]["events"] and session.get("adoption_source") == "git_commit_summary":
            session["adopted"] = dict(session["committed_summary"])
        else:
            session["adopted"] = {**code_totals_from_files(session["adopted_files"]), "events": int(session["git_commit_events"])}
        uncommitted_events = 1 if session.get("latest_workspace_diff_event_id") and session["uncommitted_files"] else session["pending_generated_events"]
        session["uncommitted"] = {
            **code_totals_from_files(session["uncommitted_files"]),
            "events": int(uncommitted_events),
        }
        merge_files(total_generated_files, session["generated_files"])
        if session["committed_summary"]["events"] and session.get("adoption_source") == "git_commit_summary":
            total_adopted_counts_by_session.append(session["committed_summary"])
        elif session["adopted_files"]:
            total_adopted_counts_by_session.append(code_totals_from_files(session["adopted_files"]))
        if session["uncommitted_files"]:
            total_uncommitted_files_by_session.append(session["uncommitted_files"])

    adopted_additions = 0
    adopted_deletions = 0
    adopted_files = 0
    for totals in total_adopted_counts_by_session:
        adopted_additions += totals["additions"]
        adopted_deletions += totals["deletions"]
        adopted_files += totals["files"]

    uncommitted_additions = 0
    uncommitted_deletions = 0
    uncommitted_files = 0
    for files in total_uncommitted_files_by_session:
        totals = code_totals_from_files(files)
        uncommitted_additions += totals["additions"]
        uncommitted_deletions += totals["deletions"]
        uncommitted_files += totals["files"]

    generated_totals = code_totals_from_files(total_generated_files)
    return {
        "definitions": {
            "generated_code": "weak: code additions/deletions parsed from successful post-write hook payloads",
            "adopted_code": "medium: generated code that is either still present in the latest session-end workspace diff or was followed by a successful git commit in the same session",
            "uncommitted_code": "medium: code still visible in the latest session-end workspace diff, or generated after the latest successful git commit when no workspace diff is available",
        },
        "generated_code": {
            **generated_totals,
            "events": generated_events,
        },
        "adopted_code": {
            "additions": adopted_additions,
            "deletions": adopted_deletions,
            "files": adopted_files,
            "sessions": len(total_adopted_counts_by_session),
        },
        "uncommitted_code": {
            "additions": uncommitted_additions,
            "deletions": uncommitted_deletions,
            "files": uncommitted_files,
            "sessions": len(total_uncommitted_files_by_session),
        },
        "by_session": {
            session_id: {
                key: value
                for key, value in session.items()
                if key
                not in {
                    "generated_files",
                    "adopted_files",
                    "uncommitted_files",
                    "committed_generated_files",
                    "committed_summary",
                    "pending_generated_files",
                    "generated_events",
                    "uncommitted_events",
                    "pending_generated_events",
                    "latest_git_commit_event",
                }
            }
            for session_id, session in sorted(by_session.items())
        },
        "notes": [
            "Only code-like file paths are counted.",
            "Assistant response code blocks are not counted as generated code unless they appear in a write/patch payload.",
            "Git commit adoption uses git commit summary output when available; without that summary it falls back to generated code in the same session.",
            "Adopted code is session cumulative. latest_git_commit_code shows only the latest observed commit summary.",
            "Uncommitted fallback counts generated post-write payloads after the latest successful git commit when no workspace diff is present.",
        ],
    }
