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

Consumers handled:
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

Modes:
  --check  verify only (default); exits 1 on any stale/mismatched consumer
  --apply  refresh the jedarden.com checkout in place (never commits), then verify

A published Forgejo release record is required before either mode runs. Set
FORGEJO_TOKEN to a read-only Forgejo API token when the Forgejo instance requires
authentication.

Examples:
  python3 tools/consumer_sync.py --check
  python3 tools/consumer_sync.py --apply --release-tag v1.0.0
  python3 tools/consumer_sync.py --check --offline   # skip live-network checks
  python3 tools/consumer_sync.py --check --site ~/src/jedarden.com
"""
import argparse
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parent.parent

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

DEFAULT_SITE = Path.home() / "jedarden.com"

# (repo file, site file, description)
COPIES = [
    ("logo/logo.svg", "public/brand/logo.svg", "vector logo"),
    ("logo/logo-512.png", "public/brand/logo-512.png", "512px logo PNG"),
]

# (width, height, fy crop bias, site file, description)
HERO_DERIVATIVES = [
    (1200, 630, 0.45, "public/brand/og.jpg", "open-graph card (social image for /brand)"),
    (1536, 1024, 0.0, "src/assets/brand-hero.jpg", "brand page hero (via Astro asset pipeline)"),
]


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


def check_copies(site, apply):
    """Logo assets must be byte-identical — the honest staleness test.

    Pixels can match while bytes differ (e.g. this repo's oxipng pass), and a
    byte-differing copy means the consumer did not come from the tagged
    release, which is exactly what this workflow exists to catch.
    """
    ok = True
    for repo_rel, site_rel, desc in COPIES:
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


def check_hero_derivatives(site, apply):
    ok = True
    for tw, th, fy, site_rel, desc in HERO_DERIVATIVES:
        site_f = site / site_rel
        fresh = hero_crop(tw, th, fy)
        if not site_f.exists():
            print(f"FAIL  {site_rel}: missing ({desc})")
            ok = False
            continue
        diff = mean_luma_diff(Image.open(site_f).convert("RGB"), fresh)
        if diff <= JPEG_TOLERANCE:
            print(f"PASS  {site_rel}: matches source/hero.png crop "
                  f"({tw}x{th}, fy={fy}, mean diff {diff:.2f})")
        elif apply:
            fresh.save(site_f, "JPEG", quality=JPEG_QUALITY)
            print(f"SYNC  {site_rel}: regenerated from source/hero.png "
                  f"({tw}x{th}, quality {JPEG_QUALITY}; was off by {diff:.2f})")
        else:
            print(f"STALE {site_rel}: mean luma diff {diff:.2f} vs source/hero.png "
                  f"(tolerance {JPEG_TOLERANCE}) — run --apply")
            ok = False
    return ok


def fetch(url, headers=None):
    request_headers = {"User-Agent": "brand-kit-consumer-sync"}
    request_headers.update(headers or {})
    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


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


def check_live_avatar(offline):
    """The avatar is set outside any build system, so it can only be verified, never synced."""
    if offline:
        print("SKIP  github avatar: --offline passed; the re-verify did NOT happen — "
              "re-run without --offline before declaring this step done")
        return True
    ref = Image.open(ROOT / "avatars/github-460.png").convert("RGB")
    try:
        live = Image.open(io.BytesIO(fetch(AVATAR_URL))).convert("RGB")
    except Exception as e:
        print(f"SKIP  github avatar: could not fetch {AVATAR_URL} ({e}) — "
              "the re-verify did NOT happen; re-run when online")
        return True
    if live.size != ref.size:
        ref = ref.resize(live.size, Image.LANCZOS)
    diff = mean_luma_diff(live, ref)
    if diff <= AVATAR_TOLERANCE:
        print(f"PASS  github avatar: live avatar matches avatars/github-460.png "
              f"(mean diff {diff:.2f}, tolerance {AVATAR_TOLERANCE} for GitHub recompression)")
        return True
    print(f"STALE github avatar: live avatar mean diff {diff:.2f} vs "
          f"avatars/github-460.png — upload avatars/github-460.png via "
          f"github.com Settings > Public profile > Edit avatar, then re-run")
    return False


def check_live_og(site, offline):
    """Catches a refreshed checkout that was never pushed, or an undeployed Pages build."""
    if offline:
        print("SKIP  live og.jpg: --offline passed; deploy status unverified")
        return True
    local = site / "public/brand/og.jpg"
    try:
        live = Image.open(io.BytesIO(fetch(LIVE_OG_URL))).convert("RGB")
    except Exception as e:
        print(f"SKIP  live og.jpg: could not fetch {LIVE_OG_URL} ({e}) — "
              "deploy status unverified; re-run when online")
        return True
    if not local.exists():
        print(f"FAIL  live og.jpg: local {local} missing to compare against")
        return False
    if live.size != Image.open(local).size:
        print("STALE live og.jpg: live size differs from checkout copy — "
              "commit + push the checkout, wait for the Cloudflare Pages build")
        return False
    diff = mean_luma_diff(live, Image.open(local).convert("RGB"))
    if diff <= JPEG_TOLERANCE:
        print(f"PASS  live og.jpg: jedarden.com serves the checkout copy (mean diff {diff:.2f})")
        return True
    print(f"STALE live og.jpg: live copy differs from checkout (mean diff {diff:.2f}) — "
          "commit + push the checkout, wait for the Cloudflare Pages build")
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="verify only (default)")
    mode.add_argument("--apply", action="store_true",
                      help="refresh the jedarden.com checkout in place, then verify")
    ap.add_argument("--site", type=Path, default=DEFAULT_SITE,
                    help=f"jedarden.com checkout (default {DEFAULT_SITE})")
    ap.add_argument("--release-tag",
                    help="release tag to verify (default: the exact tag checked out at HEAD)")
    ap.add_argument("--offline", action="store_true",
                    help="skip the live github-avatar and live og.jpg checks; the "
                         "Forgejo release gate is still required")
    args = ap.parse_args()

    try:
        tag = release_tag(args.release_tag)
    except RuntimeError as e:
        print(f"error: {e}")
        return 2

    if not check_forgejo_release(tag):
        print("Forgejo release gate failed; no consumer files were changed.")
        return 1

    if not (args.site / "public/brand").is_dir():
        print(f"error: {args.site} does not look like a jedarden.com checkout "
              f"(no public/brand/) — pass --site <path>")
        return 2

    print(f"brand-kit: {ROOT} @ {tag}")
    print(f"consumer:  {args.site} (mode: {'apply' if args.apply else 'check'})\n")

    ok = True
    print("logo copies (byte-identical required):")
    ok &= check_copies(args.site, args.apply)
    print("\nhero derivatives (regenerated from source/hero.png):")
    ok &= check_hero_derivatives(args.site, args.apply)
    print("\nresources set outside any build system (verify only):")
    ok &= check_live_avatar(args.offline)
    ok &= check_live_og(args.site, args.offline)

    print()
    if args.apply:
        print("Applied. Finish by hand:")
        print(f"  cd {args.site} && git status            # review the refreshed files")
        print(f"  git add public/brand src/assets && git commit -m "
              f"'chore(brand): sync to brand-kit @{tag}'")
        print("  git push                                # Cloudflare Pages deploys on push")
        print(f"  python3 {ROOT / 'tools/consumer_sync.py'} --release-tag {tag} --check   # must be all-PASS after deploy")
        return 0
    print("All consumer copies in sync." if ok else
          "Consumer drift found — see the STALE/FAIL lines above, or the checklist in "
          "docs/notes/post-tag-consumer-update.md.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
