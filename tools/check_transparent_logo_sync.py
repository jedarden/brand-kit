#!/usr/bin/env python3
"""Review trigger tying the transparent logo master to opaque logo changes.

``source/logo.svg`` and ``source/logo-transparent.svg`` are independently
maintained: the transparent master is hand-edited, never generated (see
docs/notes/asset-toolchain.md, "Logo source-of-truth contract").  That contract
means a redesign of the authoritative opaque logo can ship while the
transparent master silently keeps the old artwork: the transparent sidecar
integrity pin and the CI regen-diff both still pass because nothing compares
the two designs.

This tool closes that gap as a review trigger, not an auto-generator.  Given a
Git diff (``--base`` .. ``--head``), it:

1. fires whenever ``source/logo.svg`` carries new content or is removed in the
   diff (added, modified, copied, or either side of a rename);
2. renders both SVGs with the pinned ``resvg`` and compares them pixel-wise,
   honouring the documented background relationship: every opaque pixel must
   match the transparent artwork composited over one of the known background
   layers, and fully transparent pixels whose opaque color sits on a
   background blend segment are background anti-aliasing, not artwork; and
3. reports the outcome and enforces a deliberate transparent-variant decision:
   a diverging design fails unless ``source/logo-transparent.svg`` itself is
   part of the same diff, which is the reviewable record of the decision to
   keep the transparent design intentionally different.

The tool never writes SVG content.  Updating the transparent master stays a
human decision; the check only makes a missing decision impossible to miss.

Typical use, from the repository root::

    python3 tools/check_transparent_logo_sync.py --base v1.0.0

Exit status is ``0`` when the trigger did not fire, the rendered designs are
in sync, or the divergence is acknowledged by a same-diff change to
``source/logo-transparent.svg``; ``1`` when a triggered comparison is
divergent without that acknowledgment, when the comparison cannot run, or on
a usage error.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageChops

# Support both `python3 tools/check_transparent_logo_sync.py` and module/test
# imports from the repository root.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.check_release_preflight import PathChange, changed_path_changes

OPAQUE_LOGO_RELPATH = "source/logo.svg"
TRANSPARENT_LOGO_RELPATH = "source/logo-transparent.svg"

# The opaque SVG paints two background layers beneath the artwork: a
# full-canvas ink rectangle and the canvas-cream cut-out that wraps the
# artwork silhouette.  The transparent master removes both.  These are the
# documented kit fills; a redesign that changes them must either update the
# transparent master or pass explicit --background-color values in the same
# review.  The corner-derived canvas color (see compare_renders) is added to
# these automatically, so a canvas-fill-only change still compares correctly.
DEFAULT_BACKGROUND_COLORS = (
    (0xEF, 0xDD, 0xCB),  # Canvas Cream canvas cut-out
    (0x0A, 0x0A, 0x08),  # Ink silhouette backdrop
)
DEFAULT_RENDER_SIZE = 256
# Per-channel rounding headroom for re-blending resvg's quantized alpha over
# a solid layer.  Verified 2026-11-08: the committed in-sync pair reports zero
# divergent pixels at tolerance 3 with band 4, while a one-pixel artwork move
# or any color substitution produces margins two orders of magnitude larger.
DEFAULT_TOLERANCE = 3
DEFAULT_BLEND_BAND = 4

_HEAD_SIDE_STATUSES = frozenset({"A", "M", "C", "R"})


class TransparentLogoSyncError(ValueError):
    """The sync check cannot produce a trustworthy comparison."""


@dataclass(frozen=True)
class ComparisonReport:
    """Pixel-level outcome of comparing the two rendered SVG masters."""

    render_size: int
    tolerance: int
    blend_band: int
    background_colors: tuple[tuple[int, int, int], ...]
    compared_pixels: int
    divergent_pixels: int
    divergent_fraction: float
    divergent_bbox: tuple[int, int, int, int] | None
    max_margin: int
    opaque_artwork_pixels: int
    excluded_blend_pixels: int
    corner_color: tuple[int, int, int] | None
    notes: tuple[str, ...] = field(default=())

    @property
    def in_sync(self) -> bool:
        return self.divergent_pixels == 0


@dataclass(frozen=True)
class SyncCheckReport:
    """Full CLI outcome: trigger state, comparison, and enforcement decision."""

    triggered: bool
    trigger_reason: str
    opaque_status: str | None
    transparent_status: str | None
    comparison: ComparisonReport | None
    acknowledged: bool
    failed: bool
    failure_reason: str = ""
    opaque_dimensions: tuple[int, int] | None = None
    transparent_dimensions: tuple[int, int] | None = None


def parse_background_color(text: str) -> tuple[int, int, int]:
    """Parse ``#RRGGBB`` or ``RRGGBB`` into an RGB triple."""

    value = text.strip().lstrip("#")
    if len(value) != 6:
        raise TransparentLogoSyncError(
            f"invalid background color {text!r}: expected #RRGGBB"
        )
    try:
        return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
    except ValueError as error:
        raise TransparentLogoSyncError(
            f"invalid background color {text!r}: expected hexadecimal RGB"
        ) from error


