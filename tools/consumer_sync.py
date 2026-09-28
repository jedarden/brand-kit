#!/usr/bin/env python3
"""Refresh and verify the downstream consumers of the brand kit (ADR-1 follow-through).

CI proves that this repo's committed assets match their sources. It cannot tell
a *consumer* that its copy went stale when a new Forgejo Release is published —
that loop is closed here by running this script after each published release and
after its tag propagates through the GitHub mirror (see
docs/notes/post-tag-consumer-update.md for the full checklist).

Before inspecting or changing a consumer, this script performs a read-only
Forgejo API lookup for the release tag. A missing, draft, malformed, or
inaccessible release record fails closed; ``--offline`` does not bypass this
release gate.

Consumers are registered in consumer_registry.json. The default registration is:
  jedarden.com  (local checkout, default ~/jedarden.com)
    public/brand/logo.svg        byte-identical copy of logo/logo.svg
    public/brand/logo-512.png    byte-identical copy of logo/logo-512.png
    public/brand/og.jpg          hand-recompressed JPEG derivative of source/hero.png
                                 (same crop as banners/open-graph-1200x630.png,
                                 saved as JPEG quality 88)
    src/assets/brand-hero.jpg    hand-recompressed JPEG derivative of source/hero.png
                                 at native 1536x1024 (quality 88); rendered on the
                                 /brand page through Astro's asset pipeline
  github.com/jedarden
    profile avatar               manual upload; verified live against
                                 avatars/github-460.png (GitHub recompresses on
                                 serve, so this is a perceptual compare)

The --apply mode writes the registered consumer's machine-readable provenance
file after every registered asset passes. It records the exact brand-kit commit,
the release tag when HEAD is tagged, every transform, and a SHA-256 for each
consumer file. --check reports missing, malformed, or stale provenance.

Site-owned favicon refresh:
  node scripts/make-favicons.mjs
                                 run in jedarden.com after copying a changed
                                 logo; consumer_sync.py deliberately does not
                                 regenerate or overwrite these files

Modes:
  --check  verify only (default); exits 1 on any stale/mismatched consumer
  --apply  transactionally refresh the registered checkout (never commits), then verify

Apply transaction behavior:
  all changed copies, JPEG derivatives, and the provenance manifest are built in
  a checkout-local staging directory first. A staging or conversion failure
  leaves the checkout unchanged. Installation replaces changed files only after
  the complete staged set is ready; if a replacement fails, backups restore the
  files already replaced and the command exits non-zero. A successful rerun of
  the same release compares the staged result and performs no writes when the
  checkout is already current.

A published Forgejo release record is required before either mode runs. Set
FORGEJO_TOKEN to a read-only Forgejo API token when the Forgejo instance requires
authentication.

Examples:
  python3 tools/consumer_sync.py --check
  python3 tools/consumer_sync.py --apply --release-tag v1.0.0
  python3 tools/consumer_sync.py --check --offline   # skip live-network checks
  python3 tools/consumer_sync.py --check --consumer <id> --site <checkout>
"""
import argparse
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from tools import release_publish

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_RELPATH = "consumer_registry.json"
REGISTRY_SCHEMA_VERSION = 1
PROVENANCE_SCHEMA_VERSION = 1
DEFAULT_CONSUMER = "jedarden.com"

# JPEG recompression settings that reproduce the derivatives already shipped by
# jedarden.com (brand-hero.jpg was verified pixel-identical to a quality-88
# re-encode of the current source/hero.png).
JPEG_QUALITY = 88
# Mean-luma tolerance for comparing a stored JPEG decode against the freshly
# cropped source. A just-applied q88 file still decodes ~1.5 off the in-memory
# crop (JPEG loss itself); the headroom above that absorbs cross-version
# encoder drift while staying far below "actually a different image".
JPEG_TOLERANCE = 3.0
# GitHub re-encodes the uploaded avatar for serving; the live copy of the
# identical source image measures ~3 mean luma against the committed PNG.
# A genuinely different image measures an order of magnitude higher.
AVATAR_TOLERANCE = 8.0
AVATAR_URL = "https://avatars.githubusercontent.com/jedarden"
LIVE_OG_URL = "https://jedarden.com/brand/og.jpg"
FORGEJO_API_URL = "https://git.ardenone.com/api/v1"
FORGEJO_REPOSITORY = "jedarden/brand-kit"

