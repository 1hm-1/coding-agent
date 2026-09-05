from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from coding_agent.domain import WorkspaceViolation, utc_now
from coding_agent.domain import FileFact, RepositorySnapshot, TestFact


@dataclass(frozen=True)
class WorkspaceManifest:
    session_id: str
    source_path: str
    workspace_path: str
    created_at: str
    removed_external_symlinks: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class WorkspaceGuard:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve(strict=True)

    def resolve(self, relative_path: str, *, must_exist: bool = True) -> Path:
        supplied = Path(relative_path)
        if supplied.is_absolute():
            raise WorkspaceViolation("absolute paths are not allowed")
        if any(part == ".." for part in supplied.parts):
            raise WorkspaceViolation("parent traversal is not allowed")

        candidate = (self.root / supplied).resolve(strict=False)
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceViolation("path escapes the isolated workspace") from exc
        if relative.parts and relative.parts[0] == ".git":
            raise WorkspaceViolation("direct access to workspace git metadata is not allowed")
        if must_exist and not candidate.exists():
            raise FileNotFoundError(relative_path)
        return candidate

    def relative(self, path: Path) -> str:
        return path.resolve(strict=False).relative_to(self.root).as_posix()


class WorkspaceManager:
    def __init__(self, agent_home: str | Path):
        self.agent_home = Path(agent_home)
        self.workspaces_root = self.agent_home / "workspaces"
        self.workspaces_root.mkdir(parents=True, exist_ok=True)

    def create(self, source: str | Path, session_id: str) -> WorkspaceManifest:
        if not session_id or not session_id.replace("-", "").replace("_", "").isalnum():
            raise WorkspaceViolation("session id contains unsafe path characters")
        source_path = Path(source).resolve(strict=True)
        if not source_path.is_dir():
            raise WorkspaceViolation("source repository must be a directory")
        destination = self.workspaces_root / session_id / "repo"
        if destination.exists():
            if destination.is_dir() and tree_fingerprint(source_path) == tree_fingerprint(destination):
                return WorkspaceManifest(
                    session_id=session_id,
                    source_path=str(source_path),
                    workspace_path=str(destination.resolve()),
                    created_at=utc_now(),
                    removed_external_symlinks=(),
                )
            raise WorkspaceViolation(
                f"workspace already exists and cannot be safely recovered for session {session_id}"
            )
        destination.parent.mkdir(parents=True, exist_ok=False)

        shutil.copytree(
            source_path,
            destination,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", ".agent-data", "__pycache__", ".pytest_cache"),
        )
        removed = self._remove_external_symlinks(destination)
        self._initialize_git(destination)
        return WorkspaceManifest(
            session_id=session_id,
            source_path=str(source_path),
            workspace_path=str(destination.resolve()),
            created_at=utc_now(),
            removed_external_symlinks=tuple(removed),
        )

    def get(self, session_id: str) -> WorkspaceGuard:
        root = self.workspaces_root / session_id / "repo"
        return WorkspaceGuard(root)

    def repository_snapshot(
        self,
        session_id: str,
        *,
        read_paths: Iterable[str] = (),
        last_test: TestFact | None = None,
        file_limit: int = 200,
    ) -> RepositorySnapshot:
        """Build bounded, read-only metadata from an isolated workspace."""

        if file_limit <= 0:
            raise ValueError("repository snapshot file limit must be positive")
        root = self.get(session_id).root
        paths = tuple(repository_files(root, limit=file_limit))
        allowed = set(paths)
        read_facts: list[FileFact] = []
        for relative in sorted(set(read_paths)):
            if relative not in allowed:
                continue
            path = self.get(session_id).resolve(relative)
            if not path.is_file() or path.is_symlink():
                continue
            read_facts.append(
                FileFact(
                    path=relative,
                    content_hash=file_content_hash(path),
                    revision=tree_fingerprint(root),
                )
            )
        return RepositorySnapshot(
            workspace_revision=tree_fingerprint(root),
            file_paths=paths,
            diff_summary=git_diff_summary(root),
            read_files=tuple(read_facts),
            last_test=last_test,
        )

    @staticmethod
    def _remove_external_symlinks(root: Path) -> list[str]:
        resolved_root = root.resolve()
        removed: list[str] = []
        for current, dir_names, file_names in os.walk(root, followlinks=False):
            for name in [*dir_names, *file_names]:
                candidate = Path(current) / name
                if not candidate.is_symlink():
                    continue
                target = candidate.resolve(strict=False)
                try:
                    target.relative_to(resolved_root)
                except ValueError:
                    removed.append(candidate.relative_to(root).as_posix())
                    candidate.unlink()
                    if name in dir_names:
                        dir_names.remove(name)
        return sorted(removed)

    @staticmethod
    def _initialize_git(root: Path) -> None:
        commands = (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            [
                "git",
                "-c",
                "user.name=Coding Agent",
                "-c",
                "user.email=agent@localhost",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "isolated workspace baseline",
            ],
        )
        for argv in commands:
            completed = subprocess.run(
                argv,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                raise WorkspaceViolation(
                    f"failed to initialize workspace git repository: {completed.stderr.strip()}"
                )


def tree_fingerprint(root: str | Path) -> str:
    base = Path(root).resolve(strict=True)
    digest = hashlib.sha256()
    for path in sorted(_iter_workspace_files(base), key=lambda item: item.as_posix()):
        relative = path.relative_to(base).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(os.readlink(path).encode("utf-8", errors="replace"))
        else:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(64 * 1024), b""):
                    digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def workspace_revision(root: str | Path) -> str:
    """Compatibility name for the future context engine's revision provider."""

    return tree_fingerprint(root)


def file_content_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_diff_summary(root: str | Path, *, max_output_chars: int = 4096) -> str:
    """Return bounded diff metadata without exposing arbitrary command execution."""

    completed = subprocess.run(
        ["git", "diff", "--stat", "--no-renames"],
        cwd=Path(root),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        return "git_diff_unavailable"
    return completed.stdout[:max_output_chars]


def repository_files(root: str | Path, *, limit: int = 200) -> list[str]:
    base = Path(root).resolve(strict=True)
    return [path.relative_to(base).as_posix() for path in _iter_workspace_files(base)][:limit]


def _iter_workspace_files(root: Path) -> Iterable[Path]:
    for current, dir_names, file_names in os.walk(root, followlinks=False):
        dir_names[:] = sorted(name for name in dir_names if name != ".git")
        for name in sorted(file_names):
            yield Path(current) / name