def format_background_color(color: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*color)


def parse_svg_dimensions(svg_bytes: bytes) -> tuple[int, int] | None:
    """Return the SVG's declared ``(width, height)``, or ``None``."""

    try:
        root = ET.fromstring(svg_bytes)
    except ET.ParseError:
        return None
    dimensions = []
    for attribute in ("width", "height"):
        raw = root.get(attribute)
        if raw is None:
            return None
        value = raw.strip().removesuffix("px")
        try:
            dimensions.append(int(float(value)))
        except ValueError:
            return None
    return tuple(dimensions)  # type: ignore[return-value]


def _run_resvg(resvg: str, svg_bytes: bytes, render_size: int) -> Image.Image:
    with tempfile.TemporaryDirectory() as workdir:
        source = Path(workdir) / "input.svg"
        output = Path(workdir) / "render.png"
        source.write_bytes(svg_bytes)
        try:
            subprocess.run(
                [
                    resvg,
                    "--width",
                    str(render_size),
                    "--height",
                    str(render_size),
                    str(source),
                    str(output),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as error:
            raise TransparentLogoSyncError(
                f"resvg is not available at {resvg!r}; install the pinned"
                " toolchain (docs/notes/asset-toolchain.md) so the rendered"
                " comparison can run"
            ) from error
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or "").strip()
            suffix = f": {detail}" if detail else ""
            raise TransparentLogoSyncError(
                f"resvg failed to render an SVG master{suffix}"
            ) from error
        if not output.is_file():
            raise TransparentLogoSyncError("resvg produced no render output")
        return Image.open(output).convert("RGBA")


def _git_show(root: Path, head: str, relpath: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "show", f"{head}:{relpath}"],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as error:
        raise TransparentLogoSyncError(
            f"{relpath} is not present in the candidate tree ({head})"
        ) from error
    except OSError as error:
        raise TransparentLogoSyncError(f"could not read {relpath} from Git") from error
    return result.stdout


def _head_side_status(change: PathChange, relpath: str) -> str | None:
    """Return the diff status of *relpath*'s candidate-tree side, if any.

    Renames and copies carry two paths; only the second (new) side has content
    in the candidate tree.  A deletion removes the path from that tree, so it
    never counts as candidate content.
    """

    status = change.status[:1]
    if status in {"R", "C"}:
        if len(change.paths) >= 2 and change.paths[1] == relpath:
            return status
        if status == "R" and change.paths and change.paths[0] == relpath:
            # The candidate no longer has the old path.  Return a deletion
            # trigger so the subsequent candidate-tree read fails closed.
            return "D"
        return None
    if relpath in change.paths:
        return status
    return None


def trigger_status(changes: Sequence[PathChange], relpath: str) -> str | None:
    """Return the candidate-tree diff status of *relpath*, or ``None``."""

    for change in changes:
        status = _head_side_status(change, relpath)
        if status is not None:
            return status[:1]
    return None


def _corner_background(opaque: Image.Image) -> tuple[int, int, int] | None:
    """Derive the canvas color from the opaque render's corner patches.

    Returns ``None`` when the corner patches disagree, which means artwork
    reaches the canvas corners and the canvas color cannot be sampled safely.
    """

    size = opaque.size[0]
    patch = max(2, size // 16)
    offsets = (0, size - patch)
    medians = []
    for top in offsets:
        for left in offsets:
            pixels = (
                opaque.crop((left, top, left + patch, top + patch))
                .convert("RGB")
                .tobytes()
            )
            medians.append(
                tuple(
                    sorted(pixels[index] for index in range(channel, len(pixels), 3))[
                        len(pixels) // 6
                    ]
                    for channel in range(3)
                )
            )
    if any(
        max(abs(medians[0][channel] - other[channel]) for channel in range(3)) > 4
        for other in medians[1:]
    ):
        return None
    return tuple(
        sorted(median[channel] for median in medians)[len(medians) // 2]
        for channel in range(3)
    )  # type: ignore[return-value]


def _max_channel_diff(left: Image.Image, right: Image.Image) -> Image.Image:
    combined = None
    for left_channel, right_channel in zip(left.split(), right.split()):
        difference = ImageChops.difference(left_channel, right_channel)
        combined = difference if combined is None else ImageChops.lighter(combined, difference)
    return combined


def _blend_segment_distance(
    pixel: tuple[int, int, int],
    backgrounds: Sequence[tuple[int, int, int]],
) -> int:
    """Closest per-channel distance from *pixel* to any background pair's blend."""

    best = 255 * 3
    for index, first in enumerate(backgrounds):
        for second in backgrounds[index + 1 :]:
            direction = tuple(b - f for f, b in zip(first, second))
            length_squared = sum(value * value for value in direction)
            offset = tuple(pixel[channel] - first[channel] for channel in range(3))
            if length_squared == 0:
                position = 0.0
            else:
                position = sum(
                    offset[channel] * direction[channel] for channel in range(3)
                ) / length_squared
                position = max(0.0, min(1.0, position))
            point = tuple(
                first[channel] + position * direction[channel] for channel in range(3)
            )
            distance = sum(
                abs(pixel[channel] - int(round(point[channel]))) for channel in range(3)
            )
            best = min(best, distance)
    return best


def compare_renders(
    opaque_rgb: Image.Image,
    transparent_rgba: Image.Image,
    *,
    background_colors: Sequence[tuple[int, int, int]],
    tolerance: int = DEFAULT_TOLERANCE,
    blend_band: int = DEFAULT_BLEND_BAND,
    corner_color: tuple[int, int, int] | None = None,
) -> ComparisonReport:
    """Compare the rendered masters using the documented background model.

    Every opaque pixel must match the transparent artwork re-composited over
    one of the background layers within *tolerance* per channel.  Fully
    transparent pixels whose opaque color lies within *blend_band* of the
    segment between two background layers are background anti-aliasing
    transitions and are excluded; artwork removed at those colors is
    indistinguishable from background, a documented limit of the comparison.
    """

    if opaque_rgb.size != transparent_rgba.size:
        raise TransparentLogoSyncError(
            "rendered masters must share one canvas size for comparison"
        )

    backgrounds = list(dict.fromkeys(background_colors))
    notes = []
    if corner_color is not None and corner_color not in backgrounds:
        backgrounds.append(corner_color)
        notes.append(
            "canvas color sampled from the opaque render corners added as a "
            f"background layer ({format_background_color(corner_color)})"
        )

    size = opaque_rgb.size[0]
    total = size * size
    composited = []
    for color in backgrounds:
        base = Image.new("RGBA", (size, size), tuple(color) + (255,))
        artwork = Image.alpha_composite(base, transparent_rgba).convert("RGB")
        composited.append(_max_channel_diff(opaque_rgb, artwork))
    margins = composited[0]
    for other in composited[1:]:
        margins = ImageChops.darker(margins, other)

    divergent = margins.point(lambda value: 255 if value > tolerance else 0)
    alpha_bytes = transparent_rgba.split()[3].tobytes()
    opaque_bytes = opaque_rgb.tobytes()

    excluded = 0
    kept = 0
    mask_bytes = divergent.tobytes()
    for index in range(total):
        if mask_bytes[index]:
            if (
                alpha_bytes[index] == 0
                and _blend_segment_distance(
                    tuple(opaque_bytes[3 * index : 3 * index + 3]), backgrounds
                )
                <= blend_band
            ):
                excluded += 1
            else:
                kept += 1

    histogram = margins.histogram()
    max_margin = max((value for value, count in enumerate(histogram) if count), default=0)
    bbox = divergent.getbbox() if kept else None
    artwork_pixels = sum(1 for value in alpha_bytes if value)

    return ComparisonReport(
        render_size=size,
        tolerance=tolerance,
        blend_band=blend_band,
        background_colors=tuple(backgrounds),
        compared_pixels=total,
        divergent_pixels=kept,
        divergent_fraction=kept / total,
        divergent_bbox=bbox,
        max_margin=max_margin,
        opaque_artwork_pixels=artwork_pixels,
        excluded_blend_pixels=excluded,
        corner_color=corner_color,
        notes=tuple(notes),
    )


def check_transparent_logo_sync(
    root: Path,
    base: str,
    head: str = "HEAD",
    *,
    resvg: str | None = None,
    render_size: int = DEFAULT_RENDER_SIZE,
    tolerance: int = DEFAULT_TOLERANCE,
    blend_band: int = DEFAULT_BLEND_BAND,
    background_colors: Sequence[tuple[int, int, int]] = DEFAULT_BACKGROUND_COLORS,
) -> SyncCheckReport:
    """Run the transparent-logo sync review trigger for one Git diff.

    Rendering always reads the candidate tree via ``git show <head>:<path>``,
    so the comparison matches the diff being classified even when the working
    tree has moved on.
    """

    root = root.resolve()
    changes = changed_path_changes(root, base, head)
    opaque_status = trigger_status(changes, OPAQUE_LOGO_RELPATH)
    transparent_status = trigger_status(changes, TRANSPARENT_LOGO_RELPATH)

    if opaque_status is None:
        return SyncCheckReport(
            triggered=False,
            trigger_reason=(
                f"{OPAQUE_LOGO_RELPATH} carries no new content in {base}..{head}; "
                "the transparent-logo sync trigger does not fire"
            ),
            opaque_status=None,
            transparent_status=transparent_status,
            comparison=None,
            acknowledged=False,
            failed=False,
        )

    if resvg is None:
        resvg = shutil.which("resvg")
        if resvg is None:
            raise TransparentLogoSyncError(
                "resvg was not found on PATH; install the pinned toolchain"
                " (docs/notes/asset-toolchain.md) or pass --resvg"
            )

    triggered_reason = (
        f"{OPAQUE_LOGO_RELPATH} changed in {base}..{head} (status {opaque_status}); "
        "the transparent-logo sync trigger fires"
    )
    try:
        opaque_bytes = _git_show(root, head, OPAQUE_LOGO_RELPATH)
    except TransparentLogoSyncError as error:
        return SyncCheckReport(
            triggered=True,
            trigger_reason=triggered_reason,
            opaque_status=opaque_status,
            transparent_status=transparent_status,
            comparison=None,
            acknowledged=False,
            failed=True,
            failure_reason=str(error),
        )
    try:
        transparent_bytes = _git_show(root, head, TRANSPARENT_LOGO_RELPATH)
    except TransparentLogoSyncError as error:
        return SyncCheckReport(
            triggered=True,
            trigger_reason=triggered_reason,
            opaque_status=opaque_status,
            transparent_status=transparent_status,
            comparison=None,
            acknowledged=False,
            failed=True,
            failure_reason=str(error) + (
                "; restore or update the transparent master so the"
                " transparent-variant decision is explicit"
            ),
        )

    opaque_render = _run_resvg(resvg, opaque_bytes, render_size)
    transparent_render = _run_resvg(resvg, transparent_bytes, render_size)
    opaque_dimensions = parse_svg_dimensions(opaque_bytes)
    transparent_dimensions = parse_svg_dimensions(transparent_bytes)

    corner_color = _corner_background(opaque_render)
    comparison = compare_renders(
        opaque_render.convert("RGB"),
        transparent_render,
        background_colors=background_colors,
        tolerance=tolerance,
        blend_band=blend_band,
        corner_color=corner_color,
    )

    dimensions_note = ""
    if (
        opaque_dimensions is not None
        and transparent_dimensions is not None
        and opaque_dimensions != transparent_dimensions
    ):
        dimensions_note = (
            f"declared SVG dimensions differ: opaque {opaque_dimensions}, "
            f"transparent {transparent_dimensions}"
        )
        comparison = ComparisonReport(
            render_size=comparison.render_size,
            tolerance=comparison.tolerance,
            blend_band=comparison.blend_band,
            background_colors=comparison.background_colors,
            compared_pixels=comparison.compared_pixels,
            divergent_pixels=max(1, comparison.divergent_pixels),
            divergent_fraction=comparison.divergent_fraction,
            divergent_bbox=comparison.divergent_bbox,
            max_margin=comparison.max_margin,
            opaque_artwork_pixels=comparison.opaque_artwork_pixels,
            excluded_blend_pixels=comparison.excluded_blend_pixels,
            corner_color=comparison.corner_color,
            notes=comparison.notes + (dimensions_note,),
        )

    acknowledged = transparent_status is not None
    failed = not comparison.in_sync and not acknowledged
    return SyncCheckReport(
        triggered=True,
        trigger_reason=triggered_reason,
        opaque_status=opaque_status,
        transparent_status=transparent_status,
        comparison=comparison,
        acknowledged=acknowledged,
        failed=failed,
        failure_reason=(
            "the rendered transparent design diverges from the rendered opaque"
            " design while source/logo-transparent.svg is unchanged in this"
            " diff"
            if failed
            else ""
        ),
        opaque_dimensions=opaque_dimensions,
        transparent_dimensions=transparent_dimensions,
    )


def _format_bbox(bbox: tuple[int, int, int, int] | None) -> str:
    if bbox is None:
        return "none"
    return (
        f"x{bbox[0]}..{bbox[2]} y{bbox[1]}..{bbox[3]} "
        f"({bbox[2] - bbox[0]}x{bbox[3] - bbox[1]} region)"
    )


def format_report(report: SyncCheckReport) -> str:
    """Render the human-readable CLI report."""

    lines: list[str] = []
    if not report.triggered:
        lines.append(f"PASS transparent-logo-sync: not-triggered ({report.trigger_reason})")
        if report.transparent_status is not None:
            lines.append(
                "NOTE transparent-logo-sync: "
                f"{TRANSPARENT_LOGO_RELPATH} changed (status {report.transparent_status}) "
                f"without a {OPAQUE_LOGO_RELPATH} change; no comparison applies"
            )
        return "\n".join(lines)

    lines.append(f"TRIGGER transparent-logo-sync: {report.trigger_reason}")
    if report.comparison is None:
        lines.append(f"FAIL transparent-logo-sync: {report.failure_reason}")
        return "\n".join(lines)

    comparison = report.comparison
    for note in comparison.notes:
        lines.append(f"NOTE transparent-logo-sync: {note}")
    stats = (
        f"render={comparison.render_size}x{comparison.render_size} "
        f"tolerance={comparison.tolerance} blend-band={comparison.blend_band} "
        f"backgrounds={'/'.join(format_background_color(color) for color in comparison.background_colors)} "
        f"divergent={comparison.divergent_pixels}/{comparison.compared_pixels} "
        f"({comparison.divergent_fraction:.2%}) max-margin={comparison.max_margin} "
        f"transparent-coverage={comparison.opaque_artwork_pixels} "
        f"excluded-background-blend={comparison.excluded_blend_pixels} "
        f"divergence-bbox={_format_bbox(comparison.divergent_bbox)}"
    )
    if comparison.in_sync:
        lines.append(f"PASS transparent-logo-sync: in-sync ({stats})")
        return "\n".join(lines)
    if report.acknowledged:
        lines.append(
            "REVIEW transparent-logo-sync: divergence acknowledged by a change to "
            f"{TRANSPARENT_LOGO_RELPATH} (status {report.transparent_status}) in the "
            "same diff; confirm the transparent-variant decision in release review"
        )
        lines.append(f"REVIEW transparent-logo-sync: {stats}")
        return "\n".join(lines)
    lines.append(f"FAIL transparent-logo-sync: {stats}")
    lines.append(f"FAIL transparent-logo-sync: {report.failure_reason}")
    lines.append(
        "FAIL transparent-logo-sync: update "
        f"{TRANSPARENT_LOGO_RELPATH} to the new opaque design, regenerate the"
        " transparent assets, refresh its .sha256 sidecar, and rerun; or, if"
        " the diverging transparent design is deliberate, change"
        f" {TRANSPARENT_LOGO_RELPATH} in the same commit so the decision is"
        " reviewable"
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Review trigger: compare rendered source/logo.svg and"
            " source/logo-transparent.svg whenever the opaque master changes."
        )
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
        default=_ROOT,
        help="repository root (default: the brand-kit checkout)",
    )
    parser.add_argument(
        "--resvg",
        default=None,
        help="resvg executable (default: the pinned resvg on PATH)",
    )
    parser.add_argument(
        "--render-size",
        type=int,
        default=DEFAULT_RENDER_SIZE,
        help=f"square comparison render size (default: {DEFAULT_RENDER_SIZE})",
    )
    parser.add_argument(
        "--tolerance",
        type=int,
        default=DEFAULT_TOLERANCE,
        help=(
            "maximum per-channel rounding delta for a matching pixel"
            f" (default: {DEFAULT_TOLERANCE})"
        ),
    )
    parser.add_argument(
        "--blend-band",
        type=int,
        default=DEFAULT_BLEND_BAND,
        help=(
            "per-channel distance to a background blend segment at which a"
            " fully transparent pixel is background anti-aliasing"
            f" (default: {DEFAULT_BLEND_BAND})"
        ),
    )
    parser.add_argument(
        "--background-color",
        action="append",
        dest="background_colors",
        default=None,
        metavar="#RRGGBB",
        help=(
            "opaque background layer color; repeatable (defaults: "
            + " and ".join(
                format_background_color(color) for color in DEFAULT_BACKGROUND_COLORS
            )
            + ")"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    try:
        background_colors = (
            [parse_background_color(value) for value in args.background_colors]
            if args.background_colors
            else list(DEFAULT_BACKGROUND_COLORS)
        )
        if not background_colors:
            raise TransparentLogoSyncError(
                "at least one --background-color is required for the comparison"
            )
        if args.render_size < 1:
            raise TransparentLogoSyncError("--render-size must be a positive integer")
        if args.tolerance < 0 or args.blend_band < 0:
            raise TransparentLogoSyncError(
                "--tolerance and --blend-band must be non-negative integers"
            )
        report = check_transparent_logo_sync(
            root,
            args.base,
            args.head,
            resvg=args.resvg,
            render_size=args.render_size,
            tolerance=args.tolerance,
            blend_band=args.blend_band,
            background_colors=background_colors,
        )
    except TransparentLogoSyncError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(format_report(report))
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
