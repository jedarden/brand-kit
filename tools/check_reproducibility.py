#!/usr/bin/env python3
"""Prove that two clean asset-toolchain installs produce identical bytes.

The ordinary CI regeneration check proves that one installed toolchain can
recreate the committed tree.  This check is deliberately stronger: it creates
two independent source trees, empty Cargo homes, fresh virtual environments,
and fresh wheel installs.  It exercises both the normal renderer/encoder path
and the explicit vtracer trace path, then compares the complete generated
asset set, the trace output, the Python environment, and every transitive
Cargo lock graph.

Run from the repository root with the system Python, before or after the
normal CI toolchain has been installed::

    python3 tools/check_reproducibility.py

The script uses ``--locked`` for Cargo installs.  A changed transitive graph
therefore fails at install time, while a changed graph between the two clean
runs fails during the lock-graph comparison.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent.parent
GENERATED_DIRECTORIES = ("avatars", "banners", "favicon", "logo")
GENERATED_FILES = ("palette.json", "platform-assets.json")
TRACE_FILES = ("source/logo.svg", "source/logo.svg.sha256")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.check_asset_toolchain import parse_pin_table


class ReproducibilityError(RuntimeError):
    """Raised when the clean-toolchain evidence is incomplete or diverges."""


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=True,
            text=True,
            capture_output=capture_output,
        )
    except FileNotFoundError as error:
        raise ReproducibilityError(
            f"required executable {command[0]!r} was not found"
        ) from error
    except subprocess.CalledProcessError as error:
        details = (error.stderr or error.stdout or "").strip()
        suffix = f": {details[-1200:]}" if details else ""
        raise ReproducibilityError(
            f"command failed with exit {error.returncode}: {' '.join(command)}{suffix}"
        ) from error
    return result


def _toolchain_pins() -> dict[str, tuple[str, str]]:
    document = (ROOT / "docs/notes/asset-toolchain.md").read_text(encoding="utf-8")
    pins = parse_pin_table(document)
    result = {
        pin.install.package.casefold(): (pin.version, pin.install.kind)
        for pin in pins
    }
    required = {"resvg", "vtracer", "pillow"}
    missing = sorted(required - result.keys())
    if missing:
        raise ReproducibilityError(
            "asset-toolchain.md is missing reproducibility pins: "
            + ", ".join(missing)
        )
    if result["resvg"][1] != "cargo" or result["vtracer"][1] != "cargo":
        raise ReproducibilityError("resvg and vtracer must use Cargo pins")
    if result["pillow"][1] != "pip":
        raise ReproducibilityError("Pillow must use the pinned PyPI wheel")
    return result


def _archive_source(destination: Path) -> None:
    """Create a committed source snapshot, also when running from an archive."""

    git_directory = ROOT / ".git"
    if git_directory.exists():
        archive = destination.with_suffix(".tar")
        with archive.open("wb") as output:
            subprocess.run(
                ["git", "archive", "--format=tar", "HEAD"],
                cwd=ROOT,
                check=True,
                stdout=output,
            )
        destination.mkdir(parents=True)
        with tarfile.open(archive, "r") as source:
            source.extractall(destination)
        archive.unlink()
        return

    shutil.copytree(
        ROOT,
        destination,
        ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache"),
    )


def _generated_paths(repo: Path) -> tuple[Path, ...]:
    paths: set[Path] = {Path(value) for value in GENERATED_FILES}
    for directory in GENERATED_DIRECTORIES:
        root = repo / directory
        if root.exists():
            paths.update(
                path.relative_to(repo)
                for path in root.rglob("*")
                if path.is_file()
            )
    paths.update(Path(value) for value in TRACE_FILES)
    return tuple(sorted(paths))


def _fingerprint(repo: Path, paths: tuple[Path, ...]) -> dict[str, str]:
    fingerprint: dict[str, str] = {}
    for relative in paths:
        path = repo / relative
        if not path.is_file():
            raise ReproducibilityError(f"expected generated file is missing: {relative}")
        fingerprint[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return fingerprint


def _compare_fingerprints(
    expected: dict[str, str], actual: dict[str, str], *, label: str
) -> None:
    if expected == actual:
        return
    paths = sorted(set(expected) | set(actual))
    differences = [
        path
        for path in paths
        if expected.get(path) != actual.get(path)
    ]
    raise ReproducibilityError(
        f"{label} differs for {len(differences)} file(s): "
        + ", ".join(differences[:12])
        + (" ..." if len(differences) > 12 else "")
    )


def _cargo_target_root(work: Path) -> Path:
    """Keep Rust output in the shared build volume when the wrapper requires it."""

    requested = os.environ.get("BRAND_KIT_REPRO_TARGET_ROOT")
    if requested:
        target_root = Path(requested)
    else:
        build_root = Path("/build") / ROOT.name.lower()
        target_root = build_root if build_root.parent.exists() and os.access(build_root.parent, os.W_OK) else work
    target_root.mkdir(parents=True, exist_ok=True)
    return target_root / f"reproducibility-{uuid.uuid4().hex}"


def _install_run(
    run_root: Path,
    repo: Path,
    pins: dict[str, tuple[str, str]],
    *,
    target_root: Path,
) -> tuple[Path, dict[str, str], list[str]]:
    cargo_home = run_root / "cargo-home"
    cargo_root = run_root / "cargo-root"
    cargo_target = target_root / "cargo-target"
    cargo_home.mkdir()
    cargo_root.mkdir()
    cargo_target.mkdir(parents=True)
    env = os.environ.copy()
    env.update(
        {
            "CARGO_HOME": str(cargo_home),
            "LC_ALL": "C",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TZ": "UTC",
        }
    )

    for package in ("resvg", "vtracer"):
        version, kind = pins[package]
        if kind != "cargo":
            raise ReproducibilityError(f"{package} is not a Cargo pin")
        _run(
            [
                "cargo",
                "install",
                "--locked",
                "--root",
                str(cargo_root),
                "--target-dir",
                str(cargo_target),
                f"{package}@{version}",
            ],
            cwd=repo,
            env=env,
        )

    python = run_root / "venv" / "bin" / "python"
    _run([sys.executable, "-m", "venv", str(run_root / "venv")], cwd=repo, env=env)
    pillow_version, pillow_kind = pins["pillow"]
    if pillow_kind != "pip":
        raise ReproducibilityError("Pillow is not a pip pin")
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--only-binary=:all:",
            f"Pillow=={pillow_version}",
        ],
        cwd=repo,
        env=env,
    )

    tool_env = env.copy()
    tool_env["PATH"] = f"{cargo_root / 'bin'}{os.pathsep}{tool_env['PATH']}"
    resvg_version = _run(
        [str(cargo_root / "bin" / "resvg"), "--version"],
        cwd=repo,
        env=tool_env,
        capture_output=True,
    ).stdout.strip()
    vtracer_version = _run(
        [str(cargo_root / "bin" / "vtracer"), "--version"],
        cwd=repo,
        env=tool_env,
        capture_output=True,
    ).stdout.strip()
    if pins["resvg"][0] not in resvg_version:
        raise ReproducibilityError(
            f"fresh resvg reports {resvg_version!r}, expected {pins['resvg'][0]}"
        )
    if pins["vtracer"][0] not in vtracer_version:
        raise ReproducibilityError(
            f"fresh vtracer reports {vtracer_version!r}, expected {pins['vtracer'][0]}"
        )
    if pillow_version not in _run(
        [str(python), "-c", "import PIL; print(PIL.__version__)"],
        cwd=repo,
        env=tool_env,
        capture_output=True,
    ).stdout:
        raise ReproducibilityError("fresh virtualenv did not import the pinned Pillow")

    locks: dict[str, str] = {}
    for package in ("resvg", "vtracer"):
        version = pins[package][0]
        matches = sorted(
            cargo_home.glob(f"registry/src/*/{package}-{version}/Cargo.lock")
        )
        if len(matches) != 1:
            raise ReproducibilityError(
                f"expected one locked dependency graph for {package}, found {len(matches)}"
            )
        locks[package] = hashlib.sha256(matches[0].read_bytes()).hexdigest()

    freeze = _run(
        [str(python), "-m", "pip", "freeze", "--all"],
        cwd=repo,
        env=tool_env,
        capture_output=True,
    ).stdout.splitlines()
    fingerprint = {
        "cargo-locks": "\n".join(f"{key}={locks[key]}" for key in sorted(locks)),
        "pip-freeze": "\n".join(sorted(line.strip() for line in freeze if line.strip())),
        "rustc": _run(
            ["rustc", "--version", "--verbose"],
            cwd=repo,
            env=tool_env,
            capture_output=True,
        ).stdout.strip(),
        "cargo": _run(
            ["cargo", "--version"], cwd=repo, env=tool_env, capture_output=True
        ).stdout.strip(),
        "resvg": resvg_version,
        "vtracer": vtracer_version,
    }
    return python, fingerprint, [str(cargo_root / "bin"), tool_env["PATH"]]


def _run_clean_build(
    run_root: Path,
    baseline: Path,
    generated_paths: tuple[Path, ...],
    pins: dict[str, tuple[str, str]],
    *,
    target_root: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    repo = run_root / "repo"
    shutil.copytree(baseline, repo)
    baseline_fingerprint = _fingerprint(repo, generated_paths)
    python, environment, path_details = _install_run(
        run_root, repo, pins, target_root=target_root
    )
    tool_env = os.environ.copy()
    tool_env.update(
        {
            "PATH": path_details[1],
            "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TZ": "UTC",
        }
    )

    _run([str(python), "tools/verify_assets.py"], cwd=repo, env=tool_env)
    _run([str(python), "tools/trace_logo.py"], cwd=repo, env=tool_env)
    _compare_fingerprints(
        _fingerprint(baseline, tuple(Path(value) for value in TRACE_FILES)),
        _fingerprint(repo, tuple(Path(value) for value in TRACE_FILES)),
        label="vtracer output",
    )
    _run([str(python), "tools/build_assets.py"], cwd=repo, env=tool_env)
    _run([str(python), "tools/verify_assets.py"], cwd=repo, env=tool_env)
    actual = _fingerprint(repo, generated_paths)
    _compare_fingerprints(baseline_fingerprint, actual, label="clean regeneration")
    print(
        f"clean run {run_root.name}: {len(actual)} generated files match committed bytes"
    )
    return actual, environment


def run() -> int:
    pins = _toolchain_pins()
    with tempfile.TemporaryDirectory(prefix="brand-kit-repro-") as temporary:
        work = Path(temporary)
        baseline = work / "baseline"
        _archive_source(baseline)
        generated_paths = _generated_paths(baseline)
        target_root = _cargo_target_root(work)
        try:
            first, first_environment = _run_clean_build(
                work / "run-1",
                baseline,
                generated_paths,
                pins,
                target_root=target_root / "run-1",
            )
            second, second_environment = _run_clean_build(
                work / "run-2",
                baseline,
                generated_paths,
                pins,
                target_root=target_root / "run-2",
            )
        finally:
            shutil.rmtree(target_root, ignore_errors=True)

    _compare_fingerprints(first, second, label="independent clean regeneration")
    if first_environment != second_environment:
        raise ReproducibilityError(
            "clean environments differ, including transitive dependency or runtime pins"
        )
    print(
        "clean-toolchain reproducibility passed: "
        f"{len(first)} generated files, Pillow encoding, vtracer trace, "
        "and locked transitive dependency graphs agree"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    try:
        return run()
    except (OSError, ReproducibilityError) as error:
        print(f"reproducibility check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
