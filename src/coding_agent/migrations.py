from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Callable, Sequence

from coding_agent.domain import utc_now


LATEST_SCHEMA_VERSION = 7


class MigrationError(RuntimeError):
    """The SQLite schema cannot be migrated safely."""


class FutureSchemaVersion(MigrationError, ValueError):
    """The database was written by a newer application version."""


@dataclass(frozen=True)
class Migration:
    version: int
    statements: Sequence[str]


V1 = Migration(
    version=1,
    statements=(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            task TEXT NOT NULL,
            source_path TEXT NOT NULL,
            workspace_path TEXT,
            state TEXT NOT NULL,
            policy_json TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            final_answer TEXT,
            failure_json TEXT,
            step_count INTEGER NOT NULL DEFAULT 0,
            model_calls INTEGER NOT NULL DEFAULT 0,
            tool_calls INTEGER NOT NULL DEFAULT 0,
            last_event_sequence INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            message_index INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_call_id TEXT,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, message_index),
            UNIQUE(session_id, tool_call_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            schema_version INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            state TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, sequence)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS checkpoints (
            session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
            state TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    ),
)


V2 = Migration(
    version=2,
    statements=(
        "ALTER TABLE sessions ADD COLUMN lease_owner TEXT",
        "ALTER TABLE sessions ADD COLUMN lease_expires_at TEXT",
        "ALTER TABLE sessions ADD COLUMN interrupt_requested_at TEXT",
        "ALTER TABLE sessions ADD COLUMN resume_target_state TEXT",
        "ALTER TABLE sessions ADD COLUMN context_version TEXT NOT NULL DEFAULT '1'",
        """
        CREATE TABLE IF NOT EXISTS model_calls (
            request_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            backend TEXT NOT NULL,
            status TEXT NOT NULL,
            request_json TEXT NOT NULL,
            response_json TEXT,
            error_json TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            UNIQUE(session_id, ordinal)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tool_calls (
            call_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            tool_name TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            recovery_mode TEXT NOT NULL,
            status TEXT NOT NULL,
            pre_revision TEXT,
            planned_post_revision TEXT,
            result_json TEXT,
            error_json TEXT,
            started_at TEXT,
            finished_at TEXT,
            UNIQUE(session_id, ordinal)
        )
        """,
        "CREATE INDEX IF NOT EXISTS model_calls_session_ordinal ON model_calls(session_id, ordinal)",
        "CREATE INDEX IF NOT EXISTS tool_calls_session_ordinal ON tool_calls(session_id, ordinal)",
    ),
)


