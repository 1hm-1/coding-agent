from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from coding_agent.product_instructions import (
    DiscoveredInstruction,
    InstructionDiscovery,
    classify_instruction_source_mutation,
    discover_instruction_sources,
    resolve_instruction_manifest,
    sha256_bytes,
)


class M3InstructionTests(unittest.TestCase):
    def test_bounded_exact_name_discovery_trust_and_path_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "project" / "src"
            nested.mkdir(parents=True)
            (root / "AGENTS.md").write_text("rule.style: broad\n", encoding="utf-8")
            (root / "project" / "AGENTS.md").write_text(
                "rule.style: project\n", encoding="utf-8",
            )
            (nested / "AGENTS.txt").write_text("ignored", encoding="utf-8")
            discovered = discover_instruction_sources(
                repository_root=root, project_scope_root=root / "project",
                workspace_root=root / "project", target_paths=("src/value.py",),
            )
            self.assertEqual([Path(source.locator).name for source in discovered.sources],
                             ["AGENTS.md", "AGENTS.md"])
            inactive = resolve_instruction_manifest(discovered, trusted=False, manifest_id="inactive")
            self.assertTrue(all(entry.disposition == "inactive" for entry in inactive.entries))
            active = resolve_instruction_manifest(discovered, trusted=True, manifest_id="active")
            self.assertEqual(len(active.overrides), 1)
            self.assertEqual(active.overrides[0].resolution_kind, "authority")
            self.assertEqual(classify_instruction_source_mutation("x/AGENTS.md"),
                             "exact_review_required")

    def test_escape_symlink_and_oversize_are_explicitly_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary)
            external = Path(outside) / "instructions"
            external.write_text("rule.x: escape\n", encoding="utf-8")
            (root / "AGENTS.md").symlink_to(external)
            discovery = discover_instruction_sources(
                repository_root=root, project_scope_root=root, workspace_root=root,
            )
            self.assertFalse(discovery.complete)
            self.assertEqual(discovery.sources[0].reason, "symlink_target_escapes_boundary")
            (root / "AGENTS.md").unlink()
            (root / "AGENTS.md").write_bytes(b"x" * (256 * 1024 + 1))
            oversize = discover_instruction_sources(
                repository_root=root, project_scope_root=root, workspace_root=root,
            )
            self.assertEqual(oversize.sources[0].reason, "source_byte_bound_exceeded")

    def test_equal_authority_conflict_is_durable_resolver_input(self) -> None:
        left = b"conflict.format: left\n"
        right = b"conflict.format: right\n"
        sources = tuple(
            DiscoveredInstruction(
                f"source-{index}", f"/repo/{index}/AGENTS.md", f"{index}/AGENTS.md",
                "conversation", 50, 5, sha256_bytes(content), "discovered", None, content,
            )
            for index, content in enumerate((left, right), start=1)
        )
        manifest = resolve_instruction_manifest(
            InstructionDiscovery("/repo", "/repo", "/repo", sources, {}, len(left) + len(right), True),
            trusted=True, manifest_id="conflicted",
        )
        self.assertEqual(manifest.unresolved_conflicts, ("conflict.format",))
        self.assertEqual(manifest.overrides, ())

    def test_prose_conflict_is_action_exact_and_unrelated_rules_remain_additive(self) -> None:
        contents = (
            b"Always run tests before finishing.\nAlways document public APIs.\n",
            b"Never run tests before finishing.\nAlways keep changes focused.\n",
        )
        sources = tuple(
            DiscoveredInstruction(
                f"prose-{index}", f"/repo/{index}/AGENTS.md", f"{index}/AGENTS.md",
                "conversation", 50, 5, sha256_bytes(content), "discovered", None, content,
            )
            for index, content in enumerate(contents, start=1)
        )
        manifest = resolve_instruction_manifest(
            InstructionDiscovery("/repo", "/repo", "/repo", sources, {},
                                 sum(map(len, contents)), True),
            trusted=True, manifest_id="prose",
        )
        self.assertEqual(
            manifest.unresolved_conflicts,
            ("prose.action.run tests before finishing",),
        )
        self.assertEqual(manifest.overrides, ())


if __name__ == "__main__":
    unittest.main()
