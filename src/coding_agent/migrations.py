from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Callable, Sequence

from coding_agent.domain import utc_now


LATEST_SCHEMA_VERSION = 3


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


MIGRATIONS: tuple[Migration, ...] = (V1, V2, V3)


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
    ):
        _validate_migrations(migrations)
        self.migrations = tuple(migrations)
        self.clock = clock

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
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (migration.version, self.clock()),
                )
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
