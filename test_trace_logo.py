import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from tools import trace_logo


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def configure_trace(monkeypatch, tmp_path, svg=b"<svg>current</svg>\n"):
    src = tmp_path / "source"
    src.mkdir()
    logo_png = src / "logo.png"
    logo_svg = src / "logo.svg"
    logo_svg_sha256 = src / "logo.svg.sha256"
    Image.new("RGB", (2, 2), "#DC3127").save(logo_png)
    logo_svg.write_bytes(svg)
    logo_svg_sha256.write_text(f"{sha256(svg)}\n", encoding="utf-8")
    monkeypatch.setattr(trace_logo, "SRC", src)
    monkeypatch.setattr(trace_logo, "LOGO_PNG", logo_png)
    monkeypatch.setattr(trace_logo, "LOGO_SVG", logo_svg)
    monkeypatch.setattr(trace_logo, "LOGO_SVG_SHA256", logo_svg_sha256)
    return logo_svg, logo_svg_sha256


def write_traced_svg(content):
    def run(command, check):
        assert check
        output = Path(command[command.index("--output") + 1])
        output.write_bytes(content)

    return run


def test_trace_refuses_to_overwrite_modified_svg(monkeypatch, tmp_path):
    current = b"<svg>hand edited</svg>\n"
    logo_svg, logo_svg_sha256 = configure_trace(monkeypatch, tmp_path, current)
    logo_svg_sha256.write_text(f"{sha256(b'<svg>traced</svg>')}\n", encoding="utf-8")
    monkeypatch.setattr(
        trace_logo.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("vtracer must not run"),
    )

    with pytest.raises(SystemExit, match="--force"):
        trace_logo.main()

    assert logo_svg.read_bytes() == current


def test_trace_rechecks_svg_before_replacement(monkeypatch, tmp_path):
    current = b"<svg>current</svg>\n"
    hand_edited = b"<svg>edited during trace</svg>\n"
    logo_svg, logo_svg_sha256 = configure_trace(monkeypatch, tmp_path, current)
    recorded = logo_svg_sha256.read_text(encoding="utf-8")

    def edit_while_tracing(command, check):
        assert check
        output = Path(command[command.index("--output") + 1])
        output.write_bytes(b"<svg>traced</svg>\n")
        logo_svg.write_bytes(hand_edited)

    monkeypatch.setattr(trace_logo.subprocess, "run", edit_while_tracing)

    with pytest.raises(SystemExit, match="--force"):
        trace_logo.main()

    assert logo_svg.read_bytes() == hand_edited
    assert logo_svg_sha256.read_text(encoding="utf-8") == recorded


@pytest.mark.parametrize(
    ("checksum", "message"),
    [(None, "missing"), ("not-a-sha256", "malformed")],
)
def test_trace_fails_closed_for_invalid_checksum(
    monkeypatch, tmp_path, checksum, message
):
    current = b"<svg>current</svg>\n"
    logo_svg, logo_svg_sha256 = configure_trace(monkeypatch, tmp_path, current)
    if checksum is None:
        logo_svg_sha256.unlink()
    else:
        logo_svg_sha256.write_text(checksum, encoding="utf-8")
    monkeypatch.setattr(
        trace_logo.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("vtracer must not run"),
    )

    with pytest.raises(SystemExit, match=message):
        trace_logo.main()

    assert logo_svg.read_bytes() == current


def test_force_flag_is_exposed_by_cli(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["trace_logo.py", "--force"])

    assert trace_logo.parse_args().force


def test_trace_updates_svg_and_checksum(monkeypatch, tmp_path):
    logo_svg, logo_svg_sha256 = configure_trace(monkeypatch, tmp_path)
    traced = b"<svg>new trace</svg>\n"
    monkeypatch.setattr(trace_logo.subprocess, "run", write_traced_svg(traced))

    trace_logo.main()

    assert logo_svg.read_bytes() == traced
    assert logo_svg_sha256.read_text(encoding="utf-8") == f"{sha256(traced)}\n"


def test_force_explicitly_replaces_modified_svg(monkeypatch, tmp_path):
    logo_svg, logo_svg_sha256 = configure_trace(monkeypatch, tmp_path)
    logo_svg_sha256.write_text(
        f"{sha256(b'<svg>previous trace</svg>')}\n", encoding="utf-8"
    )
    traced = b"<svg>forced trace</svg>\n"
    monkeypatch.setattr(trace_logo.subprocess, "run", write_traced_svg(traced))

    trace_logo.main(force=True)

    assert logo_svg.read_bytes() == traced
    assert logo_svg_sha256.read_text(encoding="utf-8") == f"{sha256(traced)}\n"


def test_failed_trace_preserves_svg_and_checksum(monkeypatch, tmp_path):
    current = b"<svg>current</svg>\n"
    logo_svg, logo_svg_sha256 = configure_trace(monkeypatch, tmp_path, current)
    recorded = logo_svg_sha256.read_text(encoding="utf-8")

    def fail(command, check):
        raise subprocess.CalledProcessError(1, command[0])

    monkeypatch.setattr(trace_logo.subprocess, "run", fail)

    with pytest.raises(subprocess.CalledProcessError):
        trace_logo.main()

    assert logo_svg.read_bytes() == current
    assert logo_svg_sha256.read_text(encoding="utf-8") == recorded


def test_missing_svg_can_be_recovered_from_recorded_trace(monkeypatch, tmp_path):
    logo_svg, logo_svg_sha256 = configure_trace(monkeypatch, tmp_path)
    recorded = logo_svg_sha256.read_text(encoding="utf-8")
    logo_svg.unlink()
    traced = b"<svg>recovered</svg>\n"
    monkeypatch.setattr(trace_logo.subprocess, "run", write_traced_svg(traced))

    trace_logo.main()

    assert logo_svg.read_bytes() == traced
    assert logo_svg_sha256.read_text(encoding="utf-8") == f"{sha256(traced)}\n"
    assert recorded != logo_svg_sha256.read_text(encoding="utf-8")