def _relative_path(value, label):
    """Validate a path stored in the registry or provenance metadata."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must stay within the consumer checkout: {value}")
    return value


def _validate_assets(
    consumer_id, assets, destinations, supported_transforms, require_nonempty
):
    if require_nonempty and not assets:
        raise ValueError(f"consumer {consumer_id!r} has no assets")
    for index, asset in enumerate(assets):
        if not isinstance(asset, dict):
            raise ValueError(f"consumer {consumer_id!r} asset {index} must be an object")
        destination = _relative_path(asset.get("path"), f"asset {index} path")
        _relative_path(asset.get("source"), f"asset {index} source")
        if not isinstance(asset.get("description"), str) or not asset["description"]:
            raise ValueError(f"asset {destination} must declare a description")
        if destination in destinations:
            raise ValueError(f"consumer {consumer_id!r} registers {destination} more than once")
        destinations.add(destination)
        transform = asset.get("transform")
        if not isinstance(transform, dict) or not isinstance(transform.get("type"), str):
            raise ValueError(f"asset {destination} must declare transform.type")
        transform_type = transform["type"]
        if transform_type not in supported_transforms:
            raise ValueError(f"asset {destination} has unsupported transform {transform_type!r}")
        missing = [
            field for field in supported_transforms[transform_type] if field not in transform
        ]
        if missing:
            raise ValueError(
                f"transform for {destination} is missing: {', '.join(missing)}"
            )

        if transform_type == "jpeg-crop":
            try:
                width = int(transform["width"])
                height = int(transform["height"])
                crop_y = float(transform["crop_y"])
                quality = int(transform["quality"])
                tolerance = float(transform["tolerance"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"transform for {destination} has invalid numeric values") from exc
            if width <= 0 or height <= 0 or not 0 <= crop_y <= 1:
                raise ValueError(f"transform for {destination} has invalid crop dimensions")
            if not 1 <= quality <= 100 or tolerance < 0:
                raise ValueError(f"transform for {destination} has invalid JPEG settings")


def load_consumer(consumer_id, root=None):
    """Load and validate one consumer registration from the repository registry."""
    root = ROOT if root is None else Path(root)
    registry_path = root / REGISTRY_RELPATH
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"consumer registry not found: {registry_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid consumer registry {registry_path}: {exc}") from exc

    if not isinstance(registry, dict):
        raise ValueError(f"{REGISTRY_RELPATH} must contain a JSON object")
    if registry.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise ValueError(
            f"{REGISTRY_RELPATH} schema_version must be {REGISTRY_SCHEMA_VERSION}"
        )
    repository = registry.get("repository")
    if not isinstance(repository, str) or not repository:
        raise ValueError(f"{REGISTRY_RELPATH} repository must be a non-empty string")
    consumers = registry.get("consumers")
    if not isinstance(consumers, dict) or consumer_id not in consumers:
        choices = ", ".join(sorted(consumers)) if isinstance(consumers, dict) else "none"
        raise ValueError(f"unknown consumer {consumer_id!r}; registered: {choices}")

    config = consumers[consumer_id]
    if not isinstance(config, dict):
        raise ValueError(f"consumer {consumer_id!r} must be an object")
    for field in ("checkout", "provenance"):
        if not isinstance(config.get(field), str) or not config[field]:
            raise ValueError(f"consumer {consumer_id!r} {field} must be a non-empty string")
    provenance = _relative_path(config["provenance"], "provenance")

    assets = config.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ValueError(f"consumer {consumer_id!r} assets must be a non-empty list")
    destinations = set()
    _validate_assets(
        consumer_id,
        assets,
        destinations,
        {
            "copy": (),
            "jpeg-crop": ("width", "height", "crop_y", "quality", "tolerance"),
        },
        require_nonempty=True,
    )
    related_assets = config.get("related_assets", [])
    if not isinstance(related_assets, list):
        raise ValueError(f"consumer {consumer_id!r} related_assets must be a list")
    _validate_assets(
        consumer_id,
        related_assets,
        destinations,
        {"consumer-generated": ()},
        require_nonempty=False,
    )
    if provenance in destinations:
        raise ValueError(f"consumer {consumer_id!r} provenance path is also a registered asset")

    live_checks = config.get("live_checks", {})
    if not isinstance(live_checks, dict):
        raise ValueError(f"consumer {consumer_id!r} live_checks must be an object")
    for name, check in live_checks.items():
        if not isinstance(check, dict):
            raise ValueError(f"consumer {consumer_id!r} live check {name!r} must be an object")
        if name == "github_avatar":
            _relative_path(check.get("source"), f"live check {name} source")
        elif name == "live_og":
            _relative_path(check.get("path"), f"live check {name} path")
        if not isinstance(check.get("url"), str) or not check["url"]:
            raise ValueError(f"live check {name!r} must declare a URL")
        try:
            tolerance = float(check.get("tolerance"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"live check {name!r} has invalid tolerance") from exc
        if tolerance < 0:
            raise ValueError(f"live check {name!r} has invalid tolerance")
    return config


def copy_assets(config):
    return [
        (asset["source"], asset["path"], asset["description"])
        for asset in config["assets"]
        if asset["transform"]["type"] == "copy"
    ]


def hero_derivatives(config):
    return [
        (
            asset["transform"]["width"],
            asset["transform"]["height"],
            asset["transform"]["crop_y"],
            asset["path"],
            asset["description"],
        )
        for asset in config["assets"]
        if asset["transform"]["type"] == "jpeg-crop"
    ]


def registered_assets(config):
    return config["assets"] + config.get("related_assets", [])


def load_registry(root=None):
    root = ROOT if root is None else Path(root)
    try:
        registry = json.loads((root / REGISTRY_RELPATH).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load {REGISTRY_RELPATH}: {exc}") from exc
    if not isinstance(registry, dict):
        raise ValueError(f"{REGISTRY_RELPATH} must contain a JSON object")
    return registry


REGISTRY = load_registry()
DEFAULT_CONFIG = load_consumer(DEFAULT_CONSUMER)
COPIES = copy_assets(DEFAULT_CONFIG)
HERO_DERIVATIVES = hero_derivatives(DEFAULT_CONFIG)
_JPEG_SETTINGS = {
    (
        asset["transform"]["quality"],
        asset["transform"]["tolerance"],
    )
    for asset in DEFAULT_CONFIG["assets"]
    if asset["transform"]["type"] == "jpeg-crop"
}
if len(_JPEG_SETTINGS) != 1:
    raise ValueError("all registered jpeg-crop assets must share quality and tolerance")
JPEG_QUALITY, JPEG_TOLERANCE = next(iter(_JPEG_SETTINGS))
DEFAULT_SITE = Path(DEFAULT_CONFIG["checkout"]).expanduser()


def hero_crop(tw, th, fy):
    """Same scale-to-cover crop build_assets.py uses for banners (fx=0.5)."""
    hero = Image.open(ROOT / "source/hero.png").convert("RGB")
    scale = max(tw / hero.width, th / hero.height)
    im = hero.resize((round(hero.width * scale), round(hero.height * scale)), Image.LANCZOS)
    left, top = round((im.width - tw) / 2), round((im.height - th) * fy)
    return im.crop((left, top, left + tw, top + th))


def mean_luma_diff(a, b):
    d = ImageChops.difference(a, b)
    return ImageStat.Stat(d.convert("L")).mean[0]


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def brand_kit_reference(repository, release=None):
    """Return the immutable identity to write into a consumer manifest."""
    try:
        commit_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError(f"cannot record brand-kit commit: {exc}") from exc
    if commit_result.returncode != 0 or not commit_result.stdout.strip():
        detail = commit_result.stderr.strip() or "git rev-parse HEAD failed"
        raise RuntimeError(f"cannot record brand-kit commit: {detail}")

    if release is None:
        try:
            release_result = subprocess.run(
                ["git", "describe", "--tags", "--exact-match", "HEAD"],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise RuntimeError(f"cannot determine the brand-kit release: {exc}") from exc
        release = release_result.stdout.strip() if release_result.returncode == 0 else None
    return {
        "repository": repository,
        "commit": commit_result.stdout.strip(),
        "release": release or None,
    }


def provenance_manifest(site, consumer_id, config, reference, asset_paths=None):
    """Build provenance using staged paths where an apply transaction provided them."""
    asset_paths = {} if asset_paths is None else asset_paths
    assets = {}
    for asset in registered_assets(config):
        destination = asset_paths.get(asset["path"], site / asset["path"])
        if not destination.is_file():
            raise FileNotFoundError(destination)
        assets[asset["path"]] = {
            "source": asset["source"],
            "transform": asset["transform"],
            "sha256": sha256_file(destination),
        }
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "consumer": consumer_id,
        "brand_kit": reference,
        "assets": assets,
    }


def provenance_label(reference):
    if not isinstance(reference, dict):
        return "<missing>"
    return reference.get("release") or reference.get("commit") or "<missing>"


def check_provenance(site, consumer_id, config, reference, apply):
    """Create or validate the consumer's exact release/commit manifest."""
    path = site / config["provenance"]
    if not apply and not path.is_file():
        print(f"STALE consumer provenance: {config['provenance']} is missing; run --apply")
        return False

    try:
        expected = provenance_manifest(site, consumer_id, config, reference)
    except FileNotFoundError as exc:
        print(f"FAIL  consumer provenance: cannot record missing asset {exc}")
        return False

    payload = json.dumps(expected, indent=2, sort_keys=True) + "\n"
    if apply:
        try:
            current = path.read_text(encoding="utf-8") if path.is_file() else None
        except UnicodeDecodeError:
            current = None
        path.parent.mkdir(parents=True, exist_ok=True)
        if current == payload:
            print(
                f"PASS  consumer provenance: {config['provenance']} records "
                f"brand-kit @{provenance_label(reference)}"
            )
        else:
            path.write_text(payload, encoding="utf-8")
            print(
                f"SYNC  consumer provenance: {config['provenance']} records "
                f"brand-kit @{provenance_label(reference)}"
            )
        return True

    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(
            f"STALE consumer provenance: {config['provenance']} is invalid JSON ({exc}); "
            "run --apply"
        )
        return False

    if current == expected:
        print(
            f"PASS  consumer provenance: {config['provenance']} records "
            f"brand-kit @{provenance_label(reference)} with all asset digests"
        )
        return True

    current_reference = current.get("brand_kit") if isinstance(current, dict) else None
    if current_reference != reference:
        print(
            "STALE consumer provenance: brand-kit reference differs; expected "
            f"{provenance_label(reference)} ({reference['commit']}), recorded "
            f"{provenance_label(current_reference)}; run --apply"
        )
    else:
        print(
            f"STALE consumer provenance: manifest or asset digest differs at "
            f"brand-kit @{provenance_label(reference)}; run --apply"
        )
    return False


