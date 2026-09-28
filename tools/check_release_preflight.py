#!/usr/bin/env python3
"""Check whether a Git diff is ready for release publication.

The brand kit has two kinds of changes with consumer-visible release impact:
authoritative files under ``source/`` and the complete derived-asset inventory.
Those changes must be accompanied by a new canonical ``VERSION`` value, a
dated matching changelog section, and the repository's Forgejo publication
workflow. Documentation, metadata, and operational changes may remain under
``Unreleased``.

The check is intentionally local and read-only. It classifies committed Git
paths and validates the release inputs in the candidate tree; it does not call
Forgejo, create tags, or publish anything.

Typical use, from the repository root::

    python3 tools/check_release_preflight.py --base v1.0.0

The base should be the last published release (usually its tag), while the
candidate defaults to ``HEAD``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parent.parent
VERSION_PATH = Path("VERSION")
CHANGELOG_PATH = Path("CHANGELOG.md")
PUBLICATION_WORKFLOW_PATH = Path("docs/notes/release-publication.md")
PUBLICATION_PUBLISHER_PATH = Path("tools/release_publish.py")

# Keep this inventory in lockstep with tools/check_reproducibility.py.  The
# source sidecars are source changes, while all files in these directories are
# consumer-visible derived output.
DERIVED_DIRECTORIES = ("avatars", "banners", "favicon", "logo")
DERIVED_FILES = ("palette.json", "platform-assets.json")

VERSION_PATTERN = re.compile(r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
RELEASE_HEADING_PATTERN = re.compile(
    r"^## \[(?P<version>v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))\]"
    r" - (?P<released>[0-9]{4}-[0-9]{2}-[0-9]{2})$"
)
PUBLICATION_WORKFLOW_MARKERS = (
    "# Forgejo release-publication workflow",
    "tools/release_publish.py",
    "--tag",
    "--ci-run",
    "published Forgejo Release",
)


class ReleasePreflightError(ValueError):
    """A release-required diff is missing a required publication input."""


@dataclass(frozen=True)
class DiffClassification:
    """The release significance of the paths in one candidate diff."""

    paths: tuple[str, ...]
    source: tuple[str, ...]
    derived: tuple[str, ...]
    documentation: tuple[str, ...]
    metadata: tuple[str, ...]
    other: tuple[str, ...]

    @property
    def release_required(self) -> bool:
        return bool(self.source or self.derived)

    @property
    def kind(self) -> str:
        if self.release_required:
            return "release-required"
        if self.documentation and not (self.metadata or self.other):
            return "documentation-only"
        if self.metadata and not (self.documentation or self.other):
            return "metadata-only"
        if not self.paths:
            return "empty"
        return "non-release"


@dataclass(frozen=True)
class PreflightReport:
    """Successful preflight results, suitable for callers and CLI output."""

    classification: DiffClassification
    version: str | None
    changelog_heading: str | None
    publication_workflow: Path | None


def _normalize_path(path: str | Path) -> str:
    value = str(path).replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value


def _is_under(path: str, directory: str) -> bool:
    return path == directory or path.startswith(f"{directory}/")


def classify_paths(paths: Iterable[str | Path]) -> DiffClassification:
    """Classify changed repository paths against release-relevant inventories."""

    normalized = tuple(sorted({_normalize_path(path) for path in paths if str(path)}))
    source: list[str] = []
    derived: list[str] = []
    documentation: list[str] = []
    metadata: list[str] = []
    other: list[str] = []

    for path in normalized:
        if _is_under(path, "source"):
            source.append(path)
        elif path in DERIVED_FILES or any(
            _is_under(path, directory) for directory in DERIVED_DIRECTORIES
        ):
            derived.append(path)
        elif path == "README.md" or _is_under(path, "docs") or path.endswith(
            (".md", ".mdx", ".rst", ".txt")
        ):
            documentation.append(path)
        elif (
            path == str(VERSION_PATH)
            or path == ".gitignore"
            or path == ".needle.yaml"
            or path.endswith((".json", ".jsonl", ".toml", ".yaml", ".yml"))
            or _is_under(path, "release-evidence")
            or _is_under(path, "automation")
        ):
            metadata.append(path)
        else:
            other.append(path)

    return DiffClassification(
        paths=normalized,
        source=tuple(source),
        derived=tuple(derived),
        documentation=tuple(documentation),
        metadata=tuple(metadata),
        other=tuple(other),
    )


def _git(*arguments: str, root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = ""
        if isinstance(error, subprocess.CalledProcessError):
            detail = (error.stderr or error.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise ReleasePreflightError(
            f"could not inspect Git diff ({' '.join(arguments)}){suffix}"
        ) from error
    return result.stdout


def changed_paths(root: Path, base: str, head: str = "HEAD") -> tuple[str, ...]:
    """Return every old and new path touched by a Git diff.

    Including both sides of renames/deletions prevents a source or generated
    file from escaping the release gate merely because it was renamed.
    """

    output = _git(
        "diff",
        "--name-status",
        "--find-renames",
        "--find-copies",
        base,
        head,
        "--",
        root=root,
    )
    paths: list[str] = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        status = fields[0]
        if status.startswith(("R", "C")) and len(fields) >= 3:
            paths.extend(fields[1:3])
        else:
            paths.append(fields[1])
    return tuple(sorted({_normalize_path(path) for path in paths}))


def _read_candidate(root: Path, relative: Path) -> str:
    path = root / relative
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise ReleasePreflightError(f"required file is missing or unreadable: {relative}") from error


def _read_version(root: Path) -> str:
    content = _read_candidate(root, VERSION_PATH)
    lines = content.splitlines()
    if len(lines) != 1 or not VERSION_PATTERN.fullmatch(lines[0]):
        raise ReleasePreflightError(
            "VERSION must contain exactly one vMAJOR.MINOR.PATCH line"
        )
    return lines[0]


def _base_version(root: Path, base: str) -> str | None:
    try:
        content = _git("show", f"{base}:VERSION", root=root)
    except ReleasePreflightError:
        return None
    lines = content.splitlines()
    if len(lines) == 1 and VERSION_PATTERN.fullmatch(lines[0]):
        return lines[0]
    return None


def _matching_changelog_heading(changelog: str, version: str) -> str:
    lines = changelog.splitlines()
    matches: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = RELEASE_HEADING_PATTERN.fullmatch(line)
        if match and match.group("version") == version:
            try:
                date.fromisoformat(match.group("released"))
            except ValueError as error:
                raise ReleasePreflightError(
                    f"CHANGELOG.md release heading for {version} has an invalid date"
                ) from error
            matches.append((index, line))

    if not matches:
        raise ReleasePreflightError(
            f"CHANGELOG.md must contain a dated release heading matching VERSION ({version})"
        )
    if len(matches) > 1:
        raise ReleasePreflightError(
            f"CHANGELOG.md contains multiple release headings for {version}"
        )

    start, heading = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    if not any(line.strip() for line in lines[start + 1 : end]):
        raise ReleasePreflightError(
            f"CHANGELOG.md release section for {version} must not be empty"
        )
    return heading


def _validate_publication_workflow(root: Path, relative: Path) -> Path:
    workflow = root / relative
    try:
        content = workflow.read_text(encoding="utf-8")
    except OSError as error:
        raise ReleasePreflightError(
            f"published-release workflow is missing or unreadable: {relative}"
        ) from error

    missing = [marker for marker in PUBLICATION_WORKFLOW_MARKERS if marker not in content]
    if missing:
        raise ReleasePreflightError(
            "published-release workflow is incomplete; missing: " + ", ".join(missing)
        )
    publisher = root / PUBLICATION_PUBLISHER_PATH
    if not publisher.is_file():
        raise ReleasePreflightError(
            f"published-release workflow references missing publisher: {PUBLICATION_PUBLISHER_PATH}"
        )
    return relative


def check_preflight(
    root: Path,
    paths: Iterable[str | Path],
    *,
    base_version: str | None = None,
    publication_workflow: Path = PUBLICATION_WORKFLOW_PATH,
) -> PreflightReport:
    """Validate release requirements for *paths* in the candidate tree.

    ``base_version`` is the version from the last published tree.  When it is
    supplied, a source/derived diff must bump ``VERSION`` rather than merely
    repeating the old release number.
    """

    root = root.resolve()
    classification = classify_paths(paths)
    if not classification.release_required:
        return PreflightReport(classification, None, None, None)

    changed = set(classification.paths)
    missing = [
        path
        for path in (str(VERSION_PATH), str(CHANGELOG_PATH))
        if path not in changed
    ]
    if missing:
        raise ReleasePreflightError(
            "release-required changes must update " + ", ".join(missing)
        )

    version = _read_version(root)
    if base_version is not None and version == base_version:
        raise ReleasePreflightError(
            f"VERSION remains {version}; source or derived-asset changes require a new release version"
        )

    heading = _matching_changelog_heading(_read_candidate(root, CHANGELOG_PATH), version)
    workflow = _validate_publication_workflow(root, publication_workflow)
    return PreflightReport(classification, version, heading, workflow)


def _format_paths(label: str, paths: Sequence[str]) -> str:
    if not paths:
        return f"{label}=none"
    return f"{label}=" + ",".join(paths)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Classify a Git diff and enforce release metadata for source or derived assets."
    )
    parser.add_argument(
        "--base",
        required=True,
        help="last published commit or tag to compare with the candidate",
    )
    parser.add_argument("--head", default="HEAD", help="candidate commit (default: HEAD)")
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root (default: the brand-kit checkout)",
    )
    parser.add_argument(
        "--publication-workflow",
        type=Path,
        default=PUBLICATION_WORKFLOW_PATH,
        help=(
            "repository-relative Forgejo publication workflow runbook "
            f"(default: {PUBLICATION_WORKFLOW_PATH})"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    try:
        paths = changed_paths(root, args.base, args.head)
        base_version = _base_version(root, args.base)
        report = check_preflight(
            root,
            paths,
            base_version=base_version,
            publication_workflow=args.publication_workflow,
        )
    except ReleasePreflightError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    classification = report.classification
    print(f"PASS release-preflight: {classification.kind}")
    print(_format_paths("source", classification.source))
    print(_format_paths("derived", classification.derived))
    print(_format_paths("documentation", classification.documentation))
    print(_format_paths("metadata", classification.metadata))
    print(_format_paths("other", classification.other))
    if report.version is not None:
        print(f"PASS VERSION={report.version}")
        print(f"PASS changelog={report.changelog_heading}")
        print(f"PASS publication-workflow={report.publication_workflow}")
    else:
        print("PASS no release metadata required; Unreleased is permitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