V3 = Migration(
    version=3,
    statements=(
        """
        CREATE TABLE IF NOT EXISTS summaries (
            summary_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            schema_version INTEGER NOT NULL,
            source_event_start INTEGER NOT NULL,
            source_event_end INTEGER NOT NULL,
            source_event_hash TEXT NOT NULL,
            workspace_revision TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            superseded_by TEXT,
            stale INTEGER NOT NULL DEFAULT 0,
            UNIQUE(session_id, summary_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS summaries_session_created "
        "ON summaries(session_id, created_at, summary_id)",
        "CREATE INDEX IF NOT EXISTS summaries_source_range "
        "ON summaries(session_id, source_event_start, source_event_end)",
    ),
)


V4 = Migration(
    version=4,
    statements=(
        """
        CREATE TABLE IF NOT EXISTS memory_records (
            memory_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            scope TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            source_run_id TEXT NOT NULL,
            source_agent_id TEXT NOT NULL,
            source_event_refs_json TEXT NOT NULL,
            repository_revision TEXT,
            confidence REAL NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            status TEXT NOT NULL,
            supersedes TEXT REFERENCES memory_records(memory_id),
            content_hash TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            UNIQUE(scope, scope_id, kind, content_hash)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS memory_events (
            event_id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL REFERENCES memory_records(memory_id),
            event_type TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS memory_retrievals (
            retrieval_id TEXT PRIMARY KEY,
            query_hash TEXT NOT NULL,
            session_id TEXT,
            repository_id TEXT,
            user_id TEXT,
            repository_revision TEXT,
            selected_json TEXT NOT NULL,
            token_cost INTEGER NOT NULL,
            duration_ms REAL NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS memory_records_scope_status "
        "ON memory_records(scope, scope_id, status, kind)",
        "CREATE INDEX IF NOT EXISTS memory_records_expiry "
        "ON memory_records(status, expires_at)",
        "CREATE INDEX IF NOT EXISTS memory_events_record_time "
        "ON memory_events(memory_id, created_at, event_id)",
        "CREATE INDEX IF NOT EXISTS memory_retrievals_query_time "
        "ON memory_retrievals(query_hash, created_at, retrieval_id)",
    ),
)


# M1 product-layer records are additive.  They intentionally do not alter the
# legacy Runtime tables: ``sessions`` remains the physical Runtime execution
# record throughout the compatibility window.
V5 = Migration(
    version=5,
    statements=(
        """
        CREATE TABLE IF NOT EXISTS repository_identities (
            repository_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS repository_descriptors (
            descriptor_id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL REFERENCES repository_identities(repository_id),
            descriptor_kind TEXT NOT NULL,
            descriptor_value TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            UNIQUE(repository_id, descriptor_kind, descriptor_value)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_scopes (
            project_scope_id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL REFERENCES repository_identities(repository_id),
            relative_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(repository_id, relative_path)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS workspace_bindings (
            workspace_binding_id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL REFERENCES repository_identities(repository_id),
            project_scope_id TEXT NOT NULL REFERENCES project_scopes(project_scope_id),
            binding_kind TEXT NOT NULL,
            locator TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS conversations (
            conversation_id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL REFERENCES repository_identities(repository_id),
            project_scope_id TEXT NOT NULL REFERENCES project_scopes(project_scope_id),
            default_workspace_binding_id TEXT NOT NULL REFERENCES workspace_bindings(workspace_binding_id),
            provenance_kind TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turns (
            turn_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
            ordinal INTEGER NOT NULL,
            provenance_kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(conversation_id, ordinal)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS runtime_executions (
            runtime_execution_id TEXT PRIMARY KEY,
            legacy_session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
            turn_id TEXT NOT NULL UNIQUE REFERENCES turns(turn_id),
            workspace_binding_id TEXT NOT NULL REFERENCES workspace_bindings(workspace_binding_id),
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS conversation_semantic_events (
            conversation_event_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
            sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            provenance_kind TEXT NOT NULL,
            provenance_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(conversation_id, sequence)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS product_mapping_failures (
            legacy_session_id TEXT PRIMARY KEY REFERENCES sessions(id),
            attempt_count INTEGER NOT NULL,
            last_error TEXT NOT NULL,
            first_failed_at TEXT NOT NULL,
            last_failed_at TEXT NOT NULL,
            recovered_at TEXT
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS repository_discovery_descriptor_unique
        ON repository_descriptors(descriptor_kind, descriptor_value)
        WHERE descriptor_kind IN ('canonical_path', 'git_common_dir')
        """,
        "CREATE INDEX IF NOT EXISTS runtime_executions_legacy_session ON runtime_executions(legacy_session_id)",
        "CREATE INDEX IF NOT EXISTS conversation_semantic_events_order ON conversation_semantic_events(conversation_id, sequence)",
    ),
)


# M2 adds the Product admission and coordination projection.  These records
# deliberately complement rather than replace the legacy Runtime journal: a
# Session remains the sole physical Runtime FSM authority.
V6 = Migration(
    version=6,
    statements=(
        "ALTER TABLE conversations ADD COLUMN product_version INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE conversations ADD COLUMN open_turn_id TEXT",
        """
        CREATE TABLE IF NOT EXISTS product_admissions (
            operation_id TEXT PRIMARY KEY,
            payload_digest TEXT NOT NULL,
            conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
            turn_id TEXT NOT NULL UNIQUE REFERENCES turns(turn_id),
            runtime_execution_id TEXT NOT NULL UNIQUE REFERENCES runtime_executions(runtime_execution_id),
            legacy_session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
            checkpoint_id TEXT NOT NULL,
            instruction_manifest_id TEXT NOT NULL,
            policy_epoch_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS product_inputs (
            input_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL UNIQUE,
            payload_digest TEXT NOT NULL,
            conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
            turn_id TEXT REFERENCES turns(turn_id),
            sequence INTEGER NOT NULL,
            input_kind TEXT NOT NULL,
            correlation_id TEXT,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(conversation_id, sequence)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turn_admission_artifacts (
            turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id),
            checkpoint_id TEXT NOT NULL UNIQUE,
            checkpoint_json TEXT NOT NULL,
            instruction_manifest_id TEXT NOT NULL UNIQUE,
            instruction_manifest_json TEXT NOT NULL,
            policy_epoch_id TEXT NOT NULL,
            policy_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS workspace_binding_observations (
            observation_id TEXT PRIMARY KEY,
            workspace_binding_id TEXT NOT NULL REFERENCES workspace_bindings(workspace_binding_id),
            observation_digest TEXT NOT NULL,
            observation_json TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            UNIQUE(workspace_binding_id, observation_digest)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS conversation_rebinds (
            rebind_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL UNIQUE,
            payload_digest TEXT NOT NULL,
            conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
            previous_workspace_binding_id TEXT NOT NULL REFERENCES workspace_bindings(workspace_binding_id),
            workspace_binding_id TEXT NOT NULL REFERENCES workspace_bindings(workspace_binding_id),
            expected_version INTEGER NOT NULL,
            resulting_version INTEGER NOT NULL,
            observation_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS workspace_writer_claims (
            workspace_binding_id TEXT PRIMARY KEY REFERENCES workspace_bindings(workspace_binding_id),
            claim_id TEXT NOT NULL UNIQUE,
            operation_id TEXT NOT NULL UNIQUE,
            payload_digest TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            runtime_execution_id TEXT REFERENCES runtime_executions(runtime_execution_id),
            claim_epoch INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL,
            observation_digest TEXT NOT NULL,
            observation_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            released_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS workspace_recovery_barriers (
            barrier_id TEXT PRIMARY KEY,
            workspace_binding_id TEXT NOT NULL REFERENCES workspace_bindings(workspace_binding_id),
            claim_id TEXT,
            reason TEXT NOT NULL,
            uncertain_invocation_id TEXT,
            resolver_kind TEXT,
            evidence_digest TEXT,
            resolved_by_operation_id TEXT,
            status TEXT NOT NULL,
            reconciliation_token TEXT,
            created_at TEXT NOT NULL,
            cleared_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS workspace_writer_operation_receipts (
            operation_id TEXT PRIMARY KEY,
            payload_digest TEXT NOT NULL,
            operation_kind TEXT NOT NULL,
            claim_id TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS turn_finalizations (
            turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id),
            runtime_execution_id TEXT NOT NULL UNIQUE REFERENCES runtime_executions(runtime_execution_id),
            outcome TEXT NOT NULL,
            finalized_at TEXT NOT NULL,
            repair_provenance TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS product_inputs_conversation_order ON product_inputs(conversation_id, sequence)",
        "CREATE INDEX IF NOT EXISTS workspace_observations_binding ON workspace_binding_observations(workspace_binding_id, observed_at)",
        "CREATE INDEX IF NOT EXISTS recovery_barriers_binding ON workspace_recovery_barriers(workspace_binding_id, status)",
        "CREATE INDEX IF NOT EXISTS writer_operation_receipts_claim ON workspace_writer_operation_receipts(claim_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS recovery_barriers_uncertain_invocation ON workspace_recovery_barriers(workspace_binding_id, uncertain_invocation_id) WHERE uncertain_invocation_id IS NOT NULL",
    ),
)


# M3 adds immutable instruction/context/request evidence.  It does not alter
# sessions, Runtime events, legacy model_calls, or any M1/M2 semantics.
V7 = Migration(
    version=7,
    statements=(
        """CREATE TABLE instruction_trusts (
            trust_id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL REFERENCES repository_identities(repository_id),
            project_scope_id TEXT NOT NULL REFERENCES project_scopes(project_scope_id),
            workspace_binding_id TEXT REFERENCES workspace_bindings(workspace_binding_id),
            source_kind TEXT NOT NULL,
            disposition TEXT NOT NULL CHECK(disposition IN ('active','revoked')),
            operation_id TEXT NOT NULL UNIQUE,
            payload_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            revoked_at TEXT,
            CHECK((disposition='active' AND revoked_at IS NULL) OR
                  (disposition='revoked' AND revoked_at IS NOT NULL))
        )""",
        """CREATE TABLE instruction_sources (
            source_id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL REFERENCES repository_identities(repository_id),
            project_scope_id TEXT NOT NULL REFERENCES project_scopes(project_scope_id),
            workspace_binding_id TEXT REFERENCES workspace_bindings(workspace_binding_id),
            provider_kind TEXT NOT NULL,
            locator TEXT NOT NULL,
            normalized_path TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            authority_rank INTEGER NOT NULL,
            specificity INTEGER NOT NULL,
            revision_digest TEXT NOT NULL,
            disposition TEXT NOT NULL CHECK(disposition IN ('effective','inactive','unavailable','stale','overridden','conflict')),
            reason TEXT,
            discovered_at TEXT NOT NULL,
            UNIQUE(project_scope_id, workspace_binding_id, provider_kind, locator, revision_digest)
        )""",
        """CREATE TABLE instruction_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            content_digest TEXT NOT NULL UNIQUE,
            canonical_utf8 BLOB NOT NULL,
            byte_count INTEGER NOT NULL CHECK(byte_count>=0 AND byte_count<=262144),
            created_at TEXT NOT NULL,
            CHECK(length(canonical_utf8)=byte_count)
        )""",
        """CREATE TABLE instruction_manifests (
            instruction_manifest_id TEXT PRIMARY KEY,
            conversation_id TEXT REFERENCES conversations(conversation_id),
            turn_id TEXT REFERENCES turns(turn_id),
            workspace_binding_id TEXT NOT NULL REFERENCES workspace_bindings(workspace_binding_id),
            parent_manifest_id TEXT REFERENCES instruction_manifests(instruction_manifest_id),
            revision INTEGER NOT NULL CHECK(revision>=1),
            effective_digest TEXT NOT NULL,
            refresh_operation_id TEXT UNIQUE,
            refresh_reason TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            load_sequence_frontier INTEGER NOT NULL CHECK(load_sequence_frontier>=0),
            status TEXT NOT NULL CHECK(status IN ('active','stale','conflicted','legacy_placeholder')),
            created_at TEXT NOT NULL,
            UNIQUE(conversation_id, revision)
        )""",
        """CREATE TABLE instruction_manifest_entries (
            entry_id TEXT PRIMARY KEY,
            instruction_manifest_id TEXT NOT NULL REFERENCES instruction_manifests(instruction_manifest_id),
            source_id TEXT NOT NULL REFERENCES instruction_sources(source_id),
            snapshot_id TEXT REFERENCES instruction_snapshots(snapshot_id),
            sequence INTEGER NOT NULL CHECK(sequence>=1),
            authority_rank INTEGER NOT NULL,
            specificity INTEGER NOT NULL,
            trust_disposition TEXT NOT NULL,
            disposition TEXT NOT NULL,
            reason TEXT,
            source_digest TEXT NOT NULL,
            UNIQUE(instruction_manifest_id, sequence),
            UNIQUE(instruction_manifest_id, source_id)
        )""",
        """CREATE TABLE instruction_override_edges (
            edge_id TEXT PRIMARY KEY,
            instruction_manifest_id TEXT NOT NULL REFERENCES instruction_manifests(instruction_manifest_id),
            winner_entry_id TEXT NOT NULL REFERENCES instruction_manifest_entries(entry_id),
            loser_entry_id TEXT NOT NULL REFERENCES instruction_manifest_entries(entry_id),
            conflict_key TEXT NOT NULL,
            resolution_kind TEXT NOT NULL CHECK(resolution_kind IN ('authority','specificity','unresolved')),
            created_at TEXT NOT NULL,
            UNIQUE(instruction_manifest_id, winner_entry_id, loser_entry_id, conflict_key),
            CHECK(winner_entry_id<>loser_entry_id)
        )""",
        """CREATE TABLE instruction_manifest_status_events (
            status_event_id TEXT PRIMARY KEY,
            instruction_manifest_id TEXT NOT NULL REFERENCES instruction_manifests(instruction_manifest_id),
            status TEXT NOT NULL CHECK(status IN ('active','stale','conflicted')),
            reason TEXT NOT NULL,
            observed_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(instruction_manifest_id, status, observed_digest)
        )""",
        """CREATE TABLE instruction_manifest_conflicts (
            conflict_id TEXT PRIMARY KEY,
            instruction_manifest_id TEXT NOT NULL REFERENCES instruction_manifests(instruction_manifest_id),
            conflict_key TEXT NOT NULL,
            candidate_entry_ids_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('unresolved','resolved')),
            created_at TEXT NOT NULL,
            UNIQUE(instruction_manifest_id, conflict_key)
        )""",
        """CREATE TABLE frozen_model_requests (
            request_id TEXT PRIMARY KEY,
            runtime_execution_id TEXT REFERENCES runtime_executions(runtime_execution_id),
            legacy_session_id TEXT NOT NULL REFERENCES sessions(id),
            request_ordinal INTEGER NOT NULL CHECK(request_ordinal>=1),
            request_kind TEXT NOT NULL CHECK(request_kind IN ('agent','summary_auxiliary','legacy_import')),
            context_manifest_id TEXT NOT NULL UNIQUE,
            request_digest TEXT NOT NULL,
            request_json TEXT NOT NULL,
            audit_json TEXT NOT NULL,
            semantic_prefix_end_sequence INTEGER,
            semantic_prefix_digest TEXT,
            legacy_model_call_id TEXT REFERENCES model_calls(request_id),
            runtime_event_sequence INTEGER NOT NULL CHECK(runtime_event_sequence>=0),
            runtime_version INTEGER NOT NULL CHECK(runtime_version>=0),
            created_at TEXT NOT NULL,
            UNIQUE(legacy_session_id, request_ordinal, request_kind),
            FOREIGN KEY(context_manifest_id) REFERENCES context_manifests(context_manifest_id)
                DEFERRABLE INITIALLY DEFERRED
        )""",
        """CREATE TABLE context_manifests (
            context_manifest_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL UNIQUE,
            request_digest TEXT NOT NULL,
            manifest_digest TEXT NOT NULL UNIQUE,
            manifest_json TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            token_counter_version TEXT NOT NULL,
            provider_capability_json TEXT NOT NULL,
            context_window INTEGER NOT NULL CHECK(context_window>0),
            output_reserve INTEGER NOT NULL CHECK(output_reserve>=0),
            framing_margin INTEGER NOT NULL CHECK(framing_margin>=0),
            tool_schema_tokens INTEGER NOT NULL CHECK(tool_schema_tokens>=0),
            required_tokens INTEGER NOT NULL CHECK(required_tokens>=0),
            optional_tokens INTEGER NOT NULL CHECK(optional_tokens>=0),
            memory_status TEXT NOT NULL CHECK(memory_status='absent'),
            created_at TEXT NOT NULL,
            FOREIGN KEY(request_id) REFERENCES frozen_model_requests(request_id)
                DEFERRABLE INITIALLY DEFERRED
        )""",
        """CREATE TABLE model_attempts (
            attempt_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL REFERENCES frozen_model_requests(request_id),
            ordinal INTEGER NOT NULL CHECK(ordinal>=1),
            backend TEXT NOT NULL,
            intent_status TEXT NOT NULL CHECK(intent_status IN ('committed_not_dispatched','dispatched')),
            wall_started_at TEXT NOT NULL,
            monotonic_started REAL,
            created_at TEXT NOT NULL,
            UNIQUE(request_id, ordinal)
        )""",
        """CREATE TABLE model_attempt_outcomes (
            outcome_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL UNIQUE REFERENCES model_attempts(attempt_id),
            outcome_kind TEXT NOT NULL CHECK(outcome_kind IN ('succeeded','failed','cancelled','unknown','reused')),
            response_json TEXT,
            error_json TEXT,
            usage_json TEXT,
            usage_classification TEXT NOT NULL CHECK(usage_classification IN ('measured','estimated','synthetic','unknown')),
            wall_finished_at TEXT,
            monotonic_elapsed_ms REAL,
            coverage_status TEXT NOT NULL CHECK(coverage_status IN ('complete','censored','incomplete','unknown')),
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE model_attempt_dispatches (
            dispatch_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL UNIQUE REFERENCES model_attempts(attempt_id),
            dispatched_at TEXT NOT NULL,
            monotonic_started REAL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE context_operations (
            context_operation_id TEXT PRIMARY KEY,
            request_id TEXT REFERENCES frozen_model_requests(request_id),
            conversation_id TEXT REFERENCES conversations(conversation_id),
            operation_kind TEXT NOT NULL CHECK(operation_kind IN ('composition','selection','compaction')),
            status TEXT NOT NULL CHECK(status IN ('started','succeeded','overflow','rejected',
                                                 'failed','stale_rebuild','unknown','cancelled')),
            policy_version TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            elapsed_ms REAL,
            metrics_json TEXT NOT NULL,
            coverage_status TEXT NOT NULL
        )""",
        """CREATE TABLE context_selection_items (
            selection_id TEXT PRIMARY KEY,
            context_operation_id TEXT NOT NULL REFERENCES context_operations(context_operation_id),
            source_class TEXT NOT NULL,
            source_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence>=1),
            disposition TEXT NOT NULL CHECK(disposition IN ('included','omitted','stale','inactive','untrusted','superseded')),
            reason TEXT NOT NULL,
            token_count INTEGER NOT NULL CHECK(token_count>=0),
            token_classification TEXT NOT NULL CHECK(token_classification IN ('measured','estimated','synthetic','unknown')),
            source_digest TEXT NOT NULL,
            UNIQUE(context_operation_id, source_class, source_id)
        )""",
        """CREATE TABLE conversation_summary_artifacts (
            summary_artifact_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
            source_start_sequence INTEGER NOT NULL CHECK(source_start_sequence>=1),
            source_end_sequence INTEGER NOT NULL CHECK(source_end_sequence>=source_start_sequence),
            source_digest TEXT NOT NULL,
            auxiliary_request_id TEXT REFERENCES frozen_model_requests(request_id),
            auxiliary_attempt_id TEXT REFERENCES model_attempts(attempt_id),
            content TEXT NOT NULL,
            content_digest TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            generator_version TEXT NOT NULL,
            parent_summary_id TEXT REFERENCES conversation_summary_artifacts(summary_artifact_id),
            instruction_manifest_id TEXT REFERENCES instruction_manifests(instruction_manifest_id),
            workspace_binding_id TEXT REFERENCES workspace_bindings(workspace_binding_id),
            status TEXT NOT NULL CHECK(status IN ('valid','stale','failed','legacy_unavailable','superseded')),
            superseded_by TEXT REFERENCES conversation_summary_artifacts(summary_artifact_id),
            created_at TEXT NOT NULL,
            UNIQUE(conversation_id, source_digest, policy_version)
        )""",
        """CREATE TABLE conversation_summary_claims (
            claim_id TEXT PRIMARY KEY,
            summary_artifact_id TEXT NOT NULL REFERENCES conversation_summary_artifacts(summary_artifact_id),
            claim_kind TEXT NOT NULL CHECK(claim_kind IN ('conversational_intent','explicit_decision','completed_work','unresolved_decision','repository_code_fact','tool_test_fact')),
            claim_text TEXT NOT NULL,
            source_start_sequence INTEGER NOT NULL,
            source_end_sequence INTEGER NOT NULL,
            source_artifact_id TEXT,
            source_revision TEXT,
            status TEXT NOT NULL CHECK(status IN ('valid','stale','historical')),
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE tool_result_artifacts (
            artifact_id TEXT PRIMARY KEY,
            legacy_session_id TEXT NOT NULL REFERENCES sessions(id),
            tool_call_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            encoding TEXT NOT NULL,
            content BLOB,
            content_digest TEXT NOT NULL,
            captured_size INTEGER NOT NULL CHECK(captured_size>=0),
            range_start INTEGER NOT NULL CHECK(range_start>=0),
            range_end INTEGER NOT NULL CHECK(range_end>=range_start),
            capture_limit INTEGER NOT NULL CHECK(capture_limit>=0),
            capture_completeness TEXT NOT NULL CHECK(capture_completeness IN ('complete','incomplete','unknown')),
            created_at TEXT NOT NULL,
            UNIQUE(legacy_session_id, tool_call_id, channel, range_start, range_end)
        )""",
        """CREATE TABLE normalized_observations (
            observation_id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL REFERENCES tool_result_artifacts(artifact_id),
            status_kind TEXT NOT NULL,
            semantic_json TEXT NOT NULL,
            excerpt TEXT,
            excerpt_digest TEXT,
            prompt_truncated INTEGER NOT NULL CHECK(prompt_truncated IN (0,1)),
            projection_completeness TEXT NOT NULL CHECK(projection_completeness IN ('complete','incomplete','unknown')),
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE context_excerpts (
            excerpt_id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL REFERENCES tool_result_artifacts(artifact_id),
            range_start INTEGER NOT NULL,
            range_end INTEGER NOT NULL,
            content_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK(range_start>=0 AND range_end>=range_start)
        )""",
        """CREATE TABLE file_context_items (
            file_context_item_id TEXT PRIMARY KEY,
            workspace_binding_id TEXT NOT NULL REFERENCES workspace_bindings(workspace_binding_id),
            normalized_path TEXT NOT NULL,
            file_type TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            revision TEXT NOT NULL,
            byte_start INTEGER NOT NULL,
            byte_end INTEGER NOT NULL,
            line_start INTEGER,
            line_end INTEGER,
            range_digest TEXT NOT NULL,
            origin_kind TEXT NOT NULL,
            origin_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('current','historical','excluded','unavailable')),
            content BLOB,
            encoding TEXT NOT NULL DEFAULT 'binary',
            capture_completeness TEXT NOT NULL DEFAULT 'unknown'
                CHECK(capture_completeness IN ('complete','incomplete','unknown')),
            created_at TEXT NOT NULL,
            CHECK(byte_start>=0 AND byte_end>=byte_start)
        )""",
        """CREATE TABLE product_operation_receipts (
            operation_id TEXT PRIMARY KEY,
            payload_digest TEXT NOT NULL,
            operation_kind TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE m3_metric_samples (
            metric_sample_id TEXT PRIMARY KEY,
            metric_name TEXT NOT NULL,
            population_kind TEXT NOT NULL,
            conversation_id TEXT REFERENCES conversations(conversation_id),
            turn_id TEXT REFERENCES turns(turn_id),
            runtime_execution_id TEXT REFERENCES runtime_executions(runtime_execution_id),
            request_id TEXT REFERENCES frozen_model_requests(request_id),
            attempt_id TEXT REFERENCES model_attempts(attempt_id),
            context_operation_id TEXT REFERENCES context_operations(context_operation_id),
            value REAL,
            unit TEXT NOT NULL,
            classification TEXT NOT NULL CHECK(classification IN ('measured','estimated','synthetic','unknown','censored')),
            coverage_status TEXT NOT NULL CHECK(coverage_status IN ('complete','incomplete','unknown','censored')),
            dimensions_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS instruction_manifest_entries_order ON instruction_manifest_entries(instruction_manifest_id, sequence)",
        "CREATE INDEX IF NOT EXISTS instruction_sources_scope ON instruction_sources(repository_id, project_scope_id, workspace_binding_id, normalized_path)",
        "CREATE INDEX IF NOT EXISTS instruction_manifests_revision ON instruction_manifests(conversation_id, revision)",
        "CREATE INDEX IF NOT EXISTS instruction_manifest_status_history ON instruction_manifest_status_events(instruction_manifest_id, created_at)",
        "CREATE INDEX IF NOT EXISTS model_attempts_request_order ON model_attempts(request_id, ordinal)",
        "CREATE INDEX IF NOT EXISTS model_attempt_dispatches_attempt ON model_attempt_dispatches(attempt_id)",
        "CREATE INDEX IF NOT EXISTS context_selection_operation_order ON context_selection_items(context_operation_id, sequence)",
        "CREATE INDEX IF NOT EXISTS summary_prefix ON conversation_summary_artifacts(conversation_id, source_end_sequence)",
        "CREATE INDEX IF NOT EXISTS file_context_binding_path ON file_context_items(workspace_binding_id, normalized_path, revision)",
        "CREATE INDEX IF NOT EXISTS m3_metrics_population ON m3_metric_samples(metric_name, population_kind, created_at)",
        """CREATE TRIGGER instruction_snapshots_no_update BEFORE UPDATE ON instruction_snapshots BEGIN SELECT RAISE(ABORT, 'instruction snapshots are immutable'); END""",
        """CREATE TRIGGER instruction_snapshots_no_delete BEFORE DELETE ON instruction_snapshots BEGIN SELECT RAISE(ABORT, 'instruction snapshots are immutable'); END""",
        """CREATE TRIGGER instruction_manifests_no_update BEFORE UPDATE ON instruction_manifests BEGIN SELECT RAISE(ABORT, 'instruction manifests are immutable'); END""",
        """CREATE TRIGGER instruction_manifests_no_delete BEFORE DELETE ON instruction_manifests BEGIN SELECT RAISE(ABORT, 'instruction manifests are immutable'); END""",
        """CREATE TRIGGER instruction_manifest_entries_no_update BEFORE UPDATE ON instruction_manifest_entries BEGIN SELECT RAISE(ABORT, 'instruction manifest entries are immutable'); END""",
        """CREATE TRIGGER instruction_manifest_entries_no_delete BEFORE DELETE ON instruction_manifest_entries BEGIN SELECT RAISE(ABORT, 'instruction manifest entries are immutable'); END""",
        """CREATE TRIGGER frozen_model_requests_no_update BEFORE UPDATE ON frozen_model_requests BEGIN SELECT RAISE(ABORT, 'frozen model requests are immutable'); END""",
        """CREATE TRIGGER frozen_model_requests_no_delete BEFORE DELETE ON frozen_model_requests BEGIN SELECT RAISE(ABORT, 'frozen model requests are immutable'); END""",
        """CREATE TRIGGER context_manifests_no_update BEFORE UPDATE ON context_manifests BEGIN SELECT RAISE(ABORT, 'context manifests are immutable'); END""",
        """CREATE TRIGGER context_manifests_no_delete BEFORE DELETE ON context_manifests BEGIN SELECT RAISE(ABORT, 'context manifests are immutable'); END""",
        """CREATE TRIGGER model_attempts_no_update BEFORE UPDATE ON model_attempts BEGIN SELECT RAISE(ABORT, 'model attempts are append-only'); END""",
        """CREATE TRIGGER model_attempts_no_delete BEFORE DELETE ON model_attempts BEGIN SELECT RAISE(ABORT, 'model attempts are append-only'); END""",
        """CREATE TRIGGER model_attempt_dispatches_no_update BEFORE UPDATE ON model_attempt_dispatches BEGIN SELECT RAISE(ABORT, 'model dispatches are append-only'); END""",
        """CREATE TRIGGER model_attempt_dispatches_no_delete BEFORE DELETE ON model_attempt_dispatches BEGIN SELECT RAISE(ABORT, 'model dispatches are append-only'); END""",
        """CREATE TRIGGER model_attempt_outcomes_no_update BEFORE UPDATE ON model_attempt_outcomes BEGIN SELECT RAISE(ABORT, 'model outcomes are append-only'); END""",
        """CREATE TRIGGER model_attempt_outcomes_no_delete BEFORE DELETE ON model_attempt_outcomes BEGIN SELECT RAISE(ABORT, 'model outcomes are append-only'); END""",
    ),
)


MIGRATIONS: tuple[Migration, ...] = (V1, V2, V3, V4, V5, V6, V7)


def _validate_migrations(migrations: Sequence[Migration]) -> None:
    versions = [migration.version for migration in migrations]
    if versions != sorted(set(versions)):
        raise MigrationError("migration versions must be strictly increasing")
    if not versions or versions[-1] != LATEST_SCHEMA_VERSION:
        raise MigrationError("migration list does not end at the latest schema version")


class MigrationRunner:
    """Apply ordered, transactional schema migrations to one connection."""

    def __init__(
        self,
        migrations: Sequence[Migration] = MIGRATIONS,
        *,
        clock: Callable[[], str] = utc_now,
        migration_fault_hook: Callable[[int, str], None] | None = None,
    ):
        _validate_migrations(migrations)
        self.migrations = tuple(migrations)
        self.clock = clock
        self.migration_fault_hook = migration_fault_hook

    @property
    def latest_version(self) -> int:
        return self.migrations[-1].version

    def migrate(self, connection: sqlite3.Connection) -> int:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.commit()

        rows = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        applied = {int(row[0]) for row in rows}
        future = sorted(version for version in applied if version > self.latest_version)
        if future:
            raise FutureSchemaVersion(
                "database schema version is newer than this application: "
                f"{future[-1]} > {self.latest_version}"
            )

        for migration in self.migrations:
            if migration.version in applied:
                continue
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in migration.statements:
                    connection.execute(statement)
                if self.migration_fault_hook is not None:
                    self.migration_fault_hook(migration.version, "before_provenance_insert")
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (migration.version, self.clock()),
                )
                if self.migration_fault_hook is not None:
                    self.migration_fault_hook(migration.version, "before_commit")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            applied.add(migration.version)
        return self.latest_version


def migrate(connection: sqlite3.Connection) -> int:
    """Convenience entry point for the default migration set."""

    return MigrationRunner().migrate(connection)


apply_migrations = migrate
run_migrations = migrate
