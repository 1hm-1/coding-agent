"""Reproducible M2 direct-tree read-only manifest gate.

Usage:
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 examples/m2_direct_tree_manifest.py PATH
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile

from coding_agent.persistence import DirectTreeReadOnlyViolation, SQLiteRunJournal
from coding_agent.product_workspace import (
    ReadOnlyDirectProductComposition,
    capture_direct_tree_manifest,
    discover_direct_workspace,
)
from coding_agent.test_profiles import default_test_profiles
from coding_agent.tools.builtin import build_builtin_registry
from coding_agent.tools.harness import ToolHarness


def _first_readable_file(root: Path) -> str | None:
    for path in sorted(root.rglob("*")):
        if path.is_file() and ".git" not in path.relative_to(root).parts and not path.is_symlink():
            return path.relative_to(root).as_posix()
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    arguments = parser.parse_args()
    root = arguments.path.resolve(strict=True)
    before = capture_direct_tree_manifest(root)
    with tempfile.TemporaryDirectory(prefix="coding-agent-m2-manifest-") as temporary:
        with SQLiteRunJournal(Path(temporary) / "state.db") as journal:
            binding = discover_direct_workspace(journal, root)
            composition = ReadOnlyDirectProductComposition(
                binding,
                harness=ToolHarness(build_builtin_registry(default_test_profiles())),
            )
            readable = _first_readable_file(root)
            if readable is not None:
                result = composition.read(
                    call_id="m2-manifest-read", tool_name="read_file",
                    arguments={"path": readable},
                )
                if not result.ok:
                    raise RuntimeError(f"bounded read failed: {result.error}")
            for action in (
                "edit", "create", "delete", "command", "test", "build", "cache",
                "git-index", "git-ref", "git-lock", "hook", "helper",
                "startup-artifact", "indirect-child", "undo",
            ):
                try:
                    composition.reject_side_effect(action)
                except DirectTreeReadOnlyViolation:
                    continue
                raise AssertionError(f"side effect was not rejected: {action}")
    after = capture_direct_tree_manifest(root)
    if before != after:
        raise AssertionError("direct-tree manifest changed")
    encoded = json.dumps(before, ensure_ascii=False, sort_keys=True).encode("utf-8")
    print(json.dumps({
        "status": "unchanged",
        "entries": len(before["entries"]),
        "git_paths": len(before["git_paths"]),
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
