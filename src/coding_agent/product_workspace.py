"""M2 observation-only direct-working-tree Product composition.

This module intentionally has no subprocess, mutating filesystem, ToolHarness,
or Runtime dependency.  It is the closed M2 gate, not a preview of M4.
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol

from coding_agent.domain import Permission, ToolCall, ToolResult
from coding_agent.persistence import DirectTreeReadOnlyViolation, ProductLifecycleConflict
from coding_agent.product_domain import ProjectScope, WorkspaceBinding
from coding_agent.tools.base import ToolContext
from coding_agent.tools.harness import ToolHarness
from coding_agent.workspace import WorkspaceGuard


class _WorkspaceRegistry(Protocol):
    def register_repository_identity(self, descriptors: Mapping[str, str], *, repository_id: str | None = None) -> str:
        ...

    def register_project_scope(self, repository_id: str, relative_path: str = ".") -> ProjectScope:
        ...

    def register_workspace_binding(
        self, *, repository_id: str, project_scope_id: str, binding_kind: str,
        locator: str, observation: Mapping[str, object] | None = None,
    ) -> WorkspaceBinding:
        ...


@dataclass(frozen=True)
class DirectWorkspaceObservation:
    canonical_path: str
    git_layout: str
    git_common_dir: str | None
    directory_digest: str
    repository_root: str | None = None
    observation_frontier: str = "complete_bounded_filesystem_observation"
    exclusions: Mapping[str, str] = field(default_factory=dict)
    git_facts: Mapping[str, str] = field(default_factory=dict)
    observed_entries: int = 0
    observed_bytes: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "canonical_path": self.canonical_path,
            "git_layout": self.git_layout,
            "git_common_dir": self.git_common_dir,
            "directory_digest": self.directory_digest,
            "repository_root": self.repository_root,
            "observation_frontier": self.observation_frontier,
            "exclusions": dict(self.exclusions),
            "git_facts": dict(self.git_facts),
            "observed_entries": self.observed_entries,
            "observed_bytes": self.observed_bytes,
            "observation_mode": "m2_filesystem_only_no_git_process",
        }


@dataclass(frozen=True)
class DirectWorkspaceBinding:
    repository_id: str
    project_scope: ProjectScope
    binding: WorkspaceBinding
    observation: DirectWorkspaceObservation


def _git_root_and_entry(root: Path) -> tuple[Path | None, Path | None]:
    """Locate only explicit on-disk Git metadata, without invoking Git."""
    for candidate in (root, *root.parents):
        entry = candidate / ".git"
        if entry.is_dir() or entry.is_file():
            return candidate, entry
    return None, None


def _digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_loose_or_packed_ref(common_dir: Path, ref: str) -> str:
    """Observe one ref without invoking Git or loading repository config."""
    loose = common_dir / ref
    try:
        if loose.is_file():
            return hashlib.sha256(loose.read_bytes()).hexdigest()
        packed = common_dir / "packed-refs"
        if packed.is_file():
            for raw_line in packed.read_text(encoding="ascii", errors="strict").splitlines():
                if not raw_line or raw_line.startswith(("#", "^")):
                    continue
                value, separator, name = raw_line.partition(" ")
                if separator and name == ref:
                    return hashlib.sha256(value.encode("ascii", errors="strict")).hexdigest()
    except (OSError, UnicodeError):
        return "unavailable_read_error"
    return "unavailable_missing"


def observe_direct_workspace(directory: str | Path) -> DirectWorkspaceObservation:
    """Observe a supplied directory without cwd fallback or Git execution."""
    try:
        root = Path(directory).resolve(strict=True)
    except OSError as exc:
        raise ProductLifecycleConflict("direct workspace must be an existing concrete directory") from exc
    if not root.is_dir():
        raise ProductLifecycleConflict("direct workspace must be a concrete directory")
    repository_root, git_entry = _git_root_and_entry(root)
    common_dir: str | None = None
    git_dir: Path | None = None
    if git_entry is not None and git_entry.is_dir():
        git_layout = "git_directory"
        git_dir = git_entry
        common_pointer = git_entry / "commondir"
        if common_pointer.is_file():
            common_value = common_pointer.read_text(encoding="utf-8", errors="replace").strip()
            common_dir = str((git_entry / common_value).resolve() if not Path(common_value).is_absolute() else Path(common_value).resolve())
        else:
            common_dir = str(git_entry.resolve())
    elif git_entry is not None and git_entry.is_file():
        # A worktree .git file is plain layout metadata.  Parsing the single
        # gitdir pointer is an observation, not an invocation/config lookup.
        text = git_entry.read_text(encoding="utf-8", errors="replace").strip()
        if not text.startswith("gitdir: "):
            git_layout = "git_file_unrecognized"
        else:
            target = Path(text.removeprefix("gitdir: ").strip())
            git_dir = (git_entry.parent / target).resolve() if not target.is_absolute() else target.resolve()
            common_pointer = git_dir / "commondir"
            if common_pointer.is_file():
                relative_common = common_pointer.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
                common = (git_dir / relative_common).resolve()
            else:
                common = git_dir
            git_layout = "git_worktree_file"
            common_dir = str(common)
    else:
        git_layout = "non_git"
    git_facts: dict[str, str] = {
        "branch_head": "unavailable_no_git_process",
        "branch_tip": "unavailable_no_git_process",
        "index": "unavailable_no_git_process",
        "dirty": "unavailable_no_git_process",
        "staged": "unavailable_no_git_process",
        "untracked": "unavailable_no_git_process",
    }
    common_path = Path(common_dir) if common_dir is not None else git_dir
    if git_dir is not None:
        for name, fact in (("HEAD", "branch_head"), ("index", "index")):
            candidate = git_dir / name
            try:
                git_facts[fact] = _digest_file(candidate) if candidate.is_file() else "unavailable_missing"
            except OSError:
                git_facts[fact] = "unavailable_read_error"
        # HEAD's symbolic reference alone is not the commit evidence: observe
        # the referenced loose ref as well when it is safely available.
        try:
            head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="strict").strip()
            if head.startswith("ref: "):
                ref = head.removeprefix("ref: ").strip()
                if ref.startswith("refs/") and ".." not in Path(ref).parts:
                    # Linked worktrees keep HEAD in the worktree gitdir but
                    # branch refs in the shared common directory.
                    git_facts["branch_tip"] = (
                        _read_loose_or_packed_ref(common_path, ref)
                        if common_path is not None else "unavailable_missing"
                    )
            else:
                git_facts["branch_tip"] = hashlib.sha256(head.encode("ascii", errors="strict")).hexdigest()
        except (OSError, UnicodeError):
            git_facts["branch_tip"] = "unavailable_read_error"
    # Bounded, in-process content evidence detects equal-size drift without
    # invoking Git, aliases, hooks, filters, or repository configuration.
    digest = hashlib.sha256()
    observed_entries = 0
    observed_bytes = 0
    exclusions: dict[str, str] = {"git_status": "unavailable_without_git_process"}
    incomplete = False
    max_files = 10_000
    max_bytes = 8 * 1024 * 1024
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        if ".git" in directories:
            exclusions[".git"] = "git_metadata_observed_separately"
        directories[:] = sorted(name for name in directories if name != ".git")
        relative_directory = current_path.relative_to(root).as_posix()
        digest.update(b"D\0")
        digest.update(relative_directory.encode("utf-8", errors="surrogateescape"))
        for name in directories:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            try:
                stat = path.lstat()
            except OSError:
                incomplete = True
                exclusions[relative] = "lstat_unavailable"
                continue
            observed_entries += 1
            if observed_entries > max_files:
                digest.update(b"m2-observation-frontier-exhausted")
                return DirectWorkspaceObservation(
                    str(root), git_layout, common_dir, digest.hexdigest(),
                    str(repository_root) if repository_root is not None else None,
                    "incomplete_bound_exhausted",
                    {**exclusions, relative: "entry_bound_exceeded"}, git_facts,
                    observed_entries, observed_bytes,
                )
            digest.update(b"E\0")
            digest.update(relative.encode("utf-8", errors="surrogateescape"))
            digest.update(str(stat.st_mode).encode())
            if path.is_symlink():
                try:
                    digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
                except OSError:
                    incomplete = True
                    exclusions[relative] = "symlink_target_unavailable"
                    digest.update(b"unavailable-symlink-target")
        for name in sorted(files):
            path = current_path / name
            try:
                stat = path.lstat()
            except OSError:
                incomplete = True
                exclusions[path.relative_to(root).as_posix()] = "lstat_unavailable"
                continue
            observed_entries += 1
            relative = path.relative_to(root).as_posix()
            if observed_entries > max_files:
                digest.update(b"m2-observation-frontier-exhausted")
                return DirectWorkspaceObservation(
                    str(root), git_layout, common_dir, digest.hexdigest(),
                    str(repository_root) if repository_root is not None else None,
                    "incomplete_bound_exhausted",
                    {**exclusions, relative: "entry_bound_exceeded"}, git_facts,
                    observed_entries, observed_bytes,
                )
            digest.update(relative.encode("utf-8", errors="surrogateescape"))
            digest.update(str(stat.st_mode).encode())
            digest.update(str(stat.st_size).encode())
            if observed_bytes + stat.st_size > max_bytes:
                # An observation frontier is explicit rather than silently
                # claiming a complete content fingerprint.
                digest.update(b"m2-observation-frontier-exhausted")
                return DirectWorkspaceObservation(
                    str(root), git_layout, common_dir, digest.hexdigest(),
                    str(repository_root) if repository_root is not None else None,
                    "incomplete_bound_exhausted",
                    {**exclusions, relative: "file_or_byte_bound_exceeded"}, git_facts,
                    observed_entries, observed_bytes,
                )
            if path.is_symlink():
                try:
                    digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
                except OSError:
                    incomplete = True
                    exclusions[relative] = "symlink_target_unavailable"
                    digest.update(b"unavailable-symlink-target")
            elif path.is_file():
                try:
                    # Read no more than the declared remaining frontier plus
                    # one byte, so a racing file cannot cause an unbounded read.
                    remaining = max_bytes - observed_bytes
                    with path.open("rb") as handle:
                        content = handle.read(remaining + 1)
                    if len(content) > remaining:
                        digest.update(b"m2-observation-frontier-exhausted-during-read")
                        return DirectWorkspaceObservation(
                            str(root), git_layout, common_dir, digest.hexdigest(),
                            str(repository_root) if repository_root is not None else None,
                            "incomplete_bound_exhausted",
                            {**exclusions, relative: "byte_bound_exceeded_during_read"}, git_facts,
                            observed_entries, observed_bytes,
                        )
                    digest.update(hashlib.sha256(content).digest())
                    observed_bytes += len(content)
                except OSError:
                    incomplete = True
                    exclusions[relative] = "content_read_unavailable"
                    digest.update(b"unavailable-file-content")
    return DirectWorkspaceObservation(
        str(root), git_layout, common_dir, digest.hexdigest(),
        str(repository_root) if repository_root is not None else None,
        "incomplete_observation" if incomplete else "complete_bounded_filesystem_observation",
        exclusions, git_facts, observed_entries, observed_bytes,
    )


def capture_direct_tree_manifest(directory: str | Path) -> dict[str, object]:
    """Capture the reproducible M2 no-mutation gate surface.

    The manifest covers worktree files/directories/symlink targets plus the
    Git HEAD, refs, index, packed refs, and lock-path presence.  It never runs
    Git and never follows a symlink while hashing content.
    """
    observation = observe_direct_workspace(directory)
    root = Path(observation.canonical_path)
    entries: list[dict[str, object]] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(directories)
        relative_directory = current_path.relative_to(root).as_posix()
        directory_stat = current_path.lstat()
        entries.append({
            "path": relative_directory,
            "kind": "directory",
            "mode": directory_stat.st_mode,
            "type_bits": stat_module.S_IFMT(directory_stat.st_mode),
        })
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                path_stat = path.lstat()
                entries.append({
                    "path": path.relative_to(root).as_posix(),
                    "kind": "symlink", "target": os.readlink(path),
                    "mode": path_stat.st_mode,
                    "type_bits": stat_module.S_IFMT(path_stat.st_mode),
                })
        for name in sorted(files):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            stat = path.lstat()
            if path.is_symlink():
                entries.append({
                    "path": relative, "kind": "symlink", "target": os.readlink(path),
                    "mode": stat.st_mode, "type_bits": stat_module.S_IFMT(stat.st_mode),
                })
            else:
                entries.append({
                    "path": relative, "kind": "file", "mode": stat.st_mode,
                    "type_bits": stat_module.S_IFMT(stat.st_mode),
                    "size": stat.st_size, "sha256": _digest_file(path),
                })
    git_paths: list[dict[str, object]] = []
    git_dir: Path | None = None
    repository_root, git_entry = _git_root_and_entry(root)
    if git_entry is not None:
        if git_entry.is_file():
            text = git_entry.read_text(encoding="utf-8", errors="replace").strip()
            if text.startswith("gitdir: "):
                pointer = Path(text.removeprefix("gitdir: ").strip())
                git_dir = (git_entry.parent / pointer).resolve() if not pointer.is_absolute() else pointer.resolve()
        else:
            git_dir = git_entry.resolve()
    common_dir = Path(observation.git_common_dir) if observation.git_common_dir else git_dir
    candidates: set[Path] = set()
    for base in (git_dir, common_dir):
        if base is None:
            continue
        candidates.update(base / name for name in ("HEAD", "index", "index.lock", "packed-refs", "packed-refs.lock", "config.lock", "HEAD.lock"))
        refs = base / "refs"
        if refs.is_dir():
            candidates.update(path for path in refs.rglob("*") if path.is_file() or path.is_symlink())
        # Absence of common lock locations is evidence too.
        locks = base / "refs"
        if locks.is_dir():
            candidates.update(path for path in locks.rglob("*.lock"))
    for path in sorted(candidates, key=str):
        if path.is_symlink():
            git_paths.append({"path": str(path), "kind": "symlink", "target": os.readlink(path)})
        elif path.is_file():
            stat = path.lstat()
            git_paths.append({"path": str(path), "kind": "file", "size": stat.st_size, "sha256": _digest_file(path)})
        else:
            git_paths.append({"path": str(path), "kind": "absent"})
    return {
        "schema_version": 1,
        "root": str(root),
        "repository_root": str(repository_root) if repository_root is not None else None,
        "entries": entries,
        "git_paths": git_paths,
    }


def discover_direct_workspace(registry: _WorkspaceRegistry, directory: str | Path) -> DirectWorkspaceBinding:
    """Idempotently persist a read-only direct WorkspaceBinding observation."""
    observation = observe_direct_workspace(directory)
    descriptors = {"canonical_path": observation.repository_root or observation.canonical_path}
    if observation.git_common_dir:
        descriptors["git_common_dir"] = observation.git_common_dir
    repository_id = registry.register_repository_identity(descriptors)
    root = Path(observation.repository_root or observation.canonical_path)
    scope_path = Path(observation.canonical_path).relative_to(root).as_posix()
    scope = registry.register_project_scope(repository_id, scope_path)
    binding = registry.register_workspace_binding(
        repository_id=repository_id,
        project_scope_id=scope.project_scope_id,
        binding_kind="m2_direct_read_only",
        locator=observation.canonical_path,
        observation=observation.to_dict(),
    )
    return DirectWorkspaceBinding(repository_id, scope, binding, observation)


class ReadOnlyDirectProductComposition:
    """A deliberately capability-empty direct-tree composition for M2."""

    def __init__(self, binding: DirectWorkspaceBinding, *, harness: ToolHarness | None = None):
        self.binding = binding
        self._harness = harness
        self._guard = WorkspaceGuard(binding.observation.canonical_path)

    def read_observation(self) -> DirectWorkspaceObservation:
        return observe_direct_workspace(self.binding.observation.canonical_path)

    def reject_side_effect(self, action: str) -> None:
        raise DirectTreeReadOnlyViolation(
            f"M2 direct-working-tree gate rejects {action!r}; M3+M4 are required"
        )

    def read(self, *, call_id: str, tool_name: str, arguments: dict[str, object]) -> ToolResult:
        """Run a bounded read through the retained Harness and WorkspaceGuard.

        The M2 direct composition deliberately presents only the READ
        permission.  Rejecting by tool identity before Harness dispatch closes
        command/cache/Git/helper paths even if a caller holds a broader legacy
        registry.
        """
        if tool_name not in {"read_file", "search_files"}:
            self.reject_side_effect(tool_name)
        if self._harness is None:
            raise ProductLifecycleConflict("direct read-only composition requires a ToolHarness")
        return self._harness.execute(
            ToolCall(id=call_id, name=tool_name, arguments=arguments),
            ToolContext(workspace=self._guard, allowed_permissions=frozenset({Permission.READ})),
        )

    def edit(self, *args: object, **kwargs: object) -> None:
        self.reject_side_effect("edit")

    def run_command(self, *args: object, **kwargs: object) -> None:
        self.reject_side_effect("command")

    def run_test(self, *args: object, **kwargs: object) -> None:
        self.reject_side_effect("test/build/cache")

    def undo(self, *args: object, **kwargs: object) -> None:
        self.reject_side_effect("undo")
