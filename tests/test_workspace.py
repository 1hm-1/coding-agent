from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from coding_agent.domain import WorkspaceViolation
from coding_agent.workspace import WorkspaceManager, tree_fingerprint


class WorkspaceTest(unittest.TestCase):
    def test_copy_is_independent_and_guard_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "module.py").write_text("value = 1\n", encoding="utf-8")
            before = tree_fingerprint(source)
            manager = WorkspaceManager(root / "agent-home")
            manifest = manager.create(source, "session-1")
            guard = manager.get("session-1")

            guard.resolve("module.py").write_text("value = 2\n", encoding="utf-8")
            self.assertEqual(tree_fingerprint(source), before)
            self.assertEqual((source / "module.py").read_text(encoding="utf-8"), "value = 1\n")
            self.assertTrue(Path(manifest.workspace_path, ".git").is_dir())
            with self.assertRaises(WorkspaceViolation):
                guard.resolve("../source/module.py")
            with self.assertRaises(WorkspaceViolation):
                guard.resolve(str(source / "module.py"))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are required")
    def test_guard_rejects_external_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "file.txt").write_text("safe", encoding="utf-8")
            manager = WorkspaceManager(root / "agent-home")
            manager.create(source, "session-2")
            guard = manager.get("session-2")
            outside = root / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            (guard.root / "escape.txt").symlink_to(outside)

            with self.assertRaises(WorkspaceViolation):
                guard.resolve("escape.txt")


if __name__ == "__main__":
    unittest.main()

