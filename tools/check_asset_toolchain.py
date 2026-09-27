#!/usr/bin/env python3
"""Check the documented asset pins against the brand-kit CI manifest.

The WorkflowTemplate lives in declarative-config rather than this repository,
so this checker deliberately takes its path as an argument.  It only uses the
Python standard library: local checks must work before the pinned virtualenv
has been installed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import sys


class ToolchainParityError(ValueError):
    """Raised when the documentation and WorkflowTemplate disagree."""


@dataclass(frozen=True)
class InstallSpec:
    kind: str
    runner: tuple[str, ...]
    package: str
    version: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class Pin:
    tool: str
    version: str
    install: InstallSpec


_VERSION = re.compile(r"(?<![\w.])\d+\.\d+\.\d+(?![\w.])")
_PACKAGE_VERSION = re.compile(r"^([A-Za-z0-9_.-]+)(?:==|@)([0-9]+\.[0-9]+\.[0-9]+[^\s]*)$")


def _table_cells(line: str) -> list[str] | None:
    if not line.lstrip().startswith("|"):
        return None
    body = line.strip()[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in body.split("|")]


def _unquote_cell(value: str) -> str:
    value = value.strip()
    if value.startswith("`") and value.endswith("`"):
        return value[1:-1]
    return value


def _parse_install_command(command: str, *, context: str) -> InstallSpec:
    try:
        tokens = shlex.split(command)
    except ValueError as error:
        raise ToolchainParityError(
            f"{context}: invalid shell syntax in install command: {error}"
        ) from error

    if len(tokens) >= 3 and tokens[0] == "cargo" and tokens[1] == "install":
        package_tokens = [token for token in tokens[2:] if not token.startswith("-")]
        if len(package_tokens) != 1:
            raise ToolchainParityError(
                f"{context}: Cargo install command must name exactly one tool"
            )
        match = _PACKAGE_VERSION.fullmatch(package_tokens[0])
        if not match or "@" not in package_tokens[0]:
            raise ToolchainParityError(
                f"{context}: expected cargo install NAME@VERSION"
            )
        options = tuple(token for token in tokens[2:] if token != package_tokens[0])
        return InstallSpec(
            kind="cargo",
            runner=("cargo",),
            package=match.group(1),
            version=match.group(2),
            options=options,
        )

    try:
        install_index = tokens.index("install")
    except ValueError:
        install_index = -1

    if install_index > 0 and (
        tokens[install_index - 1] == "pip"
        or tokens[install_index - 1] == "pip3"
        or (
            install_index >= 3
            and tokens[install_index - 3].endswith("/python")
            and tokens[install_index - 2 : install_index] == ["-m", "pip"]
        )
    ):
        runner = tuple(tokens[:install_index])
        package_tokens = [
            token
            for token in tokens[install_index + 1 :]
            if not token.startswith("-")
        ]
        if len(package_tokens) != 1:
            raise ToolchainParityError(
                f"{context}: pip install command must name exactly one tool"
            )
        match = _PACKAGE_VERSION.fullmatch(package_tokens[0])
        if not match or "==" not in package_tokens[0]:
            raise ToolchainParityError(
                f"{context}: expected pip install NAME==VERSION"
            )
        options = tuple(
            token
            for token in tokens[install_index + 1 :]
            if token != package_tokens[0]
        )
        return InstallSpec(
            kind="pip",
            runner=runner,
            package=match.group(1),
            version=match.group(2),
            options=options,
        )

    raise ToolchainParityError(
        f"{context}: unsupported install command {command!r}"
    )


def parse_pin_table(document: str) -> list[Pin]:
    """Parse the canonical four-tool Markdown pin table."""

    lines = document.splitlines()
    try:
        heading = next(
            index
            for index, line in enumerate(lines)
            if line.strip() == "## Pinned versions (canonical)"
        )
    except StopIteration as error:
        raise ToolchainParityError(
            "asset-toolchain.md: missing canonical pin-table heading"
        ) from error

    header_index = None
    headers: list[str] = []
    for index in range(heading + 1, len(lines)):
        cells = _table_cells(lines[index])
        if cells and {cell.lower() for cell in cells} >= {
            "tool",
            "pin",
            "install command",
        }:
            header_index = index
            headers = [cell.lower() for cell in cells]
            break
    if header_index is None:
        raise ToolchainParityError(
            "asset-toolchain.md: canonical table must have Tool, Pin, and "
            "Install command columns"
        )

    separator = _table_cells(lines[header_index + 1]) if header_index + 1 < len(lines) else None
    if not separator or not all(set(cell.replace(":", "").replace("-", "").strip()) == set() for cell in separator):
        raise ToolchainParityError(
            "asset-toolchain.md: canonical table is missing its separator row"
        )

    positions = {
        name: headers.index(name)
        for name in ("tool", "pin", "install command")
    }
    pins: list[Pin] = []
    for index in range(header_index + 2, len(lines)):
        cells = _table_cells(lines[index])
        if cells is None:
            break
        if len(cells) < len(headers):
            raise ToolchainParityError(
                f"asset-toolchain.md:{index + 1}: incomplete pin-table row"
            )
        tool = _unquote_cell(cells[positions["tool"]])
        pin_text = _unquote_cell(cells[positions["pin"]])
        command = _unquote_cell(cells[positions["install command"]])
        if not tool or not pin_text or not command:
            raise ToolchainParityError(
                f"asset-toolchain.md:{index + 1}: empty pin-table field"
            )
        version_match = _VERSION.search(pin_text)
        if not version_match:
            raise ToolchainParityError(
                f"asset-toolchain.md:{index + 1}: {tool} has no exact version pin"
            )
        install = _parse_install_command(
            command, context=f"asset-toolchain.md:{index + 1} ({tool})"
        )
        if install.package.casefold() != tool.casefold():
            raise ToolchainParityError(
                f"asset-toolchain.md:{index + 1}: tool {tool!r} names "
                f"{install.package!r} in its install command"
            )
        if install.version != version_match.group(0):
            raise ToolchainParityError(
                f"asset-toolchain.md:{index + 1}: {tool} pin {version_match.group(0)} "
                f"does not match install command version {install.version}"
            )
        if tool.casefold() == "pillow" and "--only-binary=:all:" not in install.options:
            raise ToolchainParityError(
                "asset-toolchain.md: Pillow must require the PyPI wheel with "
                "--only-binary=:all:"
            )
        pins.append(Pin(tool=tool, version=version_match.group(0), install=install))

    if not pins:
        raise ToolchainParityError("asset-toolchain.md: canonical pin table is empty")
    package_keys = [(pin.install.kind, pin.install.package.casefold()) for pin in pins]
    if len(set(package_keys)) != len(package_keys):
        raise ToolchainParityError(
            "asset-toolchain.md: canonical pin table names a tool more than once"
        )
    return pins


def _workflow_shell_block(manifest: str) -> list[str]:
    lines = manifest.splitlines()
    if not re.search(r"^kind:\s*WorkflowTemplate\s*$", manifest, re.MULTILINE):
        raise ToolchainParityError(
            "WorkflowTemplate manifest: kind is not WorkflowTemplate"
        )
    if not re.search(r"^\s*name:\s*brand-kit-ci\s*$", manifest, re.MULTILINE):
        raise ToolchainParityError(
            "WorkflowTemplate manifest: metadata.name is not brand-kit-ci"
        )

    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)-\s*\|\s*$", line)
        if not match:
            continue
        marker_indent = len(match.group(1))
        block: list[str] = []
        for candidate in lines[index + 1 :]:
            if candidate.strip() and len(candidate) - len(candidate.lstrip()) <= marker_indent:
                break
            content_indent = min(len(candidate), marker_indent + 2)
            block.append(candidate[content_indent:])
        if block:
            return block
    raise ToolchainParityError(
        "WorkflowTemplate manifest: missing literal shell command block"
    )


def _logical_shell_commands(lines: list[str]) -> list[str]:
    commands: list[str] = []
    current = ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        continuation = stripped.endswith("\\")
        if continuation:
            stripped = stripped[:-1].rstrip()
        current = f"{current} {stripped}".strip()
        if not continuation:
            commands.append(current)
            current = ""
    if current:
        commands.append(current)
    return commands


def _parse_manifest_installs(manifest: str) -> list[InstallSpec]:
    installs: list[InstallSpec] = []
    for command in _logical_shell_commands(_workflow_shell_block(manifest)):
        # Most shell commands are not installs.  Only parse commands whose
        # first words clearly identify one of the supported installers.
        tokens = shlex.split(command)
        if not tokens:
            continue
        if tokens[0] == "cargo" and len(tokens) > 1 and tokens[1] == "install":
            package_tokens = [token for token in tokens[2:] if not token.startswith("-")]
            options = tuple(token for token in tokens[2:] if token not in package_tokens)
            if not package_tokens:
                raise ToolchainParityError(
                    "WorkflowTemplate: cargo install names no tool"
                )
            for package_token in package_tokens:
                match = _PACKAGE_VERSION.fullmatch(package_token)
                if not match or "@" not in package_token:
                    raise ToolchainParityError(
                        "WorkflowTemplate: expected cargo install NAME@VERSION"
                    )
                installs.append(
                    InstallSpec(
                        kind="cargo",
                        runner=("cargo",),
                        package=match.group(1),
                        version=match.group(2),
                        options=options,
                    )
                )
            continue

        if tokens[0] in {"pip", "pip3"}:
            install_index = 1
            runner = tuple(tokens[:install_index])
        elif (
            len(tokens) >= 4
            and tokens[0].endswith("/python")
            and tokens[1:3] == ["-m", "pip"]
            and tokens[3] == "install"
        ):
            install_index = 3
            runner = tuple(tokens[:install_index])
        else:
            continue

        package_tokens = [
            token for token in tokens[install_index + 1 :] if not token.startswith("-")
        ]
        options = tuple(
            token
            for token in tokens[install_index + 1 :]
            if token not in package_tokens
        )
        if not package_tokens:
            raise ToolchainParityError(
                "WorkflowTemplate: pip install names no tool"
            )
        for package_token in package_tokens:
            match = _PACKAGE_VERSION.fullmatch(package_token)
            if not match or "==" not in package_token:
                raise ToolchainParityError(
                    "WorkflowTemplate: expected pip install NAME==VERSION"
                )
            installs.append(
                InstallSpec(
                    kind="pip",
                    runner=runner,
                    package=match.group(1),
                    version=match.group(2),
                    options=options,
                )
            )
    return installs


def check_toolchain(document_path: Path, workflow_path: Path) -> None:
    """Raise ToolchainParityError unless both install contracts are identical."""

    pins = parse_pin_table(document_path.read_text(encoding="utf-8"))
    actual = _parse_manifest_installs(workflow_path.read_text(encoding="utf-8"))
    expected_by_key = {
        (pin.install.kind, pin.install.package.casefold()): pin.install for pin in pins
    }

    actual_by_key: dict[tuple[str, str], list[InstallSpec]] = {}
    for install in actual:
        key = (install.kind, install.package.casefold())
        actual_by_key.setdefault(key, []).append(install)

    expected_keys = set(expected_by_key)
    actual_keys = set(actual_by_key)
    missing = sorted(expected_keys - actual_keys)
    unexpected = sorted(actual_keys - expected_keys)
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing from WorkflowTemplate: " + ", ".join("/".join(key) for key in missing))
        if unexpected:
            details.append("not named by pin table: " + ", ".join("/".join(key) for key in unexpected))
        raise ToolchainParityError("tool set mismatch; " + "; ".join(details))

    for key, expected in expected_by_key.items():
        matches = actual_by_key[key]
        if len(matches) != 1:
            raise ToolchainParityError(
                f"{expected.package}: expected one install command, found {len(matches)}"
            )
        installed = matches[0]
        if installed.version != expected.version:
            raise ToolchainParityError(
                f"{expected.package}: documented version {expected.version} "
                f"does not match WorkflowTemplate version {installed.version}"
            )
        if installed.runner != expected.runner:
            raise ToolchainParityError(
                f"{expected.package}: documented installer {' '.join(expected.runner)!r} "
                f"does not match WorkflowTemplate installer {' '.join(installed.runner)!r}"
            )
        if installed.options != expected.options:
            raise ToolchainParityError(
                f"{expected.package}: documented install options {expected.options!r} "
                f"do not match WorkflowTemplate options {installed.options!r}"
            )
        if expected.package.casefold() == "pillow" and "--only-binary=:all:" not in installed.options:
            raise ToolchainParityError(
                "Pillow: WorkflowTemplate must install the PyPI wheel with "
                "--only-binary=:all:"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check asset-toolchain.md pins against brand-kit-ci"
    )
    parser.add_argument(
        "--document",
        type=Path,
        default=Path("docs/notes/asset-toolchain.md"),
        help="path to the canonical asset-toolchain.md (default: %(default)s)",
    )
    parser.add_argument(
        "--workflow-template",
        type=Path,
        required=True,
        help="path to the declarative-config brand-kit-ci WorkflowTemplate",
    )
    args = parser.parse_args(argv)
    try:
        check_toolchain(args.document, args.workflow_template)
    except (OSError, ToolchainParityError) as error:
        print(f"asset toolchain parity check failed: {error}", file=sys.stderr)
        return 1
    print("asset toolchain parity check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