def check_copies(site, apply, copies=None):
    """Logo assets must be byte-identical — the honest staleness test.

    Pixels can match while bytes differ (e.g. this repo's oxipng pass), and a
    byte-differing copy means the consumer did not come from the tagged
    release, which is exactly what this workflow exists to catch.
    """
    ok = True
    for repo_rel, site_rel, desc in COPIES if copies is None else copies:
        repo_f, site_f = ROOT / repo_rel, site / site_rel
        if not site_f.exists():
            print(f"FAIL  {site_rel}: missing (expected copy of {repo_rel})")
            ok = False
            continue
        same = repo_f.read_bytes() == site_f.read_bytes()
        if same:
            print(f"PASS  {site_rel}: byte-identical to {repo_rel}")
        elif apply:
            site_f.write_bytes(repo_f.read_bytes())
            print(f"SYNC  {site_rel}: refreshed from {repo_rel} (was byte-different)")
        else:
            print(f"STALE {site_rel}: differs from {repo_rel} "
                  f"({site_f.stat().st_size} vs {repo_f.stat().st_size} bytes) — run --apply")
            ok = False
    return ok


def check_hero_derivatives(site, apply, derivatives=None, config=None):
    ok = True
    if derivatives is None:
        derivatives = HERO_DERIVATIVES
        settings = None
    else:
        config = DEFAULT_CONFIG if config is None else config
        settings = {
            asset["path"]: (
                asset["transform"]["quality"],
                asset["transform"]["tolerance"],
            )
            for asset in config["assets"]
            if asset["transform"]["type"] == "jpeg-crop"
        }
    for tw, th, fy, site_rel, desc in derivatives:
        quality, tolerance = (
            (JPEG_QUALITY, JPEG_TOLERANCE)
            if settings is None
            else settings[site_rel]
        )
        site_f = site / site_rel
        fresh = hero_crop(tw, th, fy)
        if not site_f.exists():
            print(f"FAIL  {site_rel}: missing ({desc})")
            ok = False
            continue
        diff = mean_luma_diff(Image.open(site_f).convert("RGB"), fresh)
        if diff <= tolerance:
            print(f"PASS  {site_rel}: matches source/hero.png crop "
                  f"({tw}x{th}, fy={fy}, mean diff {diff:.2f})")
        elif apply:
            fresh.save(site_f, "JPEG", quality=quality)
            print(f"SYNC  {site_rel}: regenerated from source/hero.png "
                  f"({tw}x{th}, quality {quality}; was off by {diff:.2f})")
        else:
            print(f"STALE {site_rel}: mean luma diff {diff:.2f} vs source/hero.png "
                  f"(tolerance {tolerance}) — run --apply")
            ok = False
    return ok


