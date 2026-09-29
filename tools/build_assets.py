#!/usr/bin/env python3
"""Regenerate every platform asset in the brand kit from the canonical sources.

Sources:
  source/logo.svg             -- authoritative opaque vector logo.
  source/logo-transparent.svg -- independently maintained transparent logo.
  source/hero.png             -- authoritative banner/cover raster.
  source/logo.png             -- original raster copied to logo-original.png.

The generated ``platform-assets.json`` manifest records the platform role,
path, dimensions, authoritative source for each upload-ready asset, and the
external requirement source and date used to verify each platform target.

Profile pictures + favicons come from the opaque SVG; transparent logo assets
come from the transparent SVG; banners/covers come from the hero.

Requires the PINNED toolchain recorded in docs/notes/asset-toolchain.md:
resvg 0.47.0 on PATH and the Pillow 12.1.1 PyPI wheel in `.venv`. There is
deliberately no raster fallback — output bytes depend on the exact tool versions
(and for Pillow, the wheel build), so a missing tool aborts the build instead of
silently producing bytes the CI regen-diff would reject. Only an explicit
raster-to-vector replacement uses the pinned vtracer 0.6.5 CLI via
`.venv/bin/python tools/trace_logo.py`.

Run:  .venv/bin/python tools/build_assets.py
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
PLATFORM_MANIFEST_RELPATH = "platform-assets.json"
PLATFORM_MANIFEST_SCHEMA_VERSION = 4
PLATFORM_UPLOAD_CONSTRAINTS = {
    "formats": ["PNG", "ICO"],
    "color_mode": "RGB",
    "alpha": "forbidden",
    "max_file_size_bytes": 5 * 1024 * 1024,
}
PALETTE = {
    "Polo Red": "#DC3127",
    "Ink": "#0A0A08",
    "Canvas Cream": "#EFDECC",
    "Skin Tan": "#F5B079",
    "Control-Room Black": "#070506",
}
SRC = ROOT / "source"
LOGO_SVG = SRC / "logo.svg"
LOGO_TRANSPARENT_SVG = SRC / "logo-transparent.svg"
LOGO_PNG = SRC / "logo.png"
HERO = SRC / "hero.png"
REQUIRED_SOURCES = (LOGO_SVG, LOGO_TRANSPARENT_SVG, HERO, LOGO_PNG)


def resolve_toolchain():
    resvg = shutil.which("resvg")
    if resvg is None:
        raise SystemExit(
            "error: resvg not found on PATH. There is no raster fallback: resizing\n"
            "source/logo.png would silently produce different bytes than the vector\n"
            "renders committed in this repo and fail the CI regen-diff. Install the\n"
            "pinned toolchain (see docs/notes/asset-toolchain.md) and re-run."
        )
    for required in REQUIRED_SOURCES:
        if not required.exists():
            raise SystemExit(
                f"error: required source {required.name} not found in {SRC}. All\n"
                "sources must be present — there is no fallback path (see\n"
                "docs/notes/asset-toolchain.md)."
            )
    return resvg


def save(img, relpath):
    out = ROOT / relpath
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, optimize=True)
    print(f"  {relpath}: {img.size[0]}x{img.size[1]}")


def save_palette():
    out = ROOT / "palette.json"
    out.write_text(json.dumps(PALETTE, indent=2) + "\n", encoding="utf-8")
    print(f"  palette.json: {len(PALETTE)} colors")


def logo_at(size, resvg):
    """Render the vector logo crisply at size x size."""
    with tempfile.NamedTemporaryFile(suffix=".png") as tf:
        subprocess.run(
            [resvg, "--width", str(size), "--height", str(size),
             str(LOGO_SVG), tf.name],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return Image.open(tf.name).convert("RGB")


def logo_at_transparent(size, resvg):
    """Render the transparent logo (no background) at size x size."""
    with tempfile.NamedTemporaryFile(suffix=".png") as tf:
        subprocess.run(
            [resvg, "--width", str(size), "--height", str(size),
             str(LOGO_TRANSPARENT_SVG), tf.name],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Keep RGBA mode for transparency
        return Image.open(tf.name).convert("RGBA")


def cover(img, tw, th, fy=0.45, fx=0.5):
    """Scale-to-cover then crop to (tw,th), biased vertically by fy (0=top,1=bottom)."""
    iw, ih = img.size
    scale = max(tw / iw, th / ih)
    nw, nh = round(iw * scale), round(ih * scale)
    im = img.resize((nw, nh), Image.LANCZOS)
    left = round((nw - tw) * fx)
    top = round((nh - th) * fy)
    return im.crop((left, top, left + tw, top + th))


def save_banners(hero):
    for path, (width, height, fy) in BANNERS.items():
        save(cover(hero, width, height, fy=fy), path)


# ---- Profile pictures (from logo) -------------------------------------------
AVATARS = {
    "avatars/x-400.png": 400,
    "avatars/linkedin-400.png": 400,
    "avatars/github-460.png": 460,
    "avatars/instagram-320.png": 320,
    "avatars/facebook-320.png": 320,
    "avatars/youtube-800.png": 800,
    "avatars/tiktok-200.png": 200,
    "avatars/mastodon-400.png": 400,
    "avatars/bluesky-400.png": 400,
    "avatars/threads-320.png": 320,
    "avatars/discord-512.png": 512,
}

# ---- Banners / covers (from hero) -------------------------------------------
# fy biases the crop band; lower = higher in frame (favours monitors + head).
BANNERS = {
    "banners/x-header-1500x500.png": (1500, 500, 0.42),
    "banners/linkedin-personal-1584x396.png": (1584, 396, 0.42),
    "banners/linkedin-company-1128x191.png": (1128, 191, 0.42),
    "banners/mastodon-header-1500x500.png": (1500, 500, 0.42),
    "banners/facebook-cover-851x315.png": (851, 315, 0.42),
    "banners/facebook-cover-2x-1702x630.png": (1702, 630, 0.42),
    "banners/youtube-banner-2560x1440.png": (2560, 1440, 0.45),
    "banners/discord-banner-960x540.png": (960, 540, 0.45),
    "banners/github-social-1280x640.png": (1280, 640, 0.45),
    "banners/open-graph-1200x630.png": (1200, 630, 0.45),
    "banners/twitter-card-1200x628.png": (1200, 628, 0.45),
}

# ---- Logo masters & favicons (from logo) ------------------------------------
LOGO_SIZES = {
    "logo/logo-1024.png": 1024,
    "logo/logo-512.png": 512,
    "logo/logo-256.png": 256,
}
FAVICON_SIZES = {
    "favicon/favicon-16.png": 16,
    "favicon/favicon-32.png": 32,
    "favicon/favicon-48.png": 48,
    "favicon/favicon-192.png": 192,
    "favicon/favicon-512.png": 512,
    "favicon/apple-touch-icon-180.png": 180,
}
FAVICON_ICO_SIZES = ((16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256))


def _manifest_dimensions(path):
    """Return the dimensions recorded for one generated platform asset."""
    if path in AVATARS:
        size = AVATARS[path]
        return {"width": size, "height": size}
    if path in BANNERS:
        width, height, _ = BANNERS[path]
        return {"width": width, "height": height}
    if path in FAVICON_SIZES:
        size = FAVICON_SIZES[path]
        return {"width": size, "height": size}
    if path == "favicon/favicon.ico":
        return {
            "width": 256,
            "height": 256,
            "sizes": [
                {"width": width, "height": height}
                for width, height in FAVICON_ICO_SIZES
            ],
        }
    raise ValueError(f"unknown platform asset path: {path}")


def _manifest_asset(platform, role, path, source):
    return {
        "platform": platform,
        "role": role,
        "path": path,
        "dimensions": _manifest_dimensions(path),
        "source": source,
    }


# These are review inputs, not generated dimensions.  Each URL must be opened
# when its platform target is changed; the freshness checker makes an overdue
# review visible without making CI depend on live external sites.
PLATFORM_REQUIREMENTS = [
    {
        "platform": "X / Twitter",
        "source_url": "https://help.x.com/en/managing-your-account/common-issues-when-uploading-profile-photo",
        "last_verified": "2026-09-28",
        "source_content_sha256": None,
    },
    {
        "platform": "LinkedIn (personal)",
        "source_url": "https://www.linkedin.com/help/linkedin/answer/a549049",
        "last_verified": "2026-09-28",
        "source_content_sha256": "7a5fdf78ddd0c7333d815963b43bca8834d6d842a75d4c67f3b3244e1c7558ab",
    },
    {
        "platform": "LinkedIn (company)",
        "source_url": "https://www.linkedin.com/help/linkedin/answer/a417335",
        "last_verified": "2026-09-28",
        "source_content_sha256": "86b64ea231db64f5217330cca6d1274a541e309baa90e97c44e25659dfc3a8bc",
    },
    {
        "platform": "GitHub",
        "source_url": "https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview",
        "last_verified": "2026-09-28",
        "source_content_sha256": "3fee048e3f3bfc61210601b7f95d69fb29bbc67b10f65fc6a4753eaf5fc1103d",
    },
    {
        "platform": "Instagram",
        "source_url": "https://help.instagram.com/",
        "last_verified": "2026-09-28",
        "source_content_sha256": "a78cebc44214256ba361dcee3f1cc1408e55ea6940c65067615abd4fdcc2d788",
    },
    {
        "platform": "Threads",
        "source_url": "https://help.instagram.com/",
        "last_verified": "2026-09-28",
        "source_content_sha256": "a78cebc44214256ba361dcee3f1cc1408e55ea6940c65067615abd4fdcc2d788",
    },
    {
        "platform": "Facebook",
        "source_url": "https://www.facebook.com/help/163248423739693",
        "last_verified": "2026-09-28",
        "source_content_sha256": "1f41aee803daa2965f2f601c1829f28623c4c55737be67c090da211247bc9149",
    },
    {
        "platform": "YouTube",
        "source_url": "https://support.google.com/youtube/answer/10456525?hl=en",
        "last_verified": "2026-09-28",
        "source_content_sha256": "b0f17488973bc0fbfc97bedb8e11a217165d9f537146d3d4f8bc72f2501554c1",
    },
    {
        "platform": "TikTok",
        "source_url": "https://support.tiktok.com/en/getting-started/setting-up-your-profile/editing-your-profile",
        "last_verified": "2026-09-28",
        "source_content_sha256": "c3dd7b78df5d94e0790c66f708029445c1560b603fb43a7bebe280c989589ef2",
    },
    {
        "platform": "Mastodon",
        "source_url": "https://docs.joinmastodon.org/user/profile/",
        "last_verified": "2026-09-28",
        "source_content_sha256": "94f98d792eb6e4a3b72aea49557b22a304ae4f0298da7c306a8df7838e6a23fa",
    },
    {
        "platform": "Bluesky",
        "source_url": "https://docs.bsky.app/docs/api/app-bsky-actor-profile",
        "last_verified": "2026-09-28",
        "source_content_sha256": "38fe12958dd10cbeb39a5d1fc38780ce8139696ae903ecdd7cd75625a20b0c77",
    },
    {
        "platform": "Discord",
        "source_url": "https://support.discord.com/hc/en-us/articles/4403147417623-Custom-Profiles",
        "last_verified": "2026-09-28",
        "source_content_sha256": "1b36a6eca32632756a2915d635ed7aaab937d5a585d2c0872e90be1b72565c32",
    },
    {
        "platform": "Web / Open Graph",
        "source_url": "https://ogp.me/",
        "last_verified": "2026-09-28",
        "source_content_sha256": "8369148db254a5e894e86380d9eb39aaa317ece52651b53562b307208891db5c",
    },
]


EVIDENCE_SOURCE_CONTENT_SHA256 = {
    "https://www.linkedin.com/help/linkedin/answer/a570368": "e374edacd71ad0bcd509e3d3f3b6ed69d43640b70310985b0f29720c390cd2bf",
    "https://docs.github.com/en/account-and-profile/reference/profile-reference": "d45f8dfbffb5b562be8580288f7e6fbe147df4bf4229b71cad389970ef2964d6",
    "https://www.facebook.com/help/193629617349922/": "b213e8dee7eb785421df56c0a49e5fce32b2e6bcfb13613d3e3916c634e232fc",
    "https://html.spec.whatwg.org/multipage/links.html#rel-icon": "c4bf3410be3b13870fa17fbf32909e201ad0f73e53f7152c48dc3d02e54f9ab4",
}
EVIDENCE_SOURCE_CONTENT_SHA256.update({
    requirement["source_url"]: requirement["source_content_sha256"]
    for requirement in PLATFORM_REQUIREMENTS
})


def _evidence(platform, role, sizes, classification, source_url, evidence):
    return [
        {
            "platform": platform,
            "role": role,
            "dimensions": {"width": width, "height": height},
            "classification": classification,
            "source_url": source_url,
            "source_content_sha256": EVIDENCE_SOURCE_CONTENT_SHA256.get(source_url),
            "evidence": evidence,
        }
        for width, height in sizes
    ]


PLATFORM_REQUIREMENT_EVIDENCE = []
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "X / Twitter", "profile_picture", [(400, 400)], "recommendation",
    "https://help.x.com/en/managing-your-account/common-issues-when-uploading-profile-photo",
    "The first-party guidance labels 400×400 px as the recommended profile image size.",
)
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "X / Twitter", "banner", [(1500, 500)], "recommendation",
    "https://help.x.com/en/managing-your-account/common-issues-when-uploading-profile-photo",
    "The first-party guidance labels 1500×500 px as the recommended header image size.",
)
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "LinkedIn (personal)", "profile_picture", [(400, 400)], "upload_minimum",
    "https://www.linkedin.com/help/linkedin/answer/a549049",
    "The source accepts profile photos from 400×400 px; this export meets the stated minimum.",
)
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "LinkedIn (personal)", "banner", [(1584, 396)], "recommendation",
    "https://www.linkedin.com/help/linkedin/answer/a549049",
    "The source recommends a 1584×396 px background photo.",
)
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "LinkedIn (company)", "profile_picture", [(400, 400)], "recommendation",
    "https://www.linkedin.com/help/linkedin/answer/a570368",
    "The Page logo table gives 268×268 px as minimum and 400×400 px as recommended.",
)
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "LinkedIn (company)", "banner", [(1128, 191)], "specified_size",
    "https://www.linkedin.com/help/linkedin/answer/a417335",
    "The Landing Pages table specifies a 1128×191 px cover image.",
)
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "GitHub", "profile_picture", [(460, 460)], "within_limits",
    "https://docs.github.com/en/account-and-profile/reference/profile-reference",
    "The profile guide caps images below 3000×3000 px and recommends about 500×500; 460×460 is the selected square export within the cap.",
)
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "GitHub", "banner", [(1280, 640)], "specified_size",
    "https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview",
    "The repository social-preview guidance specifies the 1280×640 px image target.",
)
for platform in ("Instagram", "Threads"):
    PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
        platform, "profile_picture", [(320, 320)], "project_choice",
        "https://help.instagram.com/",
        "The linked Help Center landing page states no numeric avatar dimensions; 320×320 is the kit's square export choice.",
    )
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "Facebook", "profile_picture", [(320, 320)], "upload_minimum",
    "https://www.facebook.com/help/163248423739693",
    "Facebook's profile-photo guidance says at least 320×320 px for best quality.",
)
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "Facebook", "banner", [(851, 315), (1702, 630)], "project_choice",
    "https://www.facebook.com/help/193629617349922/",
    "The linked help article covers profile and cover photos but publishes no cover dimensions; the 851×315 export and its 2× derivative are kit choices.",
)
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "YouTube", "profile_picture", [(800, 800)], "project_choice",
    "https://support.google.com/youtube/answer/10456525?hl=en",
    "The source documents file limits and a 98×98 rendered size, not an upload dimension; 800×800 is the kit's square master.",
)
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "YouTube", "banner", [(2560, 1440)], "recommendation",
    "https://support.google.com/youtube/answer/10456525?hl=en",
    "The source lists 2048×1152 as the upload minimum and 2560×1440 as the recommended size.",
)
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "TikTok", "profile_picture", [(200, 200)], "upload_minimum",
    "https://support.tiktok.com/en/getting-started/setting-up-your-profile/editing-your-profile",
    "TikTok's profile-photo help states a 20×20 px minimum; this 200×200 px square exceeds it.",
)
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "Mastodon", "profile_picture", [(400, 400)], "specified_size",
    "https://docs.joinmastodon.org/user/profile/",
    "Mastodon downscales uploaded avatars to 400×400 px.",
)
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "Mastodon", "banner", [(1500, 500)], "specified_size",
    "https://docs.joinmastodon.org/user/profile/",
    "Mastodon downscales uploaded profile headers to 1500×500 px.",
)
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "Bluesky", "profile_picture", [(400, 400)], "project_choice",
    "https://docs.bsky.app/docs/api/app-bsky-actor-profile",
    "The profile schema exposes an avatar without specifying pixel dimensions; 400×400 is the kit's square choice.",
)
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "Bluesky", "banner", [(1200, 628)], "project_choice",
    "https://docs.bsky.app/docs/api/app-bsky-actor-profile",
    "The profile schema exposes a banner without specifying pixel dimensions; 1200×628 is a reusable kit choice.",
)
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "Discord", "profile_picture", [(512, 512)], "project_choice",
    "https://support.discord.com/hc/en-us/articles/4403147417623-Custom-Profiles",
    "The linked profile guide lists accepted avatar types but no pixel size; 512×512 is the kit's square choice.",
)
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "Discord", "banner", [(960, 540)], "upload_minimum",
    "https://support.discord.com/hc/en-us/articles/4403147417623-Custom-Profiles",
    "The profile-banner minimum is 680×240 px; the 960×540 px export exceeds both minimum dimensions.",
)
for width, height in ((16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256), (192, 192), (512, 512), (180, 180)):
    PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
        "Web / Open Graph", "favicon", [(width, height)], "project_choice",
        "https://html.spec.whatwg.org/multipage/links.html#rel-icon",
        f"The HTML icon link model does not prescribe {width}×{height}; this is a project-selected browser/device export size.",
    )
PLATFORM_REQUIREMENT_EVIDENCE += _evidence(
    "Web / Open Graph", "banner", [(1200, 630), (1200, 628)], "project_choice",
    "https://ogp.me/",
    "Open Graph defines an image URL and optional dimensions without mandating pixel dimensions; these are reusable project export sizes.",
)


# Keep this list in the same order as the README table.  It is the build
# input for platform-assets.json; verify_assets.py independently parses the
# README so a stale generated manifest cannot make prose drift invisible.
PLATFORM_ASSETS = [
    _manifest_asset("X / Twitter", "profile_picture", "avatars/x-400.png", "source/logo.svg"),
    _manifest_asset("X / Twitter", "banner", "banners/x-header-1500x500.png", "source/hero.png"),
    _manifest_asset("LinkedIn (personal)", "profile_picture", "avatars/linkedin-400.png", "source/logo.svg"),
    _manifest_asset("LinkedIn (personal)", "banner", "banners/linkedin-personal-1584x396.png", "source/hero.png"),
    _manifest_asset("LinkedIn (company)", "profile_picture", "avatars/linkedin-400.png", "source/logo.svg"),
    _manifest_asset("LinkedIn (company)", "banner", "banners/linkedin-company-1128x191.png", "source/hero.png"),
    _manifest_asset("GitHub", "profile_picture", "avatars/github-460.png", "source/logo.svg"),
    _manifest_asset("GitHub", "banner", "banners/github-social-1280x640.png", "source/hero.png"),
    _manifest_asset("Instagram", "profile_picture", "avatars/instagram-320.png", "source/logo.svg"),
    _manifest_asset("Threads", "profile_picture", "avatars/threads-320.png", "source/logo.svg"),
    _manifest_asset("Facebook", "profile_picture", "avatars/facebook-320.png", "source/logo.svg"),
    _manifest_asset("Facebook", "banner", "banners/facebook-cover-851x315.png", "source/hero.png"),
    _manifest_asset("Facebook", "banner", "banners/facebook-cover-2x-1702x630.png", "source/hero.png"),
    _manifest_asset("YouTube", "profile_picture", "avatars/youtube-800.png", "source/logo.svg"),
    _manifest_asset("YouTube", "banner", "banners/youtube-banner-2560x1440.png", "source/hero.png"),
    _manifest_asset("TikTok", "profile_picture", "avatars/tiktok-200.png", "source/logo.svg"),
    _manifest_asset("Mastodon", "profile_picture", "avatars/mastodon-400.png", "source/logo.svg"),
    _manifest_asset("Mastodon", "banner", "banners/mastodon-header-1500x500.png", "source/hero.png"),
    _manifest_asset("Bluesky", "profile_picture", "avatars/bluesky-400.png", "source/logo.svg"),
    _manifest_asset("Bluesky", "banner", "banners/twitter-card-1200x628.png", "source/hero.png"),
    _manifest_asset("Discord", "profile_picture", "avatars/discord-512.png", "source/logo.svg"),
    _manifest_asset("Discord", "banner", "banners/discord-banner-960x540.png", "source/hero.png"),
    _manifest_asset("Web / Open Graph", "favicon", "favicon/favicon.ico", "source/logo.svg"),
    _manifest_asset("Web / Open Graph", "favicon", "favicon/favicon-16.png", "source/logo.svg"),
    _manifest_asset("Web / Open Graph", "favicon", "favicon/favicon-32.png", "source/logo.svg"),
    _manifest_asset("Web / Open Graph", "favicon", "favicon/favicon-48.png", "source/logo.svg"),
    _manifest_asset("Web / Open Graph", "favicon", "favicon/favicon-192.png", "source/logo.svg"),
    _manifest_asset("Web / Open Graph", "favicon", "favicon/favicon-512.png", "source/logo.svg"),
    _manifest_asset("Web / Open Graph", "favicon", "favicon/apple-touch-icon-180.png", "source/logo.svg"),
    _manifest_asset("Web / Open Graph", "banner", "banners/open-graph-1200x630.png", "source/hero.png"),
    _manifest_asset("Web / Open Graph", "banner", "banners/twitter-card-1200x628.png", "source/hero.png"),
]


def save_platform_manifest():
    out = ROOT / PLATFORM_MANIFEST_RELPATH
    out.write_text(
        json.dumps(
            {
                "schema_version": PLATFORM_MANIFEST_SCHEMA_VERSION,
                "upload_constraints": PLATFORM_UPLOAD_CONSTRAINTS,
                "platform_requirements": PLATFORM_REQUIREMENTS,
                "assets": PLATFORM_ASSETS,
                "requirement_evidence": PLATFORM_REQUIREMENT_EVIDENCE,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"  {PLATFORM_MANIFEST_RELPATH}: {len(PLATFORM_ASSETS)} platform assets")


def main():
    resvg = resolve_toolchain()
    with Image.open(HERO) as source:
        hero = source.convert("RGB")
    resvg_version = subprocess.run(
        [resvg, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    print(f"toolchain: resvg {resvg_version}, Pillow {Image.__version__}"
          f" (pinned per docs/notes/asset-toolchain.md)")
    print("logo source: vector (resvg)")

    print("palette:")
    save_palette()

    print("avatars:")
    for path, size in AVATARS.items():
        save(logo_at(size, resvg), path)

    print("logo masters:")
    for path, size in LOGO_SIZES.items():
        save(logo_at(size, resvg), path)
    shutil.copy(LOGO_SVG, ROOT / "logo/logo.svg")
    print("  logo/logo.svg: vector")
    shutil.copy(LOGO_PNG, ROOT / "logo/logo-original.png")
    print("  logo/logo-original.png: byte-for-byte copy")

    print("logo masters (transparent):")
    for path, size in LOGO_SIZES.items():
        transparent_img = logo_at_transparent(size, resvg)
        # Save with -transparent suffix
        base_path = path.replace(".png", "-transparent.png")
        save(transparent_img, base_path)
    shutil.copy(LOGO_TRANSPARENT_SVG, ROOT / "logo/logo-transparent.svg")
    print("  logo/logo-transparent.svg: vector (no background)")

    print("favicons:")
    for path, size in FAVICON_SIZES.items():
        save(logo_at(size, resvg), path)
    ico = ROOT / "favicon/favicon.ico"
    logo_at(256, resvg).save(
        ico, sizes=FAVICON_ICO_SIZES
    )
    print("  favicon/favicon.ico: multi-res")

    print("banners:")
    save_banners(hero)

    print("platform manifest:")
    save_platform_manifest()

    print("done.")


if __name__ == "__main__":
    main()
