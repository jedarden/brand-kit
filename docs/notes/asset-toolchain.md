# Asset toolchain pins

The committed PNGs in `avatars/`, `banners/`, `favicon/` and `logo/` are
byte-reproducible only with the exact toolchain below. The CI regression gate
(`brand-kit-ci` WorkflowTemplate in `jedarden/declarative-config`,
`k8s/iad-ci/argo-workflows/brand-kit-ci-workflowtemplate.yml`) checks that its
install commands still match this table before installing them, then runs the
full pytest suite and `tools/verify_assets.py`. It fails on pin or install
contract drift, test failures, unexpected generated files, or regenerated
diffs, so a commit whose assets or checks did not pass this toolchain is a
broken commit.
The ordered commands and required Argo log evidence are documented under
[CI regression gate](../README.md#ci-regression-gate).

## Pinned versions (canonical)

| Tool | Pin | Install command | Notes |
|---|---|---|---|
| resvg | `0.47.0` | `cargo install resvg@0.47.0` | Renders `source/logo.svg` at each target size. |
| vtracer | `0.6.5` Cargo CLI | `cargo install vtracer@0.6.5` | `tools/trace_logo.py` invokes the Cargo-installed binary; it does not use the PyPI `vtracer` package. |
| Pillow | `12.1.1` — **PyPI wheel build** | `.venv/bin/python -m pip install --only-binary=:all: Pillow==12.1.1` | Encodes every PNG. The wheel-only flag is part of the pin. |
| pytest | `9.0.2` | `.venv/bin/python -m pip install --only-binary=:all: pytest==9.0.2` | Runs the regression suite; it does not affect generated asset bytes. |

Python and Rust are runtime/build prerequisites rather than byte-sensitive
pins: CI uses the Debian `python3` package and the Rustup `stable` toolchain.
They remain documented below because the checker only enforces the exact
four-tool reproducibility contract above.

### Pin parity check

`tools/check_asset_toolchain.py` parses this table and the actual
`brand-kit-ci` WorkflowTemplate. It compares every tool name, version,
installer, install command, and install option, and specifically requires
Pillow's `--only-binary=:all:` PyPI-wheel contract. It also rejects a tool
added to either side without a matching install on the other side.

Run the same check locally from the brand-kit checkout (with the sibling
`declarative-config` checkout available) before changing either set of pins:

```bash
python3 tools/check_asset_toolchain.py \
  --workflow-template ../declarative-config/k8s/iad-ci/argo-workflows/brand-kit-ci-workflowtemplate.yml
```

The CI WorkflowTemplate runs this command against the manifest cloned from the
canonical Forgejo repository before it installs Python dependencies or runs
the asset regression gate. Keep the table's install-command column and the
manifest's install block in the same commit when bumping a pin.

## How `trace_logo.py` obtains vtracer

`tools/trace_logo.py` is a Python entry point, but it does not import a Python
binding or use the similarly named package from PyPI. It launches the `vtracer`
executable found on `PATH`; the canonical pin is therefore the Cargo crate/CLI
installed by `cargo install vtracer@0.6.5`. Do not substitute a pip-installed
`vtracer` package: that package and build are not covered by this pin.

Both Python entry points use Pillow, so both must run with the venv interpreter.
`resvg` is needed whenever derived assets are regenerated. The vtracer command
is needed only for an explicit raster-to-vector replacement of
`source/logo.svg`.

## Logo source-of-truth contract

`source/logo.svg` is the authoritative source for the opaque logo. The normal
build renders avatars, favicons, opaque logo masters, and `logo/logo.svg` from
that SVG with `resvg`; `source/logo.png` is never a raster fallback.
`source/logo-transparent.svg` is a separate, hand-maintained authoritative
source for the transparent logo outputs.

`source/logo.png` has two narrower roles: it is preserved provenance and is
copied to `logo/logo-original.png`; it may also be the input to an explicit
`tools/trace_logo.py` run that replaces `source/logo.svg`. The trace is not part
of the normal build from the authoritative SVG.

`source/logo.svg.sha256` is the committed integrity sidecar for the
authoritative SVG. It contains the SVG's bare SHA-256 digest. The normal build
does not read it, but `verify_assets.py` rejects a missing, malformed, or stale
sidecar. Whenever the bytes of `source/logo.svg` change—including a direct
edit—review the SVG and refresh the sidecar in the same commit:

```bash
sha256sum source/logo.svg | awk '{print $1}' > source/logo.svg.sha256
```

`trace_logo.py` writes the refreshed sidecar automatically after a successful
trace. Do not refresh it merely to hide an unexpected SVG change. Before an
ordinary trace, `trace_logo.py` fails closed when the digest is missing or
malformed. It refuses to replace an existing SVG whose digest does not match
and checks that invariant again immediately before replacement.
`trace_logo.py --force` is the explicit escape hatch for deliberately replacing
a modified SVG from `source/logo.png`.

## The Pillow build flavor is part of the pin

This is load-bearing: **the same Pillow version number from a different build
produces different bytes.** Verified 2026-09-15: regenerating from identical
sources with NixOS-system Pillow 12.1.1 vs the PyPI `12.1.1` wheel differed in
**32 of 37** asset files (the encoder's zlib differs between builds).

Consequence: always regenerate with the PyPI wheel installed in `.venv`, **not**
a distro/system Pillow, even at the pinned version number. The setup command
below uses `--only-binary=:all:` so pip fails instead of silently building an
unpinned source distribution.

## Resvg clean-cache rebuild check

Verified 2026-09-27 with Rust `1.97.1` and Cargo `1.97.1`: two independent
`cargo install resvg@0.47.0` runs used separate empty `CARGO_HOME` directories
and separate target directories. Both installs resolved the same 63-package
dependency graph and installed resvg `0.47.0`. The installed executable bytes
were not identical, so the executable itself is not used as the determinism
criterion.

Each renderer then regenerated a clean `git archive HEAD` copy with Pillow
`12.1.1` (the pinned PyPI wheel version). `diff -qr` reported identical trees,
and the normalized SHA-256 list for all 39 generated files was identical:
`5ba92cb6753d4f905a0a38a5f583a59c5173ac8a8f0cf8a1a869ba2bc79d35e7`.
Therefore, independent clean-cache rebuilds of the currently resolved resvg
dependency graph render the committed assets byte-for-byte identically.

This is empirical evidence for the current resolution, not a Cargo lock: the
`cargo install` command can select different compatible transitive versions
after a future crates.io index change. If a later clean-cache check diverges,
replace this floating install with a committed/vendored lock and a pinned
toolchain image before accepting regenerated assets.

## Regenerating

Run the one-time setup (or repeat it after a pin bump):

```bash
cargo install resvg@0.47.0 vtracer@0.6.5
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: Pillow==12.1.1 pytest==9.0.2
```

For a normal edit to `source/logo.svg`, `source/logo-transparent.svg`, or
`source/hero.png`, skip the trace and run:

```bash
.venv/bin/python tools/build_assets.py
.venv/bin/python tools/verify_assets.py
```

Review and commit the changed bytes. On an already-generated commit, repeating
those commands and then running `git diff --exit-code` must produce no diff.

A PNG-only change that is meant to update `logo/logo-original.png` does not
require a trace; use the normal build commands above. To replace the opaque SVG
from the original raster, run this explicit transition in order:

1. Replace `source/logo.png`.
2. Run the trace:

   ```bash
   .venv/bin/python tools/trace_logo.py
   ```

3. Review `source/logo.svg`. Update `source/logo-transparent.svg` separately if
   the transparent artwork changed.
4. Run the build and verification commands:

   ```bash
   .venv/bin/python tools/build_assets.py
   .venv/bin/python tools/verify_assets.py
   ```

5. Review the complete diff and commit the traced SVG, updated checksum, sources,
   and generated assets together.

The trace writes `source/logo.svg` and `source/logo.svg.sha256` only. If it
refuses because the current SVG differs from the recorded digest, either review
the SVG, refresh the sidecar, and continue from `build_assets.py`, or run
`.venv/bin/python tools/trace_logo.py --force` only when replacing that SVG is
intentional, then continue with the same build and verification steps.

`tools/build_assets.py` aborts if `resvg` or a source file is missing — there
is no raster-resize fallback. Fallback output is not byte-identical to the
vector renders, so silently degrading would make every environment produce
different bytes for the same commit.

## Bumping a pin

All pins move together, in one commit:

1. Update the table above.
2. Update the install lines in `brand-kit-ci-workflowtemplate.yml` (they name
   this file in a comment).
3. Regenerate every asset with the new toolchain (using the applicable sequence
   above) and include the full byte diff in the same commit.
4. Push and confirm the full CI regression gate passes: asset verification,
   all pytest tests, and the no-diff check.

## Provenance

Every asset byte in the tree as of 2026-09-15 was produced by exactly the
pinned toolchain above (resvg 0.47.0 + PyPI Pillow 12.1.1 wheel): two
consecutive regenerations from `source/` were byte-identical, and the
committed tree matches that output exactly. `source/logo.svg.sha256` records
the SHA-256 of the current authoritative SVG. A later successful trace updates
it automatically; a direct SVG edit must update it in the same commit.
History before 2026-09-15 was generated by unrecorded
tool versions (including an era before `optimize=True`, and raster-fallback
renders before the 2026-05-22 vectorization) and is not reproducible.