def _stage_path(stage_root, relative):
    staged = stage_root / relative
    staged.parent.mkdir(parents=True, exist_ok=True)
    return staged


def _stage_copy_assets(site, copies, stage_root, changes):
    """Validate and stage byte copies without touching the consumer checkout."""
    ok = True
    for repo_rel, site_rel, desc in copies:
        repo_f, site_f = ROOT / repo_rel, site / site_rel
        if not site_f.is_file():
            print(f"FAIL  {site_rel}: missing (expected copy of {repo_rel})")
            ok = False
            continue
        try:
            source = repo_f.read_bytes()
            current = site_f.read_bytes()
        except OSError as exc:
            print(f"FAIL  {site_rel}: cannot read copy inputs ({exc})")
            ok = False
            continue
        if source == current:
            print(f"PASS  {site_rel}: byte-identical to {repo_rel}")
            continue
        try:
            staged = _stage_path(stage_root, site_rel)
            staged.write_bytes(source)
            if staged.read_bytes() != source:
                raise OSError("staged copy does not match its source")
        except Exception as exc:
            print(f"FAIL  {site_rel}: could not stage copy of {repo_rel} ({exc})")
            ok = False
            continue
        changes[site_rel] = staged
        print(f"SYNC  {site_rel}: staged refresh from {repo_rel} (was byte-different)")
    return ok


