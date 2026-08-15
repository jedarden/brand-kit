#!/usr/bin/env python3
"""Verify that committed PNG assets match the dimensions documented in README.md.

This script catches drift between documented dimensions and actual shipped files.
Run: python3 tools/verify_assets.py
Exits 1 on any mismatch, 0 if all dimensions match.
"""
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent

# Expected dimensions mirror the README.md table
# Format: {path: (expected_width, expected_height)}
EXPECTED_DIMENSIONS = {
    # Avatars (profile pictures)
    "avatars/x-400.png": (400, 400),
    "avatars/linkedin-400.png": (400, 400),
    "avatars/github-460.png": (460, 460),
    "avatars/instagram-320.png": (320, 320),
    "avatars/threads-320.png": (320, 320),
    "avatars/facebook-320.png": (320, 320),
    "avatars/youtube-800.png": (800, 800),
    "avatars/tiktok-200.png": (200, 200),
    "avatars/mastodon-400.png": (400, 400),
    "avatars/bluesky-400.png": (400, 400),
    "avatars/discord-512.png": (512, 512),

    # Banners / covers
    "banners/x-header-1500x500.png": (1500, 500),
    "banners/linkedin-personal-1584x396.png": (1584, 396),
    "banners/linkedin-company-1128x191.png": (1128, 191),
    "banners/facebook-cover-851x315.png": (851, 315),
    "banners/facebook-cover-2x-1702x630.png": (1702, 630),
    "banners/youtube-banner-2560x1440.png": (2560, 1440),
    "banners/discord-banner-960x540.png": (960, 540),
    "banners/github-social-1280x640.png": (1280, 640),
    "banners/open-graph-1200x630.png": (1200, 630),
    "banners/twitter-card-1200x628.png": (1200, 628),

    # Favicons
    "favicon/favicon-16.png": (16, 16),
    "favicon/favicon-32.png": (32, 32),
    "favicon/favicon-48.png": (48, 48),
    "favicon/favicon-192.png": (192, 192),
    "favicon/favicon-512.png": (512, 512),
    "favicon/apple-touch-icon-180.png": (180, 180),

    # Logo masters
    "logo/logo-256.png": (256, 256),
    "logo/logo-512.png": (512, 512),
    "logo/logo-1024.png": (1024, 1024),
    "logo/logo-original.png": (640, 640),
}


def verify_dimensions():
    """Check all PNG dimensions against expected values.

    Returns:
        list of tuples: (path, expected, actual, status_message)
    """
    results = []
    all_match = True

    for relpath, (expected_w, expected_h) in EXPECTED_DIMENSIONS.items():
        full_path = ROOT / relpath

        if not full_path.exists():
            results.append((relpath, f"{expected_w}×{expected_h}", "MISSING", "file not found"))
            all_match = False
            continue

        try:
            with Image.open(full_path) as img:
                actual_w, actual_h = img.size

                if (actual_w, actual_h) == (expected_w, expected_h):
                    results.append((relpath, f"{expected_w}×{expected_h}", f"{actual_w}×{actual_h}", "✓"))
                else:
                    results.append((relpath, f"{expected_w}×{expected_h}", f"{actual_w}×{actual_h}", "✗ MISMATCH"))
                    all_match = False
        except Exception as e:
            results.append((relpath, f"{expected_w}×{expected_h}", "ERROR", str(e)))
            all_match = False

    return results, all_match


def main():
    print("Verifying PNG dimensions against README.md documentation...\n")

    results, all_match = verify_dimensions()

    # Print results in columns
    print(f"{'File':<50} {'Expected':>12} {'Actual':>12} {'Status':<20}")
    print("-" * 94)

    for relpath, expected, actual, status in results:
        print(f"{relpath:<50} {expected:>12} {actual:>12} {status:<20}")

    print()

    if all_match:
        print("✓ All dimensions match documentation")
        return 0
    else:
        print("✗ DIMENSION MISMATCHES FOUND")
        print("\nTo fix:")
        print("1. Update README.md to match actual dimensions, OR")
        print("2. Run: python3 tools/build_assets.py to regenerate assets")
        print("3. Commit the corrected assets")
        return 1


if __name__ == "__main__":
    exit(main())
