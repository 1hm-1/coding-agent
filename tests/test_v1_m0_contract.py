from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from coding_agent.application import AgentApplication
from coding_agent.cli import build_parser
from coding_agent.domain import Permission, RunPolicy, RuntimeState
from coding_agent.migrations import LATEST_SCHEMA_VERSION, MIGRATIONS, MigrationRunner
from coding_agent.models.scripted import ScriptedBackend
from coding_agent.workspace import tree_fingerprint


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "v1_m0"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


class M0ProtectedCompatibilityTest(unittest.TestCase):
    def test_semantic_goldens_ipc_goldens_schemas_and_vectors_are_unchanged(self) -> None:
        manifest = json.loads((FIXTURES / "protected_hashes.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        for relative, expected in manifest["files"].items():
            with self.subTest(path=relative):
                self.assertEqual(sha256_file(ROOT / relative), expected)

    def test_expected_gap_catalog_is_complete_and_milestone_owned(self) -> None:
        catalog = json.loads((FIXTURES / "expected_gaps.json").read_text(encoding="utf-8"))
        gaps = {item["id"]: item for item in catalog["gaps"]}
        self.assertEqual(
            set(gaps),
            {
                "multi_turn_continuity",
                "direct_workspace_binding",
                "ephemeral_interactive_startup",
                "product_conversation_resume",
                "typed_permission_lifecycle",
                "frozen_uncertain_retry",
                "controlled_code_undo",
                "interactive_responsiveness",
            },
        )
        self.assertTrue(all(item["owner"].startswith("M") for item in gaps.values()))

    def test_cli_remains_legacy_one_shot_surface(self) -> None:
        parser = build_parser()
        subcommands = next(
            action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        )
        names = set(subcommands.choices)
        self.assertTrue(
            {
                "run-scripted",
                "run",
                "sessions",
                "show",
                "resume",
                "interrupt",
                "resolve-call",
                "replay",
                "export-trace",
                "evaluate",
                "protocol-info",
                "run-headless",
            }
            <= names
        )
        self.assertTrue({"undo", "new", "status", "diff", "permissions"}.isdisjoint(names))


class M0OneShotAndDefaultsTest(unittest.TestCase):
    def test_two_tasks_create_distinct_sessions_and_copied_workspaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="coding-agent-v1-m0-one-shot-") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "value.txt").write_text("original\n", encoding="utf-8")
            source_before = tree_fingerprint(source)
            application = AgentApplication(root / "agent-home")
            script = [{"final": "done"}]
            first = application.run_task(source=source, task="First.", backend=ScriptedBackend(script))
            second = application.run_task(source=source, task="Second.", backend=ScriptedBackend(script))
            self.assertIs(first.state, RuntimeState.COMPLETED)
            self.assertIs(second.state, RuntimeState.COMPLETED)
            self.assertNotEqual(first.session_id, second.session_id)
            self.assertNotEqual(first.workspace_path, second.workspace_path)
            self.assertNotEqual(first.workspace_path, str(source))
            self.assertEqual(tree_fingerprint(source), source_before)
            self.assertEqual(application.journal.list_messages(first.session_id)[0].content, "First.")
            self.assertEqual(application.journal.list_messages(second.session_id)[0].content, "Second.")

    def test_permission_denial_is_observation_without_typed_permission_tables(self) -> None:
        with tempfile.TemporaryDirectory(prefix="coding-agent-v1-m0-denial-") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "value.txt").write_text("old\n", encoding="utf-8")
            application = AgentApplication(root / "agent-home")
            backend = ScriptedBackend(
                [
                    {
                        "tool_calls": [
                            {
                                "id": "edit-denied",
                                "name": "edit_file",
                                "arguments": {
                                    "path": "value.txt",
                                    "old_text": "old",
                                    "new_text": "new",
                                },
                            }
                        ]
                    },
                    {"final": "denied observed"},
                ]
            )
            result = application.run_task(
                source=source,
                task="Attempt an edit.",
                backend=backend,
                policy=RunPolicy(allowed_permissions=frozenset({Permission.READ})),
            )
            self.assertIs(result.state, RuntimeState.COMPLETED)
            observations = [message for message in application.journal.list_messages(result.session_id) if message.role == "tool"]
            self.assertEqual(len(observations), 1)
            self.assertEqual(json.loads(observations[0].content)["status"], "permission_denied")
            tables = {
                row[0]
                for row in application.journal.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("permission_requests", tables)
            self.assertNotIn("permission_decisions", tables)
            self.assertEqual((Path(result.workspace_path) / "value.txt").read_text(), "old\n")


class M0MigrationStartingPointTest(unittest.TestCase):
    def _seed(self, connection: sqlite3.Connection, version: int) -> None:
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for migration in MIGRATIONS[:version]:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (migration.version, "2026-09-21T00:00:00+00:00"),
            )
        connection.commit()

    def test_every_committed_schema_start_migrates_idempotently_to_current_schema(self) -> None:
        catalog = json.loads((FIXTURES / "migration_catalog.json").read_text(encoding="utf-8"))
        # The catalog is immutable M0 evidence: its v4 is historical rather
        # than a claim that later additive migrations do not exist.
        self.assertEqual(catalog["latest"], 4)
        self.assertGreaterEqual(LATEST_SCHEMA_VERSION, catalog["latest"])
        for start in catalog["supported_starts"]:
            with self.subTest(start=start), tempfile.TemporaryDirectory() as temporary:
                connection = sqlite3.connect(Path(temporary) / "state.db")
                if start:
                    self._seed(connection, start)
                runner = MigrationRunner()
                self.assertEqual(runner.migrate(connection), LATEST_SCHEMA_VERSION)
                self.assertEqual(runner.migrate(connection), LATEST_SCHEMA_VERSION)
                versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
                self.assertEqual(versions, list(range(1, LATEST_SCHEMA_VERSION + 1)))
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                expected = {"schema_migrations"}
                for names in catalog["versions"].values():
                    expected.update(names)
                expected.update(
                    {
                        "repository_identities",
                        "repository_descriptors",
                        "project_scopes",
                        "workspace_bindings",
                        "conversations",
                        "turns",
                        "runtime_executions",
                        "conversation_semantic_events",
                        "product_mapping_failures",
                        "product_admissions",
                        "product_inputs",
                        "turn_admission_artifacts",
                        "workspace_binding_observations",
                        "conversation_rebinds",
                        "workspace_writer_claims",
                        "workspace_writer_operation_receipts",
                        "workspace_recovery_barriers",
                        "turn_finalizations",
                        "instruction_trusts",
                        "instruction_sources",
                        "instruction_snapshots",
                        "instruction_manifests",
                        "instruction_manifest_entries",
                        "instruction_override_edges",
                        "instruction_manifest_status_events",
                        "instruction_manifest_conflicts",
                        "frozen_model_requests",
                        "context_manifests",
                        "model_attempts",
                        "model_attempt_outcomes",
                        "model_attempt_dispatches",
                        "context_operations",
                        "context_selection_items",
                        "conversation_summary_artifacts",
                        "conversation_summary_claims",
                        "tool_result_artifacts",
                        "normalized_observations",
                        "context_excerpts",
                        "file_context_items",
                        "product_operation_receipts",
                        "m3_metric_samples",
                    }
                )
                self.assertEqual(tables, expected)
                connection.close()


if __name__ == "__main__":
    unittest.main()