def _stage_hero_derivatives(site, derivatives, config, stage_root, changes):
    """Validate and stage JPEG conversions without touching the consumer checkout."""
    settings = {
        asset["path"]: (
            asset["transform"]["quality"],
            asset["transform"]["tolerance"],
        )
        for asset in config["assets"]
        if asset["transform"]["type"] == "jpeg-crop"
    }
    ok = True
    for tw, th, fy, site_rel, desc in derivatives:
        quality, tolerance = settings[site_rel]
        site_f = site / site_rel
        if not site_f.is_file():
            print(f"FAIL  {site_rel}: missing ({desc})")
            ok = False
            continue
        fresh = None
        try:
            with Image.open(site_f) as current_image:
                current = current_image.convert("RGB")
            fresh = hero_crop(tw, th, fy)
            diff = mean_luma_diff(current, fresh)
            if diff <= tolerance:
                print(f"PASS  {site_rel}: matches source/hero.png crop "
                      f"({tw}x{th}, fy={fy}, mean diff {diff:.2f})")
                continue
            staged = _stage_path(stage_root, site_rel)
            fresh.save(staged, "JPEG", quality=quality)
            with Image.open(staged) as staged_image:
                staged_decoded = staged_image.convert("RGB")
            if staged_decoded.size != fresh.size:
                raise OSError("staged JPEG has unexpected dimensions")
            staged_diff = mean_luma_diff(staged_decoded, fresh)
            if staged_diff > tolerance:
                raise OSError(
                    f"staged JPEG mean luma diff {staged_diff:.2f} exceeds "
                    f"tolerance {tolerance}"
                )
        except Exception as exc:
            print(f"FAIL  {site_rel}: could not stage JPEG conversion ({exc})")
            ok = False
            continue
        finally:
            if fresh is not None:
                fresh.close()
        changes[site_rel] = staged
        print(f"SYNC  {site_rel}: staged regeneration from source/hero.png "
              f"({tw}x{th}, quality {quality}; was off by {diff:.2f})")
    return ok


def _stage_provenance(site, consumer_id, config, reference, stage_root, changes):
    """Stage a manifest that hashes the final staged and consumer-generated files."""
    path = site / config["provenance"]
    if path.exists() and not path.is_file():
        print(f"FAIL  consumer provenance: destination is not a file: {config['provenance']}")
        return False
    staged_assets = {
        relative: staged
        for relative, staged in changes.items()
        if relative in {asset["path"] for asset in config["assets"]}
    }
    try:
        expected = provenance_manifest(
            site, consumer_id, config, reference, asset_paths=staged_assets
        )
    except FileNotFoundError as exc:
        print(f"FAIL  consumer provenance: cannot record missing asset {exc}")
        return False
    payload = json.dumps(expected, indent=2, sort_keys=True) + "\n"
    try:
        current = path.read_text(encoding="utf-8") if path.is_file() else None
    except (OSError, UnicodeDecodeError):
        current = None
    if current == payload:
        print(
            f"PASS  consumer provenance: {config['provenance']} records "
            f"brand-kit @{provenance_label(reference)}"
        )
        return True
    try:
        staged = _stage_path(stage_root, config["provenance"])
        staged.write_text(payload, encoding="utf-8")
        if staged.read_text(encoding="utf-8") != payload:
            raise OSError("staged manifest does not match its expected contents")
    except Exception as exc:
        print(f"FAIL  consumer provenance: could not stage manifest ({exc})")
        return False
    changes[config["provenance"]] = staged
    print(
        f"SYNC  consumer provenance: staged {config['provenance']} for "
        f"brand-kit @{provenance_label(reference)}"
    )
    return True


