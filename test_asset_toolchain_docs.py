from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_regeneration_commands_use_pinned_venv():
    readme = (ROOT / "README.md").read_text()
    toolchain = (ROOT / "docs/notes/asset-toolchain.md").read_text()
    tools = "\n".join(
        (ROOT / path).read_text()
        for path in (
            "tools/build_assets.py",
            "tools/trace_logo.py",
            "tools/verify_assets.py",
        )
    )
    local_readme = readme.split("## CI regression gate\n", 1)[0]
    documented_commands = local_readme + toolchain + tools

    assert ".venv/bin/python tools/trace_logo.py" in readme
    assert ".venv/bin/python tools/build_assets.py" in readme
    assert "python3 tools/trace_logo.py" not in documented_commands
    assert "python3 tools/build_assets.py" not in documented_commands
    assert (
        ".venv/bin/python -m pip install --only-binary=:all: Pillow==12.1.1"
        in readme
    )
    assert (
        ".venv/bin/python -m pip install --only-binary=:all: Pillow==12.1.1"
        in toolchain
    )
    assert "docs/notes/asset-toolchain.md" in readme


def test_ci_regression_acceptance_sequence_is_documented():
    readme = (ROOT / "README.md").read_text()
    section = readme.split("## CI regression gate\n", 1)[1].split("\n## ", 1)[0]
    commands = (
        "cargo install resvg@0.47.0 vtracer@0.6.5",
        "pip3 install --break-system-packages Pillow==12.1.1 pytest==9.0.2",
        "python3 tools/verify_assets.py",
        "python3 -m pytest -q",
        "python3 tools/build_assets.py",
        "git diff --exit-code --quiet",
    )

    positions = [section.index(command) for command in commands]
    assert positions == sorted(positions)
    assert "unexpected generated files" in section
    assert "Argo phase of `Succeeded`" in section
    assert "not CI acceptance evidence" in section


def test_trace_logo_uses_pinned_cargo_vtracer_cli():
    toolchain = (ROOT / "docs/notes/asset-toolchain.md").read_text()
    trace_logo = (ROOT / "tools/trace_logo.py").read_text()

    assert "cargo install vtracer@0.6.5" in toolchain
    assert "cargo install vtracer@0.6.5" in trace_logo
    assert "PyPI `vtracer`" in toolchain
    assert "PyPI `vtracer`" in trace_logo


def test_logo_source_and_regeneration_contract_is_consistent():
    documents = [
        (ROOT / path).read_text()
        for path in (
            "README.md",
            "docs/plan/plan.md",
            "docs/notes/asset-toolchain.md",
        )
    ]
    trace = """```bash
.venv/bin/python tools/trace_logo.py
```"""
    build_verify = """```bash
.venv/bin/python tools/build_assets.py
.venv/bin/python tools/verify_assets.py
```"""

    for document in documents:
        normalized = "\n".join(line.strip() for line in document.splitlines())
        assert (
            "`source/logo.svg` is authoritative" in document
            or "`source/logo.svg` is the authoritative" in document
        )
        assert "`source/logo.svg.sha256`" in document
        assert ".venv/bin/python tools/trace_logo.py --force" in document
        assert trace in normalized
        assert build_verify in normalized
