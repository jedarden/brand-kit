#!/usr/bin/env python3
"""Regenerate every platform asset in the brand kit from the two canonical sources.

Sources:
  source/logo.png  -- flat cartoon avatar (red polo), used for all profile pictures
  source/hero.png  -- photoreal desk scene (red polo), used for all banners/covers

Run:  python3 tools/build_assets.py
"""
from PIL import Image
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "source"
LOGO = Image.open(SRC / "logo.png").convert("RGB")
HERO = Image.open(SRC / "hero.png").convert("RGB")

# Brand background (sampled from the logo's cream field) -- used to pad where needed.
CREAM = (242, 232, 213)


def save(img, relpath):
    out = ROOT / relpath
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"  {relpath}: {img.size[0]}x{img.size[1]}")


def square(img, size):
    """Square profile asset from the (already square) logo."""
    return img.resize((size, size), Image.LANCZOS)


def cover(img, tw, th, fy=0.45, fx=0.5):
    """Scale-to-cover then crop to (tw,th), biased vertically by fy (0=top,1=bottom)."""
    iw, ih = img.size
    scale = max(tw / iw, th / ih)
    nw, nh = round(iw * scale), round(ih * scale)
    im = img.resize((nw, nh), Image.LANCZOS)
    left = round((nw - tw) * fx)
    top = round((nh - th) * fy)
    return im.crop((left, top, left + tw, top + th))


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


def main():
    print("avatars:")
    for path, size in AVATARS.items():
        save(square(LOGO, size), path)

    print("logo masters:")
    for path, size in LOGO_SIZES.items():
        save(square(LOGO, size), path)
    save(LOGO, "logo/logo-original.png")

    print("favicons:")
    for path, size in FAVICON_SIZES.items():
        save(square(LOGO, size), path)
    # multi-resolution .ico
    ico = ROOT / "favicon/favicon.ico"
    LOGO.resize((256, 256), Image.LANCZOS).save(
        ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    )
    print(f"  favicon/favicon.ico: multi-res")

    print("banners:")
    for path, (w, h, fy) in BANNERS.items():
        save(cover(HERO, w, h, fy=fy), path)

    print("done.")


if __name__ == "__main__":
    main()
