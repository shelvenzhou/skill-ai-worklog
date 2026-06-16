from __future__ import annotations

import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "ai-worklog" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def load_script(name: str):
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    assert match is not None
    return match.group(1)


class VersioningTests(unittest.TestCase):
    def test_skill_manifest_is_single_source_for_runtime_versions(self) -> None:
        manifest = json.loads((ROOT / "skills" / "ai-worklog" / "skill-version.json").read_text(encoding="utf-8"))
        skill_release = load_script("skill_release")
        journal = load_script("journal")
        installer = load_script("install")

        self.assertEqual(skill_release.VERSION, manifest["version"])
        self.assertEqual(journal.VERSION, manifest["version"])
        self.assertEqual(installer.SKILL_VERSION, manifest["version"])
        self.assertEqual(journal.EVENT_SCHEMA_VERSION, manifest["event_schema_version"])

    def test_project_package_version_matches_skill_release(self) -> None:
        manifest = json.loads((ROOT / "skills" / "ai-worklog" / "skill-version.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["package_version"], pyproject_version())
        self.assertTrue(str(manifest["release_tag"]).endswith(f"v{manifest['version']}"))


if __name__ == "__main__":
    unittest.main()
