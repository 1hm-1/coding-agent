"""Verify the offline release-package oracle used by the V1 M0 task set."""

from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit


LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def _relative_link_targets(markdown: str) -> list[str]:
    targets: list[str] = []
    for match in LINK_PATTERN.finditer(markdown):
        raw_target = match.group(1).strip()
        if raw_target.startswith("<") and ">" in raw_target:
            raw_target = raw_target[1 : raw_target.index(">")]
        else:
            raw_target = raw_target.split(maxsplit=1)[0]
        parsed = urlsplit(raw_target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        targets.append(unquote(parsed.path))
    return targets


def verify(snapshot: Path, dist: Path, version: str) -> None:
    normalized_name = "production_coding_agent"
    sdist = dist / f"{normalized_name}-{version}.tar.gz"
    wheel = dist / f"{normalized_name}-{version}-py3-none-any.whl"
    release_document = snapshot / "docs" / "releases" / f"v{version}.md"

    missing = [str(path) for path in (sdist, wheel, release_document) if not path.is_file()]
    if missing:
        raise SystemExit(f"missing required release artifact(s): {', '.join(missing)}")

    metadata_member = f"{normalized_name}-{version}.dist-info/METADATA"
    with zipfile.ZipFile(wheel) as archive:
        try:
            metadata = archive.read(metadata_member).decode("utf-8")
        except KeyError as exc:
            raise SystemExit(f"wheel lacks {metadata_member}") from exc
    if f"Version: {version}\n" not in metadata:
        raise SystemExit(f"wheel METADATA does not declare Version: {version}")

    unresolved = []
    for target in _relative_link_targets(release_document.read_text(encoding="utf-8")):
        if not (release_document.parent / target).resolve().exists():
            unresolved.append(target)
    if unresolved:
        raise SystemExit(f"unresolved relative release-document links: {unresolved}")

    print(f"release package oracle passed for v{version}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--version", default="0.1.0")
    args = parser.parse_args()
    verify(args.snapshot.resolve(), args.dist.resolve(), args.version)


if __name__ == "__main__":
    main()
