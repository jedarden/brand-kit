#!/usr/bin/env python3
"""Audit downstream copies against a published brand-kit release without changing them."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "consumer-drift.json"
DEFAULT_SITE = Path.home() / "jedarden.com"
DEFAULT_API_URL = "https://git.ardenone.com/api/v1"
DEFAULT_REPOSITORY = "jedarden/brand-kit"
DEFAULT_TAG_PATTERN = r"^v\d+\.\d+\.\d+$"
USER_AGENT = "brand-kit-consumer-drift/1"


class AuditError(RuntimeError):
    """Base class for conditions that make an audit indeterminate."""


class ConfigError(AuditError):
    """Raised when the consumer inventory is invalid."""


class ReleaseError(AuditError):
    """Raised when a published release cannot be resolved safely."""


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read consumer inventory {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ConfigError("consumer inventory must be an object with schema_version 1")
    for section in (
        "release",
        "site",
        "live",
        "source_assets",
        "site_assets",
        "live_assets",
    ):
        if section not in value:
            raise ConfigError(f"consumer inventory is missing {section!r}")
    if not isinstance(value["release"], dict) or not isinstance(value["site"], dict):
        raise ConfigError("release and site sections must be objects")
    if not isinstance(value["live"], dict):
        raise ConfigError("live section must be an object")
    for section in ("source_assets", "site_assets", "live_assets"):
        if not isinstance(value[section], list) or not value[section]:
            raise ConfigError(f"{section} must be a non-empty array")
    return value


def _relative_path(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ConfigError(f"{label} must be a non-empty relative path")
    candidate = (root / value).resolve()
    root = root.resolve()
    if candidate != root and root not in candidate.parents:
        raise ConfigError(f"{label} escapes its root")
    return candidate


def _api_base(config: dict[str, Any]) -> str:
    value = config["release"].get("api_url", DEFAULT_API_URL)
    if not isinstance(value, str) or not value:
        raise ConfigError("release.api_url must be a non-empty string")
    return value.rstrip("/")


def _repository(config: dict[str, Any]) -> str:
    value = config["release"].get("repository", DEFAULT_REPOSITORY)
    if not isinstance(value, str) or not value or value.count("/") != 1:
        raise ConfigError("release.repository must be owner/name")
    return value


def release_list_url(config: dict[str, Any]) -> str:
    return f"{_api_base(config)}/repos/{_repository(config)}/releases?limit=50"


def release_url(tag: str, config: dict[str, Any]) -> str:
    if not isinstance(tag, str) or not tag.strip():
        raise ReleaseError("release tag must not be empty")
    encoded = urllib.parse.quote(tag, safe="")
    return f"{_api_base(config)}/repos/{_repository(config)}/releases/tags/{encoded}"


def fetch_bytes(url: str, headers: dict[str, str] | None = None) -> bytes:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read()


def _call_fetcher(
    fetcher: Callable[..., bytes], url: str, headers: dict[str, str] | None
) -> bytes:
    if headers:
        return fetcher(url, headers)
    return fetcher(url)


def _decode_json(body: bytes, label: str) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"{label} returned invalid JSON: {exc}") from exc


def _api_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"token {token}"} if token else {}


def _release_tag(record: Any, expected: str | None = None) -> str:
    if not isinstance(record, dict):
        raise ReleaseError("release response is not an object")
    tag = record.get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        raise ReleaseError("release response has no tag_name")
    if record.get("draft") is not False:
        raise ReleaseError(f"release {tag} is not published")
    if record.get("prerelease") is True:
        raise ReleaseError(f"release {tag} is a prerelease")
    if expected is not None and tag != expected:
        raise ReleaseError(f"release response names {tag!r}, expected {expected!r}")
    return tag


def fetch_release(
    tag: str,
    config: dict[str, Any],
    token: str | None = None,
    fetcher: Callable[..., bytes] | None = None,
) -> dict[str, Any]:
    fetcher = fetcher or fetch_bytes
    headers = _api_headers(token)
    try:
        body = _call_fetcher(fetcher, release_url(tag, config), headers)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ReleaseError(f"cannot read release {tag}: {exc}") from exc
    record = _decode_json(body, f"release {tag}")
    _release_tag(record, tag)
    if not isinstance(record, dict):
        raise ReleaseError(f"release {tag} is not an object")
    return record


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _release_sort_key(record: dict[str, Any]) -> tuple[datetime, str]:
    timestamp = _timestamp(record.get("published_at")) or _timestamp(
        record.get("created_at")
    )
    if timestamp is None:
        return datetime.min.replace(tzinfo=timezone.utc), str(
            record.get("tag_name", "")
        )
    return timestamp, str(record.get("tag_name", ""))


def discover_release(
    config: dict[str, Any],
    token: str | None = None,
    fetcher: Callable[..., bytes] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    fetcher = fetcher or fetch_bytes
    headers = _api_headers(token)
    try:
        body = _call_fetcher(fetcher, release_list_url(config), headers)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ReleaseError(f"cannot list releases: {exc}") from exc
    records = _decode_json(body, "release list")
    if not isinstance(records, list):
        raise ReleaseError("release list response is not an array")
    pattern_value = config["release"].get("tag_pattern", DEFAULT_TAG_PATTERN)
    try:
        pattern = re.compile(pattern_value)
    except (TypeError, re.error) as exc:
        raise ConfigError(
            "release.tag_pattern is not a valid regular expression"
        ) from exc
    candidates: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        tag = record.get("tag_name")
        if not isinstance(tag, str) or not pattern.fullmatch(tag):
            continue
        try:
            _release_tag(record)
        except ReleaseError:
            continue
        candidates.append(record)
    if not candidates:
        raise ReleaseError(
            "no published stable release matches the configured tag pattern"
        )
    age = config["release"].get("minimum_age_hours", 0)
    if not isinstance(age, (int, float)) or age < 0:
        raise ConfigError("release.minimum_age_hours must be a non-negative number")
    if age:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        cutoff = current.astimezone(timezone.utc) - timedelta(hours=age)
        eligible: list[dict[str, Any]] = []
        for record in candidates:
            published = _timestamp(record.get("published_at")) or _timestamp(
                record.get("created_at")
            )
            if published is not None and published <= cutoff:
                eligible.append(record)
        candidates = eligible
        if not candidates:
            raise ReleaseError(
                f"no published stable release is at least {age:g} hours old"
            )
    return max(candidates, key=_release_sort_key)


def _git_value(root: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def checkout_state(root: Path, tag: str, required: bool) -> dict[str, Any]:
    head = _git_value(root, "rev-parse", "--verify", "HEAD")
    tag_commit = _git_value(
        root, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"
    )
    state = {
        "head": head,
        "tag_commit": tag_commit,
        "matches": bool(head and tag_commit and head == tag_commit),
    }
    if required and not state["matches"]:
        raise ReleaseError(
            f"checkout is not the published release tag {tag} "
            f"(HEAD {head or 'unknown'}, tag {tag_commit or 'unknown'})"
        )
    return state


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_rgb(path: Path) -> Image.Image:
    try:
        with Image.open(path) as image:
            return image.convert("RGB").copy()
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot decode image {path}: {exc}") from exc


def _load_rgb_bytes(value: bytes, label: str) -> Image.Image:
    try:
        with Image.open(io.BytesIO(value)) as image:
            return image.convert("RGB").copy()
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot decode {label}: {exc}") from exc


def hero_crop(root: Path, width: int, height: int, fy: float) -> Image.Image:
    source = _load_rgb(root / "source/hero.png")
    scale = max(width / source.width, height / source.height)
    resized = source.resize(
        (round(source.width * scale), round(source.height * scale)),
        Image.Resampling.LANCZOS,
    )
    left = round((resized.width - width) / 2)
    top = round((resized.height - height) * fy)
    return resized.crop((left, top, left + width, top + height))


def _expected_image(root: Path, rule: dict[str, Any]) -> Image.Image:
    if rule.get("comparison") == "crop":
        crop = rule.get("crop")
        if not isinstance(crop, dict):
            raise ConfigError("crop comparison requires a crop object")
        try:
            width = int(crop["width"])
            height = int(crop["height"])
            fy = float(crop["fy"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError("crop comparison has invalid dimensions") from exc
        if width <= 0 or height <= 0 or not 0 <= fy <= 1:
            raise ConfigError("crop comparison has invalid dimensions")
        return hero_crop(root, width, height, fy)
    source = rule.get("source")
    if not isinstance(source, str):
        raise ConfigError("image comparison requires a source")
    return _load_rgb(_relative_path(root, source, "image source"))


def _image_check(
    observed: bytes,
    expected: Image.Image,
    tolerance: float,
    label: str,
    resize_reference: bool = False,
) -> dict[str, Any]:
    try:
        actual = _load_rgb_bytes(observed, label)
    except ValueError as exc:
        return {"status": "stale", "reason": str(exc)}
    expected_size = expected.size
    if actual.size != expected.size:
        if not resize_reference:
            return {
                "status": "stale",
                "reason": f"image size {actual.size} differs from {expected.size}",
                "observed_size": list(actual.size),
                "expected_size": list(expected_size),
            }
        expected = expected.resize(actual.size, Image.Resampling.LANCZOS)
    difference = ImageChops.difference(actual, expected)
    channel_means = ImageStat.Stat(difference).mean
    mean_luma = ImageStat.Stat(difference.convert("L")).mean[0]
    metrics = {
        "mean_luma": round(mean_luma, 4),
        "max_channel": round(max(channel_means), 4),
        "observed_size": list(actual.size),
        "expected_size": list(expected_size),
    }
    if mean_luma <= tolerance:
        return {"status": "current", **metrics}
    return {
        "status": "stale",
        "reason": f"mean luma difference {mean_luma:.4f} exceeds {tolerance:g}",
        **metrics,
    }


def _rule_name(rule: dict[str, Any], fallback: str) -> str:
    value = rule.get("name", fallback)
    return value if isinstance(value, str) and value else fallback


def audit_site_assets(
    root: Path,
    site: Path,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    consumer = str(config["site"].get("name", "jedarden.com"))
    for index, rule in enumerate(config["site_assets"]):
        if not isinstance(rule, dict):
            raise ConfigError(f"site_assets[{index}] must be an object")
        name = _rule_name(rule, f"site asset {index}")
        source = rule.get("source")
        observed_relative = rule.get("path")
        if not isinstance(source, str) or not isinstance(observed_relative, str):
            raise ConfigError(f"site asset {name!r} requires source and path")
        result: dict[str, Any] = {
            "consumer": consumer,
            "asset": name,
            "path": observed_relative,
            "source": source,
        }
        try:
            expected_path = _relative_path(root, source, f"site asset {name} source")
            observed_path = _relative_path(
                site, observed_relative, f"site asset {name} path"
            )
            if not expected_path.is_file():
                result.update(status="unavailable", reason="release source is missing")
                checks.append(result)
                continue
            if not observed_path.is_file():
                result.update(status="stale", reason="consumer copy is missing")
                checks.append(result)
                continue
            if rule.get("comparison", "bytes") == "bytes":
                expected_digest = sha256_file(expected_path)
                observed_digest = sha256_file(observed_path)
                result.update(
                    expected_sha256=expected_digest,
                    observed_sha256=observed_digest,
                )
                if expected_digest == observed_digest:
                    result["status"] = "current"
                else:
                    result.update(
                        status="stale",
                        reason="consumer bytes differ from the release copy",
                    )
            elif rule.get("comparison") in {"image", "crop"}:
                tolerance = float(rule.get("tolerance", 3.0))
                expected = _expected_image(root, rule)
                result["observed_sha256"] = sha256_file(observed_path)
                result.update(
                    _image_check(
                        observed_path.read_bytes(),
                        expected,
                        tolerance,
                        f"consumer asset {name}",
                    )
                )
            else:
                raise ConfigError(f"site asset {name!r} has an unknown comparison")
        except (ConfigError, ValueError) as exc:
            result.update(status="unavailable", reason=str(exc))
        checks.append(result)
    return checks


def audit_live_assets(
    root: Path,
    config: dict[str, Any],
    offline: bool = False,
    fetcher: Callable[..., bytes] | None = None,
) -> list[dict[str, Any]]:
    fetcher = fetcher or fetch_bytes
    checks: list[dict[str, Any]] = []
    for index, rule in enumerate(config["live_assets"]):
        if not isinstance(rule, dict):
            raise ConfigError(f"live_assets[{index}] must be an object")
        name = _rule_name(rule, f"live asset {index}")
        url = rule.get("url")
        source = rule.get("source")
        if not isinstance(url, str) or not url:
            raise ConfigError(f"live asset {name!r} requires a URL")
        if not isinstance(source, str):
            raise ConfigError(f"live asset {name!r} requires a source")
        result: dict[str, Any] = {
            "consumer": str(rule.get("consumer", "live consumer")),
            "asset": name,
            "url": url,
            "source": source,
        }
        if offline:
            result.update(status="unavailable", reason="offline mode was requested")
            checks.append(result)
            continue
        try:
            expected_path = _relative_path(root, source, f"live asset {name} source")
            if not expected_path.is_file():
                raise FileNotFoundError(expected_path)
            try:
                observed = _call_fetcher(fetcher, url, None)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                result.update(status="unavailable", reason=f"fetch failed: {exc}")
                checks.append(result)
                continue
            expected = _expected_image(root, rule)
            result["observed_sha256"] = sha256_bytes(observed)
            result.update(
                _image_check(
                    observed,
                    expected,
                    float(rule.get("tolerance", 3.0)),
                    f"live asset {name}",
                    bool(rule.get("resize_reference", False)),
                )
            )
        except (ConfigError, ValueError, FileNotFoundError) as exc:
            result.update(status="unavailable", reason=str(exc))
        checks.append(result)
    return checks


def _consumer_summary(checks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    precedence = {"current": 0, "unavailable": 1, "stale": 2}
    for check in checks:
        name = str(check.get("consumer", "unknown"))
        status = str(check.get("status", "unavailable"))
        current = summary.setdefault(name, {"status": "current", "checks": 0})
        current["checks"] += 1
        if precedence.get(status, 1) > precedence.get(current["status"], 0):
            current["status"] = status
    return summary


def _report_status(checks: list[dict[str, Any]], errors: list[str]) -> str:
    statuses = {str(check.get("status")) for check in checks}
    if "stale" in statuses:
        return "stale"
    if errors or "unavailable" in statuses:
        return "indeterminate"
    return "current"


def _base_report(
    root: Path,
    site: Path,
    release_tag: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "indeterminate",
        "release": {"tag": release_tag},
        "site": {"name": "jedarden.com", "path": str(site)},
        "source_digests": [],
        "checks": [],
        "consumers": {},
        "errors": [],
    }


def _source_digests(root: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    digests: list[dict[str, Any]] = []
    for index, entry in enumerate(config["source_assets"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ConfigError(f"source_assets[{index}] requires a path")
        path = _relative_path(root, entry["path"], f"source asset {entry['path']}")
        if not path.is_file():
            raise ConfigError(f"release source is missing: {entry['path']}")
        digests.append(
            {
                "path": entry["path"],
                "role": entry.get("role", "source"),
                "sha256": sha256_file(path),
            }
        )
    return digests


def run_audit(
    root: Path = ROOT,
    site: Path | None = None,
    config: dict[str, Any] | None = None,
    release_tag: str | None = None,
    token: str | None = None,
    offline: bool = False,
    fetcher: Callable[..., bytes] | None = None,
    now: datetime | None = None,
    allow_unverified_checkout: bool = False,
) -> dict[str, Any]:
    config = config or load_config()
    if site is None:
        configured = config["site"].get("checkout", str(DEFAULT_SITE))
        site = Path(str(configured)).expanduser()
    root = root.expanduser().resolve()
    site = site.expanduser().resolve()
    report = _base_report(root, site, release_tag)
    errors: list[str] = []
    record: dict[str, Any] | None = None
    try:
        if release_tag:
            record = fetch_release(release_tag, config, token, fetcher)
            report["release"]["tag"] = _release_tag(record, release_tag)
        else:
            record = discover_release(config, token, fetcher, now)
            report["release"]["tag"] = _release_tag(record)
        report["release"]["published_at"] = record.get("published_at")
        report["release"]["record_target"] = record.get("target_commitish")
        required = bool(config["release"].get("require_checkout", True))
        if required and not allow_unverified_checkout:
            report["release"]["checkout"] = checkout_state(
                root, str(report["release"]["tag"]), True
            )
        else:
            report["release"]["checkout"] = checkout_state(
                root, str(report["release"]["tag"]), False
            )
    except (AuditError, OSError) as exc:
        errors.append(str(exc))
        report["errors"] = errors
        report["status"] = _report_status([], errors)
        return report

    try:
        report["source_digests"] = _source_digests(root, config)
        report["site"]["name"] = str(config["site"].get("name", "jedarden.com"))
        report["site"]["commit"] = _git_value(site, "rev-parse", "--verify", "HEAD")
        report["checks"] = audit_site_assets(root, site, config)
        report["checks"].extend(audit_live_assets(root, config, offline, fetcher))
    except (AuditError, OSError, ValueError) as exc:
        errors.append(str(exc))
    report["consumers"] = _consumer_summary(report["checks"])
    report["status"] = _report_status(report["checks"], errors)
    return report


def _render_human(report: dict[str, Any]) -> None:
    release = report.get("release", {})
    site = report.get("site", {})
    print(f"Consumer drift: {str(report.get('status', 'indeterminate')).upper()}")
    print(f"release: {release.get('tag', '<unresolved>')}")
    print(
        f"consumer checkout: {site.get('name', 'unknown')} ({site.get('path', 'unknown')})"
    )
    for check in report.get("checks", []):
        status = str(check.get("status", "unavailable")).upper()
        label = check.get("asset", "asset")
        location = check.get("path", check.get("url", ""))
        detail = check.get("reason", "")
        suffix = f" — {detail}" if detail else ""
        print(f"{status:11} {label} @ {location}{suffix}")
    for error in report.get("errors", []):
        print(f"ERROR       {error}")
    if report.get("status") == "current":
        print("All known consumers match the selected release.")
    elif report.get("status") == "stale":
        print("Stale consumers were found; no consumer files were changed.")
    else:
        print(
            "The audit could not prove consumer freshness; no consumer files were changed."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--site", type=Path)
    parser.add_argument("--release-tag")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--allow-unverified-checkout", action="store_true")
    parser.add_argument("--print-release-tag", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.print_release_tag:
            record = discover_release(
                config,
                token=os.environ.get("FORGEJO_TOKEN")
                or os.environ.get("FORGEJO_API_TOKEN"),
            )
            print(_release_tag(record))
            return 0
        report = run_audit(
            root=args.source_root,
            site=args.site,
            config=config,
            release_tag=args.release_tag,
            token=os.environ.get("FORGEJO_TOKEN")
            or os.environ.get("FORGEJO_API_TOKEN"),
            offline=args.offline,
            allow_unverified_checkout=args.allow_unverified_checkout,
        )
    except Exception as exc:
        report = _base_report(
            args.source_root, args.site or DEFAULT_SITE, args.release_tag
        )
        report["errors"] = [str(exc)]
        report["status"] = "indeterminate"
    if args.as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _render_human(report)
    if args.report:
        try:
            args.report.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            print(f"error: cannot write report {args.report}: {exc}", file=sys.stderr)
            return 2
    if report.get("status") == "current":
        return 0
    if report.get("status") == "stale":
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