def _ensure_parent(path, created_directories):
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir()
        created_directories.append(directory)


def _remove_created_directories(created_directories):
    for directory in reversed(created_directories):
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            pass


def _install_staged(site, changes, transaction_root):
    """Install staged files and restore every prior file if replacement fails."""
    backups = transaction_root / "backups"
    entries = []
    created_directories = []
    installed = []
    try:
        backups.mkdir()
        for index, (relative, staged) in enumerate(changes.items()):
            destination = site / relative
            backup = None
            if destination.exists() or destination.is_symlink():
                if not destination.is_file() and not destination.is_symlink():
                    raise OSError(f"destination is not a file: {relative}")
                backup = backups / str(index)
                shutil.copy2(destination, backup, follow_symlinks=False)
            _ensure_parent(destination, created_directories)
            entries.append((relative, staged, destination, backup))

        for entry in entries:
            relative, staged, destination, _ = entry
            os.replace(staged, destination)
            installed.append(entry)
    except Exception as exc:
        rollback_errors = []
        for relative, _, destination, backup in reversed(installed):
            try:
                if backup is None:
                    destination.unlink()
                else:
                    os.replace(backup, destination)
            except (FileNotFoundError, OSError) as rollback_exc:
                rollback_errors.append(f"{relative}: {rollback_exc}")
        _remove_created_directories(created_directories)
        detail = f"{exc}"
        if rollback_errors:
            detail += "; rollback failed for " + ", ".join(rollback_errors)
        else:
            detail += "; all replaced files restored"
        raise RuntimeError(detail) from exc


def apply_consumer_transaction(site, consumer_id, config, reference):
    """Stage all managed output, then commit it as one rollback-capable operation."""
    changes = {}
    with tempfile.TemporaryDirectory(
        prefix=".brand-kit-consumer-sync-", dir=site
    ) as temporary:
        stage_root = Path(temporary) / "files"
        print("registered byte copies (byte-identical required):")
        copies_ok = _stage_copy_assets(
            site, copy_assets(config), stage_root, changes
        )
        print("\nregistered JPEG derivatives (regenerated from source/hero.png):")
        derivatives_ok = _stage_hero_derivatives(
            site, hero_derivatives(config), config, stage_root, changes
        )

        print("\nconsumer provenance (release or commit plus per-file digests):")
        if copies_ok and derivatives_ok:
            provenance_ok = _stage_provenance(
                site, consumer_id, config, reference, stage_root, changes
            )
        else:
            print("FAIL  consumer provenance: not recorded until every asset check passes")
            provenance_ok = False

        if not (copies_ok and derivatives_ok and provenance_ok):
            print("ABORT consumer sync: staged files discarded; checkout unchanged")
            return False
        if not changes:
            print("PASS  consumer sync transaction: checkout already current")
            return True
        try:
            _install_staged(site, changes, Path(temporary))
        except RuntimeError as exc:
            print(f"ROLLBACK consumer sync: {exc}")
            return False
        print(f"COMMIT consumer sync: installed {len(changes)} staged file(s)")
        return True


def fetch(url, headers=None):
    return release_publish.request_bytes(
        url,
        {"User-Agent": "brand-kit-consumer-sync", **(headers or {})},
        timeout=release_publish.DEFAULT_PUBLIC_TIMEOUT_SECONDS,
        service="consumer sync endpoint",
    )


def release_tag(explicit=None):
    if explicit is not None:
        tag = explicit.strip()
        if not tag:
            raise RuntimeError("--release-tag must not be empty")
        return tag
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--exact-match", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
    except OSError as e:
        raise RuntimeError(f"cannot determine the release tag: {e}") from e
    tag = result.stdout.strip()
    if result.returncode != 0 or not tag:
        detail = result.stderr.strip()
        suffix = f" ({detail})" if detail else ""
        raise RuntimeError(
            "consumer sync requires HEAD to be checked out at the exact release tag; "
            "check out the release tag or pass --release-tag" + suffix
        )
    return tag


def forgejo_release_url(tag, api_url=None):
    base = (api_url or FORGEJO_API_URL).rstrip("/")
    encoded_tag = urllib.parse.quote(tag, safe="")
    return f"{base}/repos/{FORGEJO_REPOSITORY}/releases/tags/{encoded_tag}"


