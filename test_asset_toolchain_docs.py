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
    documented_commands = readme + toolchain + tools

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


def test_trace_logo_uses_pinned_cargo_vtracer_cli():
    toolchain = (ROOT / "docs/notes/asset-toolchain.md").read_text()
    trace_logo = (ROOT / "tools/trace_logo.py").read_text()

    assert "cargo install vtracer@0.6.5" in toolchain
    assert "cargo install vtracer@0.6.5" in trace_logo
    assert "PyPI `vtracer`" in toolchain
    assert "PyPI `vtracer`" in trace_logo
