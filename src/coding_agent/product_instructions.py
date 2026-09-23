"""Bounded, observation-only M3 instruction discovery and resolution.

This module reads only the exact ``AGENTS.md`` provider surface.  It owns no
trust authority or lifecycle state; callers supply the durable trust decision
and persist the returned immutable records through the Product repository.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from typing import Iterable, Mapping

from coding_agent.product_domain import (
    InstructionManifestEntry,
    InstructionOverrideEdge,
    InstructionSnapshot,
    InstructionSourceRecord,
)


INSTRUCTION_FILENAME = "AGENTS.md"
MAX_SOURCE_BYTES = 256 * 1024
MAX_SOURCES = 64
MAX_DISCOVERY_BYTES = 1024 * 1024
INSTRUCTION_POLICY_VERSION = "m3-instructions-v1"


def canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_id(kind: str, *parts: str) -> str:
    payload = canonical_json([kind, *parts])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, payload))


@dataclass(frozen=True)
class DiscoveredInstruction:
    source_id: str
    locator: str
    normalized_path: str
    scope_kind: str
    authority_rank: int
    specificity: int
    revision_digest: str
    disposition: str
    reason: str | None
    content: bytes | None


@dataclass(frozen=True)
class InstructionDiscovery:
    repository_root: str
    project_scope_root: str
    workspace_root: str
    sources: tuple[DiscoveredInstruction, ...]
    exclusions: Mapping[str, str]
    observed_bytes: int
    complete: bool


@dataclass(frozen=True)
class ResolvedInstructionManifest:
    manifest_id: str
    effective_digest: str
    entries: tuple[InstructionManifestEntry, ...]
    overrides: tuple[InstructionOverrideEdge, ...]
    unresolved_conflicts: tuple[str, ...]
    policy_version: str = INSTRUCTION_POLICY_VERSION

    @property
    def effective_content(self) -> tuple[str, ...]:
        return tuple(
            entry.source.snapshot.content
            for entry in self.entries
            if entry.disposition == "effective" and entry.source.snapshot is not None
        )


def _contained(path: Path, boundary: Path) -> bool:
    try:
        path.relative_to(boundary)
        return True
    except ValueError:
        return False


def _chain(start: Path, boundary: Path) -> list[Path]:
    if not _contained(start, boundary):
        raise ValueError("instruction discovery path escapes its accepted boundary")
    result: list[Path] = []
    current = start
    while True:
        result.append(current)
        if current == boundary:
            break
        current = current.parent
    return list(reversed(result))


def _nested_repository(path: Path, repository_root: Path) -> bool:
    current = path
    while current != repository_root:
        marker = current / ".git"
        if marker.exists() or marker.is_symlink():
            return True
        current = current.parent
    return False


def discover_instruction_sources(
    *,
    repository_root: str | Path,
    project_scope_root: str | Path,
    workspace_root: str | Path,
    target_paths: Iterable[str | Path] = (),
) -> InstructionDiscovery:
    """Discover exact instruction files without traversal or subprocesses."""
    repository = Path(repository_root).resolve(strict=True)
    project = Path(project_scope_root).resolve(strict=True)
    workspace = Path(workspace_root).resolve(strict=True)
    if not repository.is_dir() or not project.is_dir() or not workspace.is_dir():
        raise ValueError("instruction discovery roots must be directories")
    if not _contained(project, repository) or not _contained(workspace, project):
        raise ValueError("instruction discovery roots are not contained")

    directories: set[Path] = set(_chain(project, repository))
    directories.update(_chain(workspace, project))
    for raw_target in target_paths:
        target = Path(raw_target)
        candidate = target if target.is_absolute() else workspace / target
        parent = candidate if candidate.is_dir() else candidate.parent
        resolved_parent = parent.resolve(strict=True)
        directories.update(_chain(resolved_parent, project))

    sources: list[DiscoveredInstruction] = []
    exclusions: dict[str, str] = {}
    observed_bytes = 0
    complete = True
    for directory in sorted(directories, key=lambda item: item.relative_to(repository).as_posix()):
        candidate = directory / INSTRUCTION_FILENAME
        normalized = candidate.relative_to(repository).as_posix()
        if _nested_repository(directory, repository):
            exclusions[normalized] = "unrelated_nested_repository"
            complete = False
            continue
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            exclusions[normalized] = "lstat_unavailable"
            complete = False
            continue
        if len(sources) >= MAX_SOURCES:
            exclusions[normalized] = "source_count_bound_exceeded"
            complete = False
            break
        locator = str(candidate)
        reason: str | None
        if stat.S_ISLNK(metadata.st_mode):
            resolved_target: Path | None
            try:
                resolved_target = candidate.resolve(strict=True)
            except RuntimeError:
                reason = "cyclic_symlink"
                resolved_target = None
            except OSError:
                reason = "broken_or_unverifiable_symlink"
                resolved_target = None
            else:
                reason = (
                    None if _contained(resolved_target, repository)
                    else "symlink_target_escapes_boundary"
                )
            if reason is not None or resolved_target is None:
                revision = sha256_bytes(os.readlink(candidate).encode("utf-8", errors="surrogateescape"))
                sources.append(DiscoveredInstruction(
                    stable_id("instruction-source", locator, revision), locator, normalized,
                    "path" if directory != repository else "repository", 30 if directory != repository else 20,
                    len(directory.relative_to(repository).parts), revision, "unavailable", reason, None,
                ))
                complete = False
                continue
            read_path = resolved_target
        elif stat.S_ISREG(metadata.st_mode):
            read_path = candidate
        else:
            revision = sha256_bytes(f"mode:{metadata.st_mode}".encode("ascii"))
            sources.append(DiscoveredInstruction(
                stable_id("instruction-source", locator, revision), locator, normalized,
                "path" if directory != repository else "repository", 30 if directory != repository else 20,
                len(directory.relative_to(repository).parts), revision, "unavailable",
                "not_a_regular_file", None,
            ))
            complete = False
            continue
        content: bytes | None
        try:
            read_metadata = read_path.stat()
        except OSError:
            read_metadata = None
        if read_metadata is None or not stat.S_ISREG(read_metadata.st_mode):
            revision = sha256_bytes(f"target-unavailable:{normalized}".encode("utf-8"))
            sources.append(DiscoveredInstruction(
                stable_id("instruction-source", locator, revision), locator, normalized,
                "path" if directory != repository else "repository", 30 if directory != repository else 20,
                len(directory.relative_to(repository).parts), revision, "unavailable",
                "symlink_target_not_regular" if stat.S_ISLNK(metadata.st_mode) else "stat_unavailable",
                None,
            ))
            exclusions[normalized] = sources[-1].reason or "unavailable"
            complete = False
            continue
        if read_metadata.st_size > MAX_SOURCE_BYTES:
            revision = sha256_bytes(f"oversize:{read_metadata.st_size}".encode("ascii"))
            sources.append(DiscoveredInstruction(
                stable_id("instruction-source", locator, revision), locator, normalized,
                "path" if directory != repository else "repository", 30 if directory != repository else 20,
                len(directory.relative_to(repository).parts), revision, "unavailable",
                "source_byte_bound_exceeded", None,
            ))
            complete = False
            continue
        try:
            with read_path.open("rb") as handle:
                content = handle.read(MAX_SOURCE_BYTES + 1)
        except OSError:
            content = None
            reason = "content_read_unavailable"
        else:
            reason = None
            if len(content) > MAX_SOURCE_BYTES:
                content = None
                reason = "source_byte_bound_exceeded_during_read"
        if content is not None and observed_bytes + len(content) > MAX_DISCOVERY_BYTES:
            content = None
            reason = "discovery_byte_bound_exceeded"
        if content is not None:
            try:
                content.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                reason = "invalid_utf8"
                content = None
        revision = sha256_bytes(content) if content is not None else sha256_bytes(
            f"unavailable:{normalized}:{reason}".encode("utf-8")
        )
        if content is not None:
            observed_bytes += len(content)
        else:
            complete = False
            exclusions[normalized] = str(reason)
        sources.append(DiscoveredInstruction(
            stable_id("instruction-source", locator, revision), locator, normalized,
            "path" if directory != repository else "repository", 30 if directory != repository else 20,
            len(directory.relative_to(repository).parts), revision,
            "discovered" if content is not None else "unavailable", reason, content,
        ))
    return InstructionDiscovery(
        str(repository), str(project), str(workspace), tuple(sources), exclusions,
        observed_bytes, complete,
    )


def _structured_rules(content: str) -> dict[str, str]:
    rules: dict[str, str] = {}
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized_key = key.strip().casefold()
        if normalized_key.startswith("rule.") or normalized_key.startswith("conflict."):
            rules[normalized_key] = value.strip()
    # A bounded deterministic grammar for direct prohibitions and obligations.
    # The normalized action must match exactly; unknown prose stays additive.
    for sentence in re.split(r"[\n.!?]+", content.casefold()):
        normalized = " ".join(sentence.split())
        match = re.fullmatch(
            r"(always|never|do not|don't|must|must not) ([a-z][a-z0-9 _/-]{1,120})",
            normalized,
        )
        if match is None:
            continue
        negative = match.group(1) in {"never", "do not", "don't", "must not"}
        action = " ".join(match.group(2).split())
        rules[f"prose.action.{action}"] = "forbid" if negative else "require"
    return rules


def resolve_instruction_manifest(
    discovery: InstructionDiscovery,
    *,
    trusted: bool,
    manifest_id: str | None = None,
) -> ResolvedInstructionManifest:
    """Resolve one immutable manifest with deterministic precedence evidence."""
    selected_manifest_id = manifest_id or str(uuid.uuid4())
    records: list[InstructionSourceRecord] = []
    for source in discovery.sources:
        snapshot: InstructionSnapshot | None = None
        disposition = source.disposition
        reason = source.reason
        if source.content is not None and trusted:
            text = source.content.decode("utf-8")
            snapshot = InstructionSnapshot(
                stable_id("instruction-snapshot", source.revision_digest),
                source.revision_digest,
                text,
            )
            disposition = "effective"
        elif source.content is not None:
            disposition = "inactive"
            reason = "untrusted_soft_context"
        records.append(InstructionSourceRecord(
            source.source_id, source.locator, source.normalized_path, source.scope_kind,
            source.authority_rank, source.specificity, source.revision_digest,
            disposition, reason, snapshot,
        ))

    entries = [
        InstructionManifestEntry(
            stable_id("instruction-entry", selected_manifest_id, record.source_id), record, index,
            "active" if trusted else "inactive", record.disposition, record.reason,
        )
        for index, record in enumerate(
            sorted(records, key=lambda item: (item.authority_rank, item.specificity, item.normalized_path)),
            start=1,
        )
    ]
    by_key: dict[str, list[tuple[InstructionManifestEntry, str]]] = {}
    for entry in entries:
        if entry.source.snapshot is None or entry.disposition != "effective":
            continue
        for key, value in _structured_rules(entry.source.snapshot.content).items():
            by_key.setdefault(key, []).append((entry, value))
    overrides: list[InstructionOverrideEdge] = []
    unresolved: list[str] = []
    for key, candidates in sorted(by_key.items()):
        values = {value for _, value in candidates}
        if len(values) <= 1:
            continue
        ordered = sorted(
            candidates,
            key=lambda pair: (
                pair[0].source.authority_rank, pair[0].source.specificity,
                pair[0].source.normalized_path,
            ),
            reverse=True,
        )
        winner, _ = ordered[0]
        second = ordered[1][0]
        if (
            winner.source.authority_rank == second.source.authority_rank
            and winner.source.specificity == second.source.specificity
        ):
            unresolved.append(key)
            continue
        for loser, _ in ordered[1:]:
            resolution = (
                "authority" if winner.source.authority_rank != loser.source.authority_rank
                else "specificity"
            )
            overrides.append(InstructionOverrideEdge(
                stable_id("instruction-override", winner.entry_id, loser.entry_id, key),
                winner.entry_id, loser.entry_id, key, resolution,
            ))
    manifest_payload = {
        "policy_version": INSTRUCTION_POLICY_VERSION,
        "entries": [
            {
                "entry_id": entry.entry_id,
                "source_id": entry.source.source_id,
                "revision_digest": entry.source.revision_digest,
                "snapshot_digest": (
                    entry.source.snapshot.content_digest if entry.source.snapshot else None
                ),
                "authority_rank": entry.source.authority_rank,
                "specificity": entry.source.specificity,
                "trust": entry.trust_disposition,
                "disposition": entry.disposition,
                "reason": entry.reason,
            }
            for entry in entries
        ],
        "overrides": [edge.__dict__ for edge in overrides],
        "unresolved_conflicts": sorted(unresolved),
    }
    digest = sha256_bytes(canonical_json(manifest_payload).encode("utf-8"))
    return ResolvedInstructionManifest(
        selected_manifest_id, digest, tuple(entries),
        tuple(overrides), tuple(sorted(unresolved)),
    )


def classify_instruction_source_mutation(path: str | Path) -> str:
    """Pure M3 seam; M4 later maps this classification to exact ASK."""
    return "exact_review_required" if Path(path).name == INSTRUCTION_FILENAME else "ordinary_path"