def check_forgejo_release(tag, api_url=None, token=None):
    url = forgejo_release_url(tag, api_url)
    if token is None:
        token = os.environ.get("FORGEJO_TOKEN") or os.environ.get("FORGEJO_API_TOKEN")
    headers = {"Authorization": f"token {token}"} if token else {}
    try:
        body = fetch(url, headers) if headers else fetch(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"FAIL  Forgejo release: no published release record for {tag} (HTTP 404); "
                  "publish the Forgejo Release before running consumer_sync")
        elif e.code in (401, 403):
            print(f"FAIL  Forgejo release: GET {url} returned HTTP {e.code}; "
                  "set a read-only FORGEJO_TOKEN or fix repository access")
        else:
            print(f"FAIL  Forgejo release: GET {url} returned HTTP {e.code}; refusing to sync")
        return False
    except Exception as e:
        print(f"FAIL  Forgejo release: could not read {url} ({e}); refusing to sync")
        return False

    try:
        record = json.loads(body)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as e:
        print(f"FAIL  Forgejo release: invalid JSON for {tag} ({e}); refusing to sync")
        return False
    if not isinstance(record, dict):
        print(f"FAIL  Forgejo release: response for {tag} is not an object; refusing to sync")
        return False
    if record.get("tag_name") != tag:
        print(f"FAIL  Forgejo release: response names {record.get('tag_name')!r}, "
              f"expected {tag!r}; refusing to sync")
        return False
    if record.get("draft") is not False:
        print(f"FAIL  Forgejo release: {tag} is not published (draft must be false); "
              "publish the Forgejo Release before running consumer_sync")
        return False
    print(f"PASS  Forgejo release: {tag} is published (read-only record found)")
    return True


def check_live_avatar(
    offline,
    source="avatars/github-460.png",
    url=AVATAR_URL,
    tolerance=AVATAR_TOLERANCE,
):
    """The avatar is set outside any build system, so it can only be verified, never synced."""
    if offline:
        print("SKIP  github avatar: --offline passed; the re-verify did NOT happen — "
              "re-run without --offline before declaring this step done")
        return True
    ref = Image.open(ROOT / source).convert("RGB")
    try:
        live = Image.open(io.BytesIO(fetch(url))).convert("RGB")
    except Exception as e:
        print(f"FAIL  github avatar: could not fetch {url} ({e}) — "
              "the re-verify did NOT happen; refusing to pass")
        return False
    if live.size != ref.size:
        ref = ref.resize(live.size, Image.LANCZOS)
    diff = mean_luma_diff(live, ref)
    if diff <= tolerance:
        print(f"PASS  github avatar: live avatar matches {source} "
              f"(mean diff {diff:.2f}, tolerance {tolerance} for GitHub recompression)")
        return True
    print(f"STALE github avatar: live avatar mean diff {diff:.2f} vs "
          f"{source} — upload {source} via "
          f"github.com Settings > Public profile > Edit avatar, then re-run")
    return False


