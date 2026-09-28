#!/usr/bin/env python3
"""Smoke-check local documentation links, file references, and toolchain usage."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

# Support both `python3 tools/check_documentation.py` and module/test imports.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.check_asset_toolchain import ToolchainParityError, parse_pin_table


_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_SCHEMA = re.compile(r"(?<![\w./-])([\w./-]+\.schema\.json)\b")
_REPO_PATH = re.compile(
    r"(?<![\w./-])(?:automation|avatars|banners|docs|favicon|logo|"
    r"release-evidence|source|tests|tools)/[\w./@+-]+\."
    r"(?:json|md|py|sh|ya?ml|toml|txt|png|svg|ico|jpe?g|sha256|lock)\b"
)
_ENTRY_POINT = re.compile(r"\btools/[A-Za-z0-9_./-]+\.(?:py|sh)\b")
_GENERATORS = ("build_assets.py", "trace_logo.py", "verify_assets.py")
_PINNED_PYTHON_INSTALL = (
    ".venv/bin/python -m pip install --only-binary=:all: "
    "Pillow==12.1.1 pytest==9.0.2"
)
# This file is intentionally absent; the README documents how to recover it
# from history if that old, unused alternate composition is needed again.
_HISTORICAL_REFERENCES = {"source/hero-alt.png"}


def _markdown_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.md")
        if not any(part.startswith(".") for part in path.relative_to(root).parts)
    )


def _local_link_target(document: Path, target: str, root: Path) -> Path | None:
    target = target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None
    path = unquote(parsed.path)
    if not path:
        return document
    resolved = (document.parent / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        # References into a sibling checkout are intentionally outside this repo.
        return None
    return resolved


def check_cross_references(root: Path) -> list[str]:
    """Check local Markdown links, repository file refs, scripts, and schemas."""

    root = root.resolve()
    errors: list[str] = []
    markdown = _markdown_files(root)
    for document in markdown:
        content = document.read_text(encoding="utf-8")
        relative_document = document.relative_to(root)

        for match in _LINK.finditer(content):
            raw_target = match.group(1).split(maxsplit=1)[0]
            target = _local_link_target(document, raw_target, root)
            if target is not None and not target.exists():
                errors.append(
                    f"{relative_document}: broken local link {raw_target!r}"
                )

        references = set(_REPO_PATH.findall(content))
        references.update(_SCHEMA.findall(content))
        references.update(_ENTRY_POINT.findall(content))
        for reference in sorted(references):
            if reference in _HISTORICAL_REFERENCES:
                continue
            if "*" in reference or "<" in reference or ">" in reference:
                continue
            candidate = Path(reference)
            if not candidate.is_absolute() and reference.startswith(("./", "../")):
                candidate = (document.parent / candidate).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError:
                    # This is a deliberate path into a sibling checkout.
                    continue
            elif not candidate.is_absolute():
                candidate = root / candidate
            if not candidate.exists():
                errors.append(
                    f"{relative_document}: referenced repository path is missing: "
                    f"{reference}"
                )
    return errors


def check_toolchain_documentation(root: Path) -> list[str]:
    """Keep documented asset commands aligned with their pinned environment."""

    root = root.resolve()
    readme_path = root / "README.md"
    toolchain_path = root / "docs/notes/asset-toolchain.md"
    missing = [
        str(path.relative_to(root))
        for path in (readme_path, toolchain_path)
        if not path.is_file()
    ]
    if missing:
        return [f"required toolchain documentation is missing: {', '.join(missing)}"]

    readme = readme_path.read_text(encoding="utf-8")
    toolchain = toolchain_path.read_text(encoding="utf-8")
    errors: list[str] = []
    try:
        pins = parse_pin_table(toolchain)
    except ToolchainParityError as error:
        return [f"docs/notes/asset-toolchain.md: {error}"]

    for pin in pins:
        if pin.install.kind == "pip" and pin.install.runner != (
            ".venv/bin/python",
            "-m",
            "pip",
        ):
            errors.append(
                f"asset-toolchain pin {pin.tool} must install through "
                ".venv/bin/python -m pip"
            )
    for doc_name, content in (("README.md", readme), ("asset-toolchain.md", toolchain)):
        if _PINNED_PYTHON_INSTALL not in content:
            errors.append(
                f"{doc_name}: missing pinned .venv pip install command "
                "for Pillow and pytest"
            )
        for generator in _GENERATORS:
            command = f".venv/bin/python tools/{generator}"
            if command not in content:
                errors.append(f"{doc_name}: missing required command {command}")
            unpinned = re.compile(
                rf"(?<![\w./])python(?:3)?\s+tools/{re.escape(generator)}\b"
            )
            if unpinned.search(content):
                errors.append(
                    f"{doc_name}: tools/{generator} must use .venv/bin/python"
                )

    if ".venv/bin/python -m pytest -q" not in readme:
        errors.append("README.md: full pytest command must use .venv/bin/python")
    return errors


def check_repository(root: Path) -> list[str]:
    """Return all documentation smoke-check failures for a repository."""

    return check_cross_references(root) + check_toolchain_documentation(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to this script's parent repository)",
    )
    args = parser.parse_args(argv)
    errors = check_repository(args.root)
    if errors:
        print("Documentation smoke check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    documents = len(_markdown_files(args.root.resolve()))
    print(f"Documentation smoke check passed ({documents} Markdown files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