def check_live_og(
    site,
    offline,
    path="public/brand/og.jpg",
    url=LIVE_OG_URL,
    tolerance=JPEG_TOLERANCE,
):
    """Catches a refreshed checkout that was never pushed, or an undeployed Pages build."""
    label = Path(path).name
    if offline:
        print(f"SKIP  live {label}: --offline passed; deploy status unverified")
        return True
    local = site / path
    try:
        live = Image.open(io.BytesIO(fetch(url))).convert("RGB")
    except Exception as e:
        print(f"FAIL  live {label}: could not fetch {url} ({e}) — "
              "deploy status unverified; refusing to pass")
        return False
    if not local.exists():
        print(f"FAIL  live og.jpg: local {local} missing to compare against")
        return False
    if live.size != Image.open(local).size:
        print(f"STALE live {label}: live size differs from checkout copy — "
              "commit + push the checkout, wait for the Cloudflare Pages build")
        return False
    diff = mean_luma_diff(live, Image.open(local).convert("RGB"))
    if diff <= tolerance:
        print(f"PASS  live {label}: site serves the checkout copy (mean diff {diff:.2f})")
        return True
    print(f"STALE live {label}: live copy differs from checkout (mean diff {diff:.2f}) — "
          "commit + push the checkout, wait for the Cloudflare Pages build")
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="verify only (default)")
    mode.add_argument("--apply", action="store_true",
                      help="refresh the registered checkout in place, then verify")
    ap.add_argument("--consumer", default=DEFAULT_CONSUMER,
                    help=f"consumer id from {REGISTRY_RELPATH} (default: {DEFAULT_CONSUMER})")
    ap.add_argument("--site", type=Path,
                    help="consumer checkout (overrides the registered default)")
    ap.add_argument("--release-tag",
                    help="release tag to verify (default: the exact tag checked out at HEAD)")
    ap.add_argument("--offline", action="store_true",
                    help="skip registered live-resource checks; the Forgejo release "
                         "gate is still required")
    args = ap.parse_args()

    try:
        config = load_consumer(args.consumer)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2

    try:
        tag = release_tag(args.release_tag)
    except RuntimeError as e:
        print(f"error: {e}")
        return 2

    if not check_forgejo_release(tag):
        print("Forgejo release gate failed; no consumer files were changed.")
        return 1

    site = args.site or Path(config["checkout"]).expanduser()
    if not site.is_dir():
        print(f"error: consumer checkout {site} does not exist — pass --site <path>")
        return 2

    try:
        reference = brand_kit_reference(REGISTRY["repository"])
    except (KeyError, RuntimeError) as exc:
        print(f"error: cannot record consumer provenance: {exc}")
        return 2

    print(f"brand-kit: {ROOT} @ {provenance_label(reference)} ({reference['commit']})")
    print(f"consumer:  {site} ({args.consumer}, mode: "
          f"{'apply' if args.apply else 'check'})\n")

    if args.apply:
        local_ok = apply_consumer_transaction(
            site, args.consumer, config, reference
        )
    else:
        local_ok = True
        copies = copy_assets(config)
        derivatives = hero_derivatives(config)
        print("registered byte copies (byte-identical required):")
        copies_ok = check_copies(site, False, copies)
        local_ok &= copies_ok
        print("\nregistered JPEG derivatives (regenerated from source/hero.png):")
        derivatives_ok = check_hero_derivatives(site, False, derivatives, config)
        local_ok &= derivatives_ok

        print("\nconsumer provenance (release or commit plus per-file digests):")
        if copies_ok and derivatives_ok:
            local_ok &= check_provenance(
                site, args.consumer, config, reference, False
            )
        else:
            print("FAIL  consumer provenance: not recorded until every asset check passes")
            local_ok = False

    live_checks = config.get("live_checks", {})
    live_ok = True
    if live_checks:
        print("\nresources set outside any build system (verify only):")
        avatar = live_checks.get("github_avatar")
        if avatar:
            if avatar == DEFAULT_CONFIG.get("live_checks", {}).get("github_avatar"):
                live_ok &= check_live_avatar(args.offline)
            else:
                live_ok &= check_live_avatar(
                    args.offline,
                    source=avatar["source"],
                    url=avatar["url"],
                    tolerance=avatar["tolerance"],
                )
        live_og = live_checks.get("live_og")
        if live_og:
            if live_og == DEFAULT_CONFIG.get("live_checks", {}).get("live_og"):
                live_ok &= check_live_og(site, args.offline)
            else:
                live_ok &= check_live_og(
                    site,
                    args.offline,
                    path=live_og["path"],
                    url=live_og["url"],
                    tolerance=live_og["tolerance"],
                )

    print()
    if args.apply and local_ok:
        quoted_tag = shlex.quote(tag)
        label = provenance_label(reference)
        commit_message = shlex.quote(f"chore(brand): sync to brand-kit @{label}")
        python = shlex.quote(sys.executable)
        paths = [asset["path"] for asset in registered_assets(config)]
        # favicon.ico is generated by the same site-owned command but is not
        # included in the provenance inventory because the drift detector's
        # documented contract covers the four web-facing outputs above.
        paths.append("public/favicon.ico")
        paths.append(config["provenance"])
        print("Applied. Finish by hand:")
        print(f"  SITE={shlex.quote(str(site))}")
        print("  (")
        print('    cd -- "$SITE" || exit')
        print("    git status                 # review the refreshed files")
        print("    node scripts/make-favicons.mjs  # if the logo changed; site-owned")
        print(f"    git add -- {shlex.join(paths)}")
        print(f"    git commit -m {commit_message}")
        print("    git push                    # Cloudflare Pages deploys on push")
        print("  )")
        print(f"  {python} {ROOT / 'tools/consumer_sync.py'} --release-tag {quoted_tag} "
              f"--consumer {shlex.quote(args.consumer)} --check --site \"$SITE\"")
        print(f"  {python} {ROOT / 'tools/consumer_drift.py'} --release-tag {quoted_tag} "
              "--site \"$SITE\"   # both must be all-CURRENT after deploy")
        return 0 if local_ok and live_ok else 1
    if args.apply:
        return 1
    ok = local_ok and live_ok
    print("All consumer copies and provenance in sync." if ok else
          "Consumer drift found — see the STALE/FAIL lines above, or the checklist in "
          "docs/notes/post-tag-consumer-update.md.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
